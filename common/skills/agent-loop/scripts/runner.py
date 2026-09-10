"""Agent-loop runner. Plan 1 scope: the `implement` stage against the worker ladder, plus
`status` and `dry-run` CLIs. Plans 2 and 3 add ledger enrichment, gates, PR, and review loops."""
from __future__ import annotations
import argparse
import dataclasses
import datetime as dt
import json
import os
import pathlib
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
    def bl(items):
        return "\n".join(f"- {i}" for i in items) or "- (none)"
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


def _classify(res: contracts.Result | None, stage: procs.StageResult, violations: list) -> tuple:
    """Return (outcome, reason). outcome may be "blocked" (not a ladder outcome; the caller
    must not append it to the ladder attempts list, per ladder.OUTCOMES)."""
    if stage.timed_out:
        return "timeout", "stage timeout"
    if res is None:
        return "protocol", "result.md missing or without STATUS"
    if res.status == "blocked":
        return ("blocked", "owner") if res.reason == "owner" else ("protocol", f"blocked/{res.reason or 'unspecified'}")
    if res.reason == "environment":
        return "environment", res.next or "environment"
    if violations:
        return "rejected", "; ".join(violations)
    if res.status == "pass":
        return "accepted", "none"
    return "rejected", f"{res.status}/{res.reason}: {res.next}"


