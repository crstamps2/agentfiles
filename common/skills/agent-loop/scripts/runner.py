"""Plan 1 deterministic implementation runner."""
from __future__ import annotations
import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
import pathlib
import stat
import subprocess
import sys
import uuid

import admission
import agentdef
import config
import contracts
import ladder
import locks
import metrics
import procs
import state
import worktree


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _task_md(task: contracts.Task, task_dir: pathlib.Path, wt: pathlib.Path) -> str:
    def bl(items): return "\n".join(f"- {i}" for i in items) or "- (none)"
    return f"""# Task {task.id}: {task.summary}
Task directory: {task_dir}
Worktree: {wt}
Visual evidence required: {"yes" if task.visual else "no"}
Stage timeout: {task.timeout_s // 60} minutes
May edit tests/fixtures: {"yes" if task.may_edit_tests else "no"}

## Allowed files
{bl(task.allowed_files)}

## Invariants
{bl(task.invariants)}

## Out of scope
{bl(task.out_of_scope)}

## Acceptance
{bl(task.acceptance)}

## Verification commands
{bl(task.verification_commands)}

## Stop / escalate when
{bl(task.stop_when)}

If `{task_dir}/feedback.md` exists, read it first. Write `{task_dir}/result.md` when done, even on failure.
Do not commit or push. Never weaken an acceptance check.
"""


def _classify(res: contracts.Result | None, stage: procs.StageResult, violations: list,
              stderr: str = "") -> tuple[str, str]:
    """Classify worker output; runner-observed violations outrank worker claims."""
    if violations:
        return "rejected", "; ".join(violations)
    if stage.timed_out:
        return "timeout", "stage timeout"
    if res is None:
        if stage.returncode and any(__import__("re").search(p, stderr, __import__("re").I)
                                    for p in (r"429", r"quota", r"rate.?limit", r"ECONNREFUSED",
                                              r"ENOTFOUND", r"unauthorized", r"401", r"5\d\d", r"connection")):
            return "environment", "worker transport failure"
        return "protocol", "result.md missing or without STATUS"
    if res.status == "blocked":
        return ("blocked", "owner") if res.reason == "owner" else ("protocol", f"blocked/{res.reason or 'unspecified'}")
    if res.reason == "environment":
        return "environment", res.next or "environment"
    if res.status == "pass":
        return "accepted", "none"
    return "rejected", f"{res.status}/{res.reason}: {res.next}"


def _task_toml(task: contracts.Task) -> str:
    def arr(xs): return "[" + ", ".join(json.dumps(x) for x in xs) + "]"
    return (f'id = {json.dumps(task.id)}\nslug = {json.dumps(task.slug)}\nsummary = {json.dumps(task.summary)}\n'
            f'allowed_files = {arr(task.allowed_files)}\nverification_commands = {arr(task.verification_commands)}\n'
            f'acceptance = {arr(task.acceptance)}\nmay_edit_tests = {str(task.may_edit_tests).lower()}\n'
            f'visual = {str(task.visual).lower()}\ntimeout_s = {task.timeout_s}\n')


