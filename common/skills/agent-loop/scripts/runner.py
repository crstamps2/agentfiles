"""Plan 1c deterministic implementation runner: `_attempt` drives the attempt state machine
in attempt.py; `implement_task` requires a `reconcile.RunContext` obtained from the global
`reconcile.reconcile()` recovery entry point. There is no runner-owned recovery any more --
that lives entirely in reconcile.py (Plan 1c Tasks 4/5)."""
from __future__ import annotations
import argparse
import datetime as dt
import json
import os
import pathlib
import sys
import uuid

import admission
import agentdef
import attempt
import config
import contracts
import ladder
import locks
import procid
import procs
import reconcile
import state
import worktree


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


class Runner:
    def __init__(self, cfg: config.Config, run_id: str | None = None, pi_launcher=None, admission_override: admission.Decision | None = None):
        self.cfg = cfg
        self.run_id = run_id or uuid.uuid4().hex[:8]
        self.pi_launcher = pi_launcher
        self.admission_override = admission_override

    @property
    def launch(self):
        return self.pi_launcher or procs.run_stage

    def _stop(self, tdir, t):
        if state.paused(self.cfg.state_root):
            if t.state != "paused": state.save(tdir, state.transition(t, "paused", reason="PAUSE file present"))
            return "paused"
        if state.human_owned(tdir):
            if t.state != "paused": state.save(tdir, state.transition(t, "paused", reason="HUMAN file present"))
            return "paused"
        return None

    def implement_task(self, ctx, t: state.Ticket, task: contracts.Task, stratum_index: int, wt) -> str:
        if not isinstance(ctx, reconcile.RunContext):
            raise TypeError("implement_task requires a RunContext produced by reconcile.reconcile()")
        wt, tdir = pathlib.Path(wt), self.cfg.ticket_dir(t.key)
        if t.state == "blocked":
            return "blocked"
        stop = self._stop(tdir, t)
        if t.state == "paused":
            if stop: return "paused"
            if t.previous != "implement":
                raise state.IllegalTransition(f"{t.key}: paused from {t.previous}, not implement")
            t = state.transition(t, "implement"); state.save(tdir, t)
        elif t.state != "implement":
            raise state.IllegalTransition(f"{t.key}: cannot implement from {t.state}")

        lineage = f"{t.key}/{task.id}"
        spec = self.cfg.tickets.get(t.key)

        while True:
            t = state.load(tdir)
            if t.state == "blocked":
                return "blocked"
            if t.state == "paused":
                return "paused"

            history = t.attempts.get(lineage, [])
            arm = history[0].get("arm") if history else None
            if arm is None:
                arm = ladder.assign_arm(stratum_index, task.visual, spec.pin_arm if spec else None, self.cfg.arms_alternate)
            attempts = [ladder.Attempt(ladder.Rung(**e["rung"]), e["outcome"])
                        for e in history if e.get("outcome") in ladder.OUTCOMES]
            env_failures = 0
            for e in reversed(history):
                if e.get("outcome") == "environment": env_failures += 1
                else: break

            rung = ladder.next_rung(attempts, arm)
            if rung is None:
                if attempts and attempts[-1].outcome == "accepted": return "accepted"
                t = state.transition(t, "blocked", reason=f"task {task.id}: ladder exhausted")
                state.save(tdir, t); return "blocked"

            stop = self._stop(tdir, t)
            if stop: return stop
            reading = admission.probe(self.cfg.state_root)
            decision = self.admission_override or admission.decide(reading, self.cfg.admission)
            if not decision.ok:
                t = state.transition(t, "paused", reason="resource: " + "; ".join(decision.reasons))
                state.save(tdir, t); return "paused"
            lease = locks.Lease(self.cfg.state_root / "locks" / "heavy", f"implement {t.key}/{task.id}")
            if not lease.acquire():
                t = state.transition(t, "paused", reason="heavy lane held by a live owner")
                state.save(tdir, t); return "paused"
            task_root = self.cfg.state_root / "attempts" / t.key / task.id
            try:
                n = attempt.next_n(task_root)
                outcome, reason = self._attempt(t, task, wt, arm, rung, n, attempts, env_failures)
            finally:
                lease.release()

            if outcome in ("rejected", "protocol"):
                ladder.append_feedback(task_root, n, reason)
            if outcome == "accepted":
                return "accepted"
            # For every other outcome, loop back to the top: re-load the ticket (projections
            # may have already blocked/paused it) and recompute the ladder from persisted
            # history. A "blocked"/"paused" ticket state is caught at the top of the loop.

    def _attempt(self, t, task, wt, arm, rung, n, attempts, env_failures):
        agent = agentdef.load(self.cfg.pi_agents_dir, rung.agent)
        agentdef.assert_worker_safe(agent)
        try:
            rec = attempt.create(self.cfg, t.key, task, wt, n, agent, arm, rung, self.run_id)
        except attempt.UnsafePath as e:
            print(f"task {task.id}: unsafe task-level feedback.md, refusing to create attempt {n}: {e}",
                  file=sys.stderr)
            return "protocol", f"unsafe feedback.md: {e}"

        timeout = min(float(task.timeout_s), float(self.cfg.heavy_stage_timeout_s))
        env = {**os.environ, "AL_TASK_DIR": str(rec.path), "AL_TICKET": t.key, "AL_TASK": task.id}
        argv = agentdef.pi_argv(agent, rec.path / "prompt.md", rec.path / "session", rec.path / "body.md")

        holder = {"rec": rec}

        def on_start(pgid, pid):
            holder["rec"] = attempt.transition(holder["rec"], "RUNNING", proc=procid.capture(pid).to_dict())

        rec = attempt.transition(rec, "LAUNCHING", stages=[{"kind": "worker", "idx": 0, "proc": None}],
                                 model=agent.model, tier=rung.tier)
        holder["rec"] = rec
        stage = self.launch(argv, wt, timeout, env, rec.path / "stdout.log", rec.path / "stderr.log",
                            on_start=on_start)
        rec = holder["rec"]
        stages = list(rec.stages)
        stages[-1] = {**stages[-1], "proc": rec.proc, "terminated": stage.terminated,
                      "timed_out": stage.timed_out, "rc": stage.returncode, "elapsed_s": stage.elapsed_s}
        rec = attempt.transition(rec, "STAGE_DONE", stages=stages, proc=None)

        if not stage.terminated:
            reconcile.fence(self.cfg, rec, f"termination unverified pgid {stage.pgid}")
            raise AssertionError("unreachable: fence() always raises FenceExit")

        # Peek at the worker's claim and the tree without any state transition yet -- the
        # decision of whether to run verification is made here, before ever entering
        # CLASSIFYING (attempt.py's forward-only table only allows CLASSIFYING once, after
        # every stage -- worker and verify -- has completed).
        changed = worktree.changed_paths(wt, rec.base_tree)
        violations = worktree.check_allowlist(changed, task, self.cfg.protected_paths, self.cfg.test_path_globs)
        res = None
        symlinked_result = False
        try:
            text = attempt.safe_read(rec.path / "result.md")
            res = contracts.parse_result(text)
        except FileNotFoundError:
            res = None
        except contracts.ProtocolError:
            res = None
        except attempt.UnsafePath:
            symlinked_result = True
        try:
            stderr = (rec.path / "stderr.log").read_text(errors="replace")
        except OSError:
            stderr = ""
        if symlinked_result:
            outcome, reason = "protocol", "result.md is not a safe regular file (symlink?)"
        else:
            outcome, reason = _classify(res, stage, violations, stderr)

        verification_seconds = 0.0
        if outcome == "accepted":
            verify_outcome, verify_reason = "accepted", "none"
            for i, cmd in enumerate(task.verification_commands):
                stages = list(rec.stages) + [{"kind": "verify", "idx": i, "proc": None}]
                rec = attempt.transition(rec, "LAUNCHING", stages=stages)
                holder["rec"] = rec

                def on_verify_start(pgid, pid):
                    cur = holder["rec"]
                    proc = procid.capture(pid).to_dict()
                    stgs = list(cur.stages)
                    stgs[-1] = {**stgs[-1], "proc": proc}
                    holder["rec"] = attempt.transition(cur, "RUNNING", proc=proc, stages=stgs)

                try:
                    # Verification commands are real shell commands ("true", "npm test", ...)
                    # and must never be redirected through self.launch -- that hook exists
                    # solely to swap the WORKER's `pi` invocation for a fake in tests/dry-run.
                    vs = procs.run_stage(["/bin/sh", "-c", cmd], wt, timeout, env,
                                         rec.path / f"verify-{i}.out", rec.path / f"verify-{i}.err",
                                         on_start=on_verify_start)
                except procs.LogPathExists as e:
                    # Nothing ever executed (the log path collision is caught before on_start
                    # fires) -- there is no process to journal, but the state machine still
                    # requires LAUNCHING -> RUNNING -> STAGE_DONE, not a direct shortcut.
                    rec = holder["rec"]
                    stages = list(rec.stages)
                    stages[-1] = {**stages[-1], "proc": None, "terminated": True, "timed_out": False,
                                  "rc": None, "elapsed_s": 0.0}
                    rec = attempt.transition(rec, "RUNNING", proc=None)
                    rec = attempt.transition(rec, "STAGE_DONE", stages=stages, proc=None)
                    verify_outcome, verify_reason = "protocol", f"worker pre-created verification artifact: {e}"
                    break
                rec = holder["rec"]
                verification_seconds += vs.elapsed_s
                stages = list(rec.stages)
                stages[-1] = {**stages[-1], "terminated": vs.terminated, "timed_out": vs.timed_out,
                              "rc": vs.returncode, "elapsed_s": vs.elapsed_s}
                rec = attempt.transition(rec, "STAGE_DONE", stages=stages, proc=None)
                if not vs.terminated:
                    reconcile.fence(self.cfg, rec, f"termination unverified pgid {vs.pgid}")
                    raise AssertionError("unreachable: fence() always raises FenceExit")
                if vs.timed_out:
                    verify_outcome, verify_reason = "environment", f"verification timeout: {cmd}"
                    break
                if vs.returncode != 0:
                    verify_outcome, verify_reason = "rejected", f"verification failed: {cmd}"
                    break

            # ALWAYS recheck the tree after verification ran, whatever its exit -- a test that
            # times out may still have executed worker code that wrote a forbidden path.
            final_tree = worktree.snapshot(wt)
            changed = worktree.changed_paths(wt, rec.base_tree)
            violations = worktree.check_allowlist(changed, task, self.cfg.protected_paths, self.cfg.test_path_globs)
            if violations:
                outcome, reason = "rejected", "verification introduced forbidden change: " + "; ".join(violations)
            else:
                outcome, reason = verify_outcome, verify_reason
        else:
            final_tree = worktree.snapshot(wt)

        rec = attempt.transition(rec, "CLASSIFYING", observed_tree=final_tree)
        # Consecutive-environment-failure count INCLUDING this attempt (matching the parent
        # design's "environment pair" pause rule); `env_failures` as threaded in is the
        # trailing count from prior history only.
        this_env_failures = env_failures + 1 if outcome == "environment" else 0
        next_action = ladder.next_action(attempts, arm, outcome, this_env_failures)
        rec = attempt.transition(rec, "CLASSIFIED", outcome=outcome, reason=reason, changed_paths=changed,
                                 violations=violations, next_action=next_action,
                                 verify_seconds=round(verification_seconds, 2), end_utc=_now(),
                                 observed_tree=final_tree)
        rec = reconcile.finalize(self.cfg, rec)
        rec = reconcile.project_all(self.cfg, rec)
        return rec.outcome, rec.reason

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
    d.add_argument("--scenario", default="pass"); d.add_argument("--ticket", default="DRY-1"); d.add_argument("--skip-admission", action="store_true")
    a = ap.parse_args(argv); cfg = config.load(a.config); cfg.ensure_dirs()
    if a.cmd == "dry-run":
        def fake_launcher(pargv, cwd, timeout_s, env, out, err, on_start=None):
            fake_env = {**(env or {}), "AL_SCENARIO": a.scenario}
            fake_argv = [sys.executable, str(pathlib.Path(__file__).with_name("fake_worker.py")), *pargv[2:]]
            return procs.run_stage(fake_argv, cwd, timeout_s, fake_env, out, err, on_start=on_start)
        admission_override = admission.Decision(True, []) if a.skip_admission else None
        r = Runner(cfg, pi_launcher=fake_launcher, admission_override=admission_override)
    else: r = Runner(cfg)
    if a.cmd == "status": return r.status()
    if a.cmd == "clear-fence":
        # NOTE: this reads/writes the pre-Plan-1c fence file shape (pgid/ticket/task/attempt).
        # reconcile.fence() (Plan 1c Task 4) writes a different shape (attempt_dir/proc/reason).
        # Reconciling clear-fence with the new shape is Plan 1c Task 7's job; left as-is here.
        fence_path = cfg.state_root / "locks" / "heavy.fence"
        if not fence_path.exists():
            print("no fence present", file=sys.stderr); return 0
        data = json.loads(fence_path.read_text())
        print(json.dumps(data, sort_keys=True))
        gstate = procs.group_state(int(data["pgid"]))
        print(f"group_state={gstate}", file=sys.stderr)
        if gstate == "dead" or a.force:
            try:
                ap_ = cfg.state_root / "attempts" / data["ticket"] / data["task"] / data["attempt"] / "attempt.json"
                if ap_.exists():
                    obj = json.loads(ap_.read_text())
                    if obj.get("status") in {"launching", "running"}:
                        obj["status"] = "orphaned"
                        obj["orphaned_by"] = "clear-fence"
                        tmp = ap_.with_name(f"{ap_.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
                        tmp.write_text(json.dumps(obj, sort_keys=True)); os.replace(tmp, ap_)
            except (OSError, json.JSONDecodeError, KeyError):
                pass  # fence data corrupt or attempt.json missing; clearing fence anyway
            fence_path.unlink()
            print("fence cleared", file=sys.stderr); return 0
        print(f"refusing to clear fence: group state is {gstate} (use --force to override)", file=sys.stderr)
        return 1
    try:
        ctx = reconcile.reconcile(cfg, r.run_id)
    except reconcile.FenceExit as e:
        print(e.reason, file=sys.stderr); return 3
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
            out = r.implement_task(ctx, t, task, index, a.worktree); print(f"{a.ticket} task {task.id}: {out}")
            if out != "accepted": return 1
        return 0
    except reconcile.FenceExit as e:
        print(e.reason, file=sys.stderr); return 3
    finally: ctx.close()


if __name__ == "__main__": sys.exit(main())