class Runner:
    def __init__(self, cfg: config.Config, run_id: str | None = None, pi_launcher=None):
        self.cfg = cfg
        self.run_id = run_id or uuid.uuid4().hex[:8]
        self.pi_launcher = pi_launcher

    @property
    def launch(self):
        # Resolved at call time so patch("runner.procs.run_stage") in the CLI test takes effect.
        return self.pi_launcher or procs.run_stage

    # ----- implement stage -------------------------------------------------
    def implement_task(self, t: state.Ticket, task: contracts.Task, task_index: int, wt) -> str:
        wt = pathlib.Path(wt)
        tdir = self.cfg.ticket_dir(t.key)
        spec = self.cfg.tickets.get(t.key)
        arm = ladder.assign_arm(task_index, task.visual, spec.pin_arm if spec else None, self.cfg.arms_alternate)
        # Rebuild the ladder attempt history, filtered to outcomes ladder.next_rung accepts;
        # "blocked" records are kept in t.attempts for history but never fed back into the ladder.
        attempts = [ladder.Attempt(ladder.Rung(**a["rung"]), a["outcome"])
                    for a in t.attempts.get(task.id, []) if a["outcome"] in ladder.OUTCOMES]
        # Ruling 4: env_failures counts TRAILING persisted "environment" records so a restart
        # resumes the same consecutive-failure count instead of starting over at 0.
        persisted = t.attempts.get(task.id, [])
        env_failures = 0
        for rec in reversed(persisted):
            if rec["outcome"] == "environment":
                env_failures += 1
            else:
                break

        while True:
            rung = ladder.next_rung(attempts, arm)
            if rung is None:
                if attempts and attempts[-1].outcome == "accepted":
                    return "accepted"
                self._save(tdir, state.transition(t, "blocked", reason=f"task {task.id}: ladder exhausted"))
                return "blocked"

            if state.paused(self.cfg.state_root):
                self._save(tdir, state.transition(t, "paused", reason="PAUSE file present")); return "paused"
            if state.human_owned(tdir):
                self._save(tdir, state.transition(t, "paused", reason="HUMAN file present")); return "paused"
            reading = admission.probe(self.cfg.state_root)
            decision = admission.decide(reading, self.cfg.admission)
            if not decision.ok:
                self._save(tdir, state.transition(t, "paused", reason="resource: " + "; ".join(decision.reasons))); return "paused"

            lease = locks.Lease(self.cfg.state_root / "locks" / "heavy", f"implement {t.key}/{task.id}")
            if not lease.acquire():
                self._save(tdir, state.transition(t, "paused", reason="heavy lane held by a live owner")); return "paused"
            try:
                # Ruling 3: attempt number counts ALL persisted records for this task (including
                # blocked/environment), not just the ladder-eligible in-memory `attempts` list.
                n = len(t.attempts.get(task.id, [])) + 1
                outcome, reason, row = self._attempt(t, task, wt, arm, rung, n)
            finally:
                lease.release()

            # Ruling 3: persist the record and save state BEFORE appending metrics, so a crash
            # between the two leaves the persisted history authoritative and the attempt is not
            # re-executed on restart.
            t.attempts.setdefault(task.id, []).append({"rung": dataclasses.asdict(rung), "outcome": outcome, "reason": reason, "n": n})
            self._save(tdir, t)
            metrics.append(self.cfg.state_root, row)

            # Ruling 4: reset the consecutive-environment counter on any non-environment outcome.
            if outcome == "environment":
                env_failures += 1
            else:
                env_failures = 0

            if outcome == "accepted":
                attempts.append(ladder.Attempt(rung, outcome))
                return "accepted"
            if outcome == "blocked":
                # Ruling E: blocked is not a ladder outcome; record metrics/state and stop, do not
                # append it to `attempts`.
                self._save(tdir, state.transition(t, "blocked", reason=f"task {task.id}: {reason}"))
                return "blocked"

            attempts.append(ladder.Attempt(rung, outcome))

            if outcome == "environment" and env_failures >= 2:
                self._save(tdir, state.transition(t, "paused", reason=f"task {task.id}: repeated environment failure: {reason}")); return "paused"
            if outcome in ("rejected", "protocol"):
                ladder.append_feedback(self._attempt_root(t, task), n, reason)

    def _attempt_root(self, t, task) -> pathlib.Path:
        return self.cfg.state_root / "attempts" / t.key / task.id

    def _attempt(self, t, task, wt, arm, rung, n):
        adir = self._attempt_root(t, task) / str(n)
        adir.mkdir(parents=True, exist_ok=True)
        # feedback.md lives at the task level so every attempt sees the accumulated history
        task_level = self._attempt_root(t, task)
        (adir / "task.toml").write_text(_task_toml(task))
        # Ruling 2: _task_md tells the worker to write result.md (and read feedback.md) at the
        # ATTEMPT dir (adir), matching where the runner actually parses result.md below.
        (adir / "task.md").write_text(_task_md(task, adir, wt))
        if (task_level / "feedback.md").exists():
            (adir / "feedback.md").write_text((task_level / "feedback.md").read_text())
        agent = agentdef.load(self.cfg.pi_agents_dir, rung.agent)
        agentdef.assert_worker_safe(agent)
        (adir / "body.md").write_text(agent.body)
        prompt = adir / "prompt.md"
        prompt.write_text(f"Your task file is {adir / 'task.md'}. Read it, then begin. Write result.md to {adir}.\n")
        base = worktree.snapshot(wt)
        (adir / "base_tree").write_text(base)
        argv = agentdef.pi_argv(agent, prompt, adir / "session", adir / "body.md")
        env = {**os.environ, "AL_TASK_DIR": str(adir), "AL_TICKET": t.key, "AL_TASK": task.id}
        # Ruling A: hopper.toml's heavy_stage_timeout_s is an operator cap; a task may only shorten it.
        timeout = min(float(task.timeout_s), float(self.cfg.heavy_stage_timeout_s))
        start = _now()
        stage = self.launch(argv, wt, timeout, env, adir / "stdout.log", adir / "stderr.log")
        end = _now()

        res = None
        try:
            res = contracts.parse_result((adir / "result.md").read_text())
        except (FileNotFoundError, contracts.ProtocolError):
            res = None
        changed = worktree.changed_paths(wt, base)
        violations = worktree.check_allowlist(changed, task, self.cfg.protected_paths, self.cfg.test_path_globs)
        outcome, reason = _classify(res, stage, violations)

        # Ruling 1: a clean allowlist + STATUS: pass is not accepted on the worker's word alone;
        # the runner still runs task.verification_commands, in the worktree, while still holding
        # the heavy lease, before the task is truly "accepted".
        if outcome == "accepted":
            for i, cmd in enumerate(task.verification_commands):
                vres = procs.run_stage(["/bin/sh", "-c", cmd], wt, timeout, env,
                                        adir / f"verify-{i}.out", adir / f"verify-{i}.err")
                if vres.timed_out or vres.returncode != 0:
                    outcome, reason = "rejected", f"verification failed: {cmd}"
                    break

        # Preserve the diff for every attempt, then decide whether the tree keeps it.
        diff = subprocess.run(["git", "-C", str(wt), "diff", base, worktree.snapshot(wt)], capture_output=True, text=True).stdout
        (adir / "diff.patch").write_text(diff)
        # Ruling 5: a clean "environment" outcome keeps legitimate partial edits; only
        # rejected/protocol/blocked, and timeout-with-violations, discard the tree.
        if outcome in ("rejected", "protocol", "blocked") or (outcome == "timeout" and violations):
            worktree.restore(wt, base)

        row = {"run_id": self.run_id, "ticket": t.key, "task_id": task.id, "stage": "implement",
               "agent": rung.agent, "model": agent.model, "tier": rung.tier, "arm": arm, "attempt": n,
               "start_utc": start, "end_utc": end, "worker_seconds": round(stage.elapsed_s, 2),
               "outcome": outcome, "reason": reason, "evidence_path": str(adir), "session_dir": str(adir / "session"),
               "changed_paths": changed, "terminated": stage.terminated}
        return outcome, reason, row

    def _save(self, tdir, t):
        state.save(tdir, t)

    # ----- CLIs --------------------------------------------------------------
    def status(self) -> int:
        tickets_dir = self.cfg.state_root / "tickets"
        heavy = locks.owner(self.cfg.state_root / "locks" / "heavy")
        print(f"epic {self.cfg.epic}  paused={state.paused(self.cfg.state_root)}  heavy_lane={'held by ' + str(heavy['pid']) if heavy else 'free'}")
        for key in self.cfg.tickets:
            t = state.load(tickets_dir / key)
            human = " HUMAN" if state.human_owned(tickets_dir / key) else ""
            print(f"  {key:<10} {t.state:<14} {t.reason}{human}")
        return 0