class Runner:
    def __init__(self, cfg: config.Config, run_id: str | None = None, pi_launcher=None):
        self.cfg = cfg
        self.run_id = run_id or uuid.uuid4().hex[:8]
        self.pi_launcher = pi_launcher

    @property
    def launch(self):
        return self.pi_launcher or procs.run_stage

    @staticmethod
    def _write_artifact(path, text: str) -> None:
        """Create an attempt artifact without ever traversing a supplied symlink."""
        path = pathlib.Path(path)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)

    @staticmethod
    def _rewrite_artifact(path, text: str) -> None:
        path = pathlib.Path(path)
        try:
            if not stat.S_ISREG(os.lstat(path).st_mode):
                raise RuntimeError(f"unsafe artifact path: {path}")
        except FileNotFoundError:
            return Runner._write_artifact(path, text)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            Runner._write_artifact(tmp, text); os.replace(tmp, path)
        finally:
            if tmp.exists(): tmp.unlink()
        for stale in path.parent.glob(f"{path.name}.*tmp"):      # sweep predecessors' leftovers
            try: stale.unlink()
            except OSError: pass

    def _fingerprint(self, task, wt) -> str:
        raw = json.dumps(dataclasses.asdict(task), sort_keys=True) + "|" + str(pathlib.Path(wt).resolve())
        return hashlib.sha256(raw.encode()).hexdigest()[:12]

    def _history_key(self, task, wt) -> str:
        return f"{task.id}@{self._fingerprint(task, wt)}"

    def _attempt_root(self, t, task) -> pathlib.Path:
        # Disk artifacts remain human-addressable by the manifest task id. History is fingerprinted.
        return self.cfg.state_root / "attempts" / t.key / task.id

    def _fence_path(self):
        return self.cfg.state_root / "locks" / "heavy.fence"

    def _fence(self, stage, t, task, n):
        self._fence_path().parent.mkdir(parents=True, exist_ok=True)
        self._rewrite_artifact(self._fence_path(), json.dumps({"pgid": stage.pgid, "ticket": t.key,
            "task": task.id, "attempt": n, "reason": "termination unverified"}))

    def _clear_or_honor_fence(self, tdir, t) -> bool:
        p = self._fence_path()
        if not p.exists(): return False
        try:
            data = json.loads(p.read_text()); alive = procs.group_alive(int(data["pgid"]))
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            alive = True                         # corrupt fence fails closed
        if alive:
            self._save(tdir, state.transition(t, "paused", reason="fenced"))
            return True
        p.unlink()
        return False

    def _publish_row(self, row_path):
        try:
            row = json.loads(pathlib.Path(row_path).read_text())
        except (OSError, json.JSONDecodeError):
            return
        if row.get("published"):
            return
        data = dict(row); data.pop("published", None)
        # Reconciliation is idempotent across a crash after append and before rewrite.
        if not any(r.get("evidence_path") == data.get("evidence_path") for r in metrics.read_all(self.cfg.state_root)):
            metrics.append(self.cfg.state_root, data)
        row["published"] = True
        self._rewrite_artifact(row_path, json.dumps(row, sort_keys=True))

    def _recover(self, t, task):
        root = self._attempt_root(t, task)
        if not root.exists(): return
        for adir in root.iterdir():
            if not adir.is_dir(): continue
            ap = adir / "attempt.json"
            try:
                obj = json.loads(ap.read_text())
            except (OSError, json.JSONDecodeError):
                obj = None
            if obj and obj.get("status") in {"launching", "running"}:
                pgids = [obj.get("pgid")] + list(obj.get("verify_pgids", []))
                survivor = None
                survivor_reason = None
                for pgid in [g for g in pgids if g]:
                    pgid = int(pgid)
                    gstate = procs.group_state(pgid)
                    if gstate == "dead":
                        continue
                    if procs.kill_group(pgid):
                        continue
                    survivor = pgid
                    survivor_reason = ("zombie-or-foreign group; operator must verify and clear"
                                        if gstate == "zombie-or-foreign" else "recovery: group survived kill")
                    break
                if survivor is not None:
                    # Mark the attempt orphaned BEFORE writing the fence, so a crash between
                    # these writes doesn't leave the attempt in a recoverable state (running)
                    # that would cause re-fencing forever when the fence is later cleared.
                    obj["status"] = "orphaned"
                    obj["orphaned_by"] = "recovery: " + survivor_reason
                    self._rewrite_artifact(ap, json.dumps(obj, sort_keys=True))
                    self._fence_path().parent.mkdir(parents=True, exist_ok=True)
                    self._rewrite_artifact(self._fence_path(), json.dumps({"pgid": survivor, "ticket": t.key,
                        "task": task.id, "attempt": adir.name, "reason": survivor_reason}))
                    return "fenced"
                obj["status"] = "interrupted"
                self._rewrite_artifact(ap, json.dumps(obj, sort_keys=True))
                row = {"run_id": self.run_id, "ticket": t.key, "task_id": task.id, "stage": "implement",
                       "attempt": int(adir.name) if adir.name.isdigit() else 0, "outcome": "interrupted",
                       "reason": "runner recovery", "evidence_path": str(adir), "published": False}
                rp = adir / "row.json"
                if not rp.exists(): self._write_artifact(rp, json.dumps(row, sort_keys=True))
            rp = adir / "row.json"
            if rp.exists(): self._publish_row(rp)
        return None

    def _stop(self, tdir, t):
        if state.paused(self.cfg.state_root):
            if t.state != "paused": self._save(tdir, state.transition(t, "paused", reason="PAUSE file present"))
            return "paused"
        if state.human_owned(tdir):
            if t.state != "paused": self._save(tdir, state.transition(t, "paused", reason="HUMAN file present"))
            return "paused"
        return None

    def implement_task(self, t: state.Ticket, task: contracts.Task, stratum_index: int, wt) -> str:
        wt, tdir = pathlib.Path(wt), self.cfg.ticket_dir(t.key)
        if t.state == "blocked":
            return "blocked"
        stop = self._stop(tdir, t)
        if t.state == "paused":
            if stop: return "paused"
            if t.previous != "implement":
                raise state.IllegalTransition(f"{t.key}: paused from {t.previous}, not implement")
            t = state.transition(t, "implement"); self._save(tdir, t)
        elif t.state != "implement":
            raise state.IllegalTransition(f"{t.key}: cannot implement from {t.state}")
        if self._recover(t, task) == "fenced":
            self._save(tdir, state.transition(t, "paused", reason="fenced: recovery found a live group")); return "paused"
        if self._clear_or_honor_fence(tdir, t): return "paused"

        key = self._history_key(task, wt)
        for old_key in t.attempts:
            if old_key.startswith(task.id + "@") and old_key != key:
                print(f"task {task.id}: ignoring attempts from prior task/worktree generation {old_key}", file=sys.stderr)
        history = t.attempts.setdefault(key, [])
        spec = self.cfg.tickets.get(t.key)
        arm = history[0].get("arm") if history else None
        if arm is None:
            arm = ladder.assign_arm(stratum_index, task.visual, spec.pin_arm if spec else None, self.cfg.arms_alternate)
        attempts = [ladder.Attempt(ladder.Rung(**a["rung"]), a["outcome"])
                    for a in history if a.get("outcome") in ladder.OUTCOMES]
        env_failures = 0
        for rec in reversed(history):
            if rec.get("outcome") == "environment": env_failures += 1
            else: break

        while True:
            rung = ladder.next_rung(attempts, arm)
            if rung is None:
                if attempts and attempts[-1].outcome == "accepted": return "accepted"
                self._save(tdir, state.transition(t, "blocked", reason=f"task {task.id}: ladder exhausted")); return "blocked"
            stop = self._stop(tdir, t)
            if stop: return stop
            reading = admission.probe(self.cfg.state_root)
            decision = admission.decide(reading, self.cfg.admission)
            if not decision.ok:
                self._save(tdir, state.transition(t, "paused", reason="resource: " + "; ".join(decision.reasons))); return "paused"
            if self._clear_or_honor_fence(tdir, t): return "paused"
            lease = locks.Lease(self.cfg.state_root / "locks" / "heavy", f"implement {t.key}/{task.id}")
            if not lease.acquire():
                self._save(tdir, state.transition(t, "paused", reason="heavy lane held by a live owner")); return "paused"
            try:
                root = self._attempt_root(t, task); root.mkdir(parents=True, exist_ok=True)
                nums = [int(p.name) for p in root.iterdir() if p.is_dir() and p.name.isdigit()]
                n = 1 + max(nums, default=0)
                outcome, reason, row = self._attempt(t, task, wt, arm, rung, n)
            finally:
                lease.release()
            history.append({"rung": dataclasses.asdict(rung), "outcome": outcome, "reason": reason, "n": n, "arm": arm})
            self._save(tdir, t)
            self._publish_row(pathlib.Path(row["evidence_path"]) / "row.json")
            if outcome == "environment" and reason.startswith("termination unverified"):
                self._save(tdir, state.transition(t, "paused", reason=f"task {task.id}: {reason}")); return "paused"
            env_failures = env_failures + 1 if outcome == "environment" else 0
            if outcome == "accepted": attempts.append(ladder.Attempt(rung, outcome)); return "accepted"
            if outcome == "blocked":
                self._save(tdir, state.transition(t, "blocked", reason=f"task {task.id}: {reason}")); return "blocked"
            attempts.append(ladder.Attempt(rung, outcome))
            if outcome == "environment" and env_failures >= 2:
                self._save(tdir, state.transition(t, "paused", reason=f"task {task.id}: repeated environment failure: {reason}")); return "paused"
            if outcome in ("rejected", "protocol"):
                ladder.append_feedback(self._attempt_root(t, task), n, reason)

    def _attempt(self, t, task, wt, arm, rung, n):
        adir = self._attempt_root(t, task) / str(n)
        adir.mkdir(parents=True, exist_ok=False)
        self._write_artifact(adir / "task.toml", _task_toml(task))
        self._write_artifact(adir / "task.md", _task_md(task, adir, wt))
        task_level = self._attempt_root(t, task)
        if (task_level / "feedback.md").exists(): self._write_artifact(adir / "feedback.md", (task_level / "feedback.md").read_text())
        agent = agentdef.load(self.cfg.pi_agents_dir, rung.agent); agentdef.assert_worker_safe(agent)
        self._write_artifact(adir / "body.md", agent.body)
        prompt = adir / "prompt.md"; self._write_artifact(prompt, f"Your task file is {adir / 'task.md'}. Read it, then begin. Write result.md to {adir}.\n")
        base = worktree.snapshot(wt); self._write_artifact(adir / "base_tree", base)
        started = _now()
        self._write_artifact(adir / "attempt.json", json.dumps({"status": "launching", "started_utc": started,
            "base_tree": base, "agent": rung.agent, "rung": dataclasses.asdict(rung)}, sort_keys=True))
        argv = agentdef.pi_argv(agent, prompt, adir / "session", adir / "body.md")
        env = {**os.environ, "AL_TASK_DIR": str(adir), "AL_TICKET": t.key, "AL_TASK": task.id}
        timeout = min(float(task.timeout_s), float(self.cfg.heavy_stage_timeout_s))
        def on_start(pgid):
            self._rewrite_artifact(adir / "attempt.json", json.dumps({"status": "running", "pgid": pgid, "started_utc": started,
                "base_tree": base, "agent": rung.agent, "rung": dataclasses.asdict(rung)}, sort_keys=True))
        stage = self.launch(argv, wt, timeout, env, adir / "stdout.log", adir / "stderr.log", on_start=on_start)
        end = _now()
        verification_seconds = 0.0
        violations = []
        verify_pgids = []
        def on_verify_start(pgid):
            verify_pgids.append(pgid)
            self._rewrite_artifact(adir / "attempt.json", json.dumps({"status": "running", "pgid": stage.pgid,
                "started_utc": started, "base_tree": base, "agent": rung.agent, "rung": dataclasses.asdict(rung),
                "verify_pgids": list(verify_pgids)}, sort_keys=True))
        if not stage.terminated:
            self._fence(stage, t, task, n)
            outcome, reason, changed = "environment", f"termination unverified pgid {stage.pgid}", []
        else:
            res = None
            rp = adir / "result.md"
            try:
                if stat.S_ISREG(os.lstat(rp).st_mode): res = contracts.parse_result(rp.read_text())
            except (FileNotFoundError, contracts.ProtocolError, OSError): pass
            changed = worktree.changed_paths(wt, base)
            violations = worktree.check_allowlist(changed, task, self.cfg.protected_paths, self.cfg.test_path_globs)
            try: stderr = (adir / "stderr.log").read_text(errors="replace")
            except OSError: stderr = ""
            outcome, reason = _classify(res, stage, violations, stderr)
            if outcome == "accepted":
                verify_outcome, verify_reason = "accepted", "none"
                for i, cmd in enumerate(task.verification_commands):
                    try:
                        vs = procs.run_stage(["/bin/sh", "-c", cmd], wt, timeout, env, adir / f"verify-{i}.out", adir / f"verify-{i}.err",
                                              on_start=on_verify_start)
                    except procs.LogPathExists as e:
                        verify_outcome, verify_reason = "protocol", f"worker pre-created verification artifact: {e}"
                        break
                    verification_seconds += vs.elapsed_s
                    if not vs.terminated:
                        self._fence(vs, t, task, n)
                        verify_outcome, verify_reason = "environment", f"termination unverified pgid {vs.pgid}"
                        break
                    if vs.timed_out:
                        verify_outcome, verify_reason = "environment", f"verification timeout: {cmd}"; break
                    if vs.returncode != 0:
                        verify_outcome, verify_reason = "rejected", f"verification failed: {cmd}"; break
                # ALWAYS recheck the tree after verification ran, whatever its exit — a test that
                # times out may still have executed worker code that wrote a forbidden path.
                changed = worktree.changed_paths(wt, base)
                violations = worktree.check_allowlist(changed, task, self.cfg.protected_paths, self.cfg.test_path_globs)
                if violations:
                    outcome, reason = "rejected", "verification introduced forbidden change: " + "; ".join(violations)
                else:
                    outcome, reason = verify_outcome, verify_reason
        if stage.terminated:
            self._rewrite_artifact(adir / "attempt.json", json.dumps({"status": "completed", "pgid": stage.pgid,
                "started_utc": started, "base_tree": base, "agent": rung.agent, "rung": dataclasses.asdict(rung),
                "verify_pgids": verify_pgids}, sort_keys=True))
        restore = outcome in ("rejected", "protocol", "blocked") or (outcome == "timeout" and violations)
        diff = subprocess.run(["git", "-C", str(wt), "diff", base, worktree.snapshot(wt)], capture_output=True, text=True).stdout
        self._write_artifact(adir / "diff.patch", diff)
        if restore:
            worktree.restore(wt, base)
        row = {"run_id": self.run_id, "ticket": t.key, "task_id": task.id, "stage": "implement", "agent": rung.agent,
               "model": agent.model, "tier": rung.tier, "arm": arm, "attempt": n, "start_utc": started, "end_utc": _now(),
               "worker_seconds": round(stage.elapsed_s, 2), "verify_seconds": round(verification_seconds, 2), "outcome": outcome,
               "reason": reason, "evidence_path": str(adir), "session_dir": str(adir / "session"), "changed_paths": changed,
               "terminated": stage.terminated, "published": False}
        self._write_artifact(adir / "row.json", json.dumps(row, sort_keys=True))
        return outcome, reason, row

    def _save(self, tdir, t): state.save(tdir, t)

    def status(self) -> int:
        tickets_dir = self.cfg.state_root / "tickets"; heavy = locks.owner(self.cfg.state_root / "locks" / "heavy")
        print(f"epic {self.cfg.epic}  paused={state.paused(self.cfg.state_root)}  heavy_lane={'held by ' + str(heavy['pid']) if heavy else 'free'}")
        for key in self.cfg.tickets:
            t = state.load(tickets_dir / key); human = " HUMAN" if state.human_owned(tickets_dir / key) else ""
            print(f"  {key:<10} {t.state:<14} {t.reason}{human}")
        return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="agent-loop"); ap.add_argument("--config", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True); sub.add_parser("status"); sub.add_parser("run-once")
    cf = sub.add_parser("clear-fence"); cf.add_argument("--force", action="store_true")
    d = sub.add_parser("dry-run"); d.add_argument("--worktree", required=True); d.add_argument("--tasks", required=True)
    d.add_argument("--scenario", default="pass"); d.add_argument("--ticket", default="DRY-1")
    a = ap.parse_args(argv); cfg = config.load(a.config); cfg.ensure_dirs()
    if a.cmd == "dry-run":
        def fake_launcher(pargv, cwd, timeout_s, env, out, err, on_start=None):
            fake_env = {**(env or {}), "AL_SCENARIO": a.scenario}
            fake_argv = [sys.executable, str(pathlib.Path(__file__).with_name("fake_worker.py")), *pargv[2:]]
            return procs.run_stage(fake_argv, cwd, timeout_s, fake_env, out, err, on_start=on_start)
        r = Runner(cfg, pi_launcher=fake_launcher)
    else: r = Runner(cfg)
    if a.cmd == "status": return r.status()
    if a.cmd == "clear-fence":
        fence_path = cfg.state_root / "locks" / "heavy.fence"
        if not fence_path.exists():
            print("no fence present", file=sys.stderr); return 0
        data = json.loads(fence_path.read_text())
        print(json.dumps(data, sort_keys=True))
        gstate = procs.group_state(int(data["pgid"]))
        print(f"group_state={gstate}", file=sys.stderr)
        if gstate == "dead" or a.force:
            # Before unlinking the fence, mark the referenced attempt as orphaned so re-entry
            # doesn't re-fence forever. Find the attempt.json using ticket/task/attempt fields.
            try:
                ap = cfg.state_root / "attempts" / data["ticket"] / data["task"] / data["attempt"] / "attempt.json"
                if ap.exists():
                    obj = json.loads(ap.read_text())
                    if obj.get("status") in {"launching", "running"}:
                        obj["status"] = "orphaned"
                        obj["orphaned_by"] = "clear-fence"
                        Runner._rewrite_artifact(ap, json.dumps(obj, sort_keys=True))
            except (OSError, json.JSONDecodeError, KeyError):
                pass  # fence data corrupt or attempt.json missing; clearing fence anyway
            fence_path.unlink()
            print("fence cleared", file=sys.stderr); return 0
        print(f"refusing to clear fence: group state is {gstate} (use --force to override)", file=sys.stderr)
        return 1
    runner_lease = locks.Lease(cfg.state_root / "locks" / "runner", "runner")
    if not runner_lease.acquire(): print("another runner instance is live; exiting", file=sys.stderr); return 3
    try:
        if a.cmd == "run-once": print("run-once: real ticket execution lands in Plan 3; nothing to do", file=sys.stderr); return 0
        tasks = contracts.load_tasks(a.tasks); t = state.load(cfg.ticket_dir(a.ticket)); t.worktree = a.worktree
        if t.state == "queued":
            for s in ("spinup", "plan", "plan-review", "implement"): t = state.transition(t, s)
            state.save(cfg.ticket_dir(a.ticket), t)
        seen = {False: 0, True: 0}
        for task in tasks:
            t = state.load(cfg.ticket_dir(a.ticket)); t.worktree = a.worktree
            index = seen[task.visual]; seen[task.visual] += 1
            out = r.implement_task(t, task, index, a.worktree); print(f"{a.ticket} task {task.id}: {out}")
            if out != "accepted": return 1
        return 0
    finally: runner_lease.release()


if __name__ == "__main__": sys.exit(main())