def _task_toml(task: contracts.Task) -> str:
    def arr(xs):
        return "[" + ", ".join(json.dumps(x) for x in xs) + "]"
    return (f'id = {json.dumps(task.id)}\nslug = {json.dumps(task.slug)}\nsummary = {json.dumps(task.summary)}\n'
            f'allowed_files = {arr(task.allowed_files)}\nverification_commands = {arr(task.verification_commands)}\n'
            f'acceptance = {arr(task.acceptance)}\nmay_edit_tests = {str(task.may_edit_tests).lower()}\n'
            f'visual = {str(task.visual).lower()}\ntimeout_s = {task.timeout_s}\n')


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="agent-loop")
    ap.add_argument("--config", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("run-once")
    d = sub.add_parser("dry-run")
    d.add_argument("--worktree", required=True); d.add_argument("--tasks", required=True)
    d.add_argument("--scenario", default="pass"); d.add_argument("--ticket", default="DRY-1")
    a = ap.parse_args(argv)
    cfg = config.load(a.config); cfg.ensure_dirs()
    r = Runner(cfg)
    if a.cmd == "status":
        return r.status()
    # Global runner lease: two launchd ticks (or a tick plus a manual run) must never overlap.
    runner_lease = locks.Lease(cfg.state_root / "locks" / "runner", "runner")
    if not runner_lease.acquire():
        print("another runner instance is live; exiting", file=sys.stderr); return 3
    try:
        if a.cmd == "run-once":
            print("run-once: real ticket execution lands in Plan 3; nothing to do", file=sys.stderr); return 0
        os.environ["AL_SCENARIO"] = a.scenario
        tasks = contracts.load_tasks(a.tasks)
        t = state.load(cfg.ticket_dir(a.ticket)); t.worktree = a.worktree
        if t.state == "queued":
            for s in ("spinup", "plan", "plan-review", "implement"):
                t = state.transition(t, s)
            state.save(cfg.ticket_dir(a.ticket), t)
        for i, task in enumerate(tasks):
            out = r.implement_task(t, task, i, a.worktree)
            print(f"{a.ticket} task {task.id}: {out}")
            if out != "accepted":
                return 1
        return 0
    finally:
        runner_lease.release()


if __name__ == "__main__":
    sys.exit(main())
