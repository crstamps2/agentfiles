"""Plan 1c deterministic implementation runner: `_attempt` drives the attempt state machine
in attempt.py; `implement_task` requires a `reconcile.RunContext` obtained from the global
`reconcile.reconcile()` recovery entry point. There is no runner-owned recovery any more --
that lives entirely in reconcile.py (Plan 1c Tasks 4/5)."""
from __future__ import annotations
import argparse
import dataclasses
import datetime as dt
import os
import json
import pathlib
import subprocess
import sys
import uuid

import admission
import agentdef
import attempt
import config
import contracts
import publish
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


def _accepted_source_paths(cfg, ticket_key: str, task_id: str, wt) -> list[str]:
    """Source paths from the most recent ACCEPTED attempt of this task (ignored + harness paths dropped)."""
    root = cfg.state_root / "attempts" / ticket_key / task_id
    recs = []
    for d in sorted((p for p in root.iterdir() if p.name.isdigit()), key=lambda p: int(p.name)):
        try:
            rec = attempt.load(d, validate_worktree=False)
        except Exception:
            continue
        if rec.outcome == "accepted":
            recs.append(rec)
    if not recs:
        return []
    changed = list(recs[-1].changed_paths or [])
    ignored = worktree.ignored_paths(wt, changed)
    globs = getattr(cfg, "harness_artifact_globs", ())
    return [p for p in changed if p not in ignored and not any(worktree._match(p, g) for g in globs)
            and (pathlib.Path(wt) / p).exists()]


def publish_accepted(cfg, ticket_key: str, wt, task) -> None:
    """Commit + push the accepted task's paths and make sure the loop's DRAFT PR exists.
    Failures here pause the ticket (the code is safe on disk; publishing can be retried) and
    never undo an acceptance."""
    tdir = cfg.ticket_dir(ticket_key)
    try:
        branch = publish.guard_branch(wt, ticket_key)
        paths = _accepted_source_paths(cfg, ticket_key, task.id, wt)
        sha = publish.commit_paths(wt, paths, f"{task.summary.strip().rstrip('.')}\n\n{ticket_key} task {task.id} ({task.slug}); "
                                              f"implemented by the agent loop and accepted by its verification gate.")
        pushed = publish.push(wt, branch)
        pr = publish.existing_pr(wt, branch)
        if pr is None:
            spec = cfg.tickets.get(ticket_key)
            summary = getattr(spec, "summary", None) or ticket_key
            pr = publish.ensure_draft_pr(wt, branch, publish.pr_title(ticket_key, summary),
                                         publish.pr_body(ticket_key, summary, task.summary, paths,
                                                         ["There are automated tests (runner verification gate passed)"],
                                                         {"intent": f"Implement {ticket_key} via the agent loop."}))
            publish.record_pr_on_worktree(wt, pr["number"])
        print(f"{ticket_key} task {task.id}: published commit={str(sha)[:10] if sha else 'none'} pushed={pushed} pr=#{pr['number']}", flush=True)
    except publish.PublishError as e:
        t = state.load(tdir)
        if t.state != "paused":
            state.save(tdir, state.transition(t, "paused", reason=f"publish failed: {e}"))
        print(f"{ticket_key} task {task.id}: publish failed; ticket paused: {e}", file=sys.stderr, flush=True)


def _unload_local_model(model: str | None) -> None:
    """Best-effort `ollama stop <model>`; never raises. `model` is `ollama-local/<id>` -> `<id>`."""
    if not model:
        return
    mid = model.split("/", 1)[-1]
    try:
        subprocess.run(["ollama", "stop", mid], capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        pass


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
        if (not isinstance(ctx, reconcile.RunContext) or ctx.cfg is not self.cfg
                or ctx.closed or ctx._token is not reconcile._TOKEN):
            raise TypeError("RunContext must come from reconcile() and be open")
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
            # The arm is sticky for a task once an attempt has produced a REAL outcome. Attempts that
            # never got a fair shot (environment / interrupted) do not pin it, so an operator can
            # re-pin a ticket after an environment defect (ZIP-7873/001: three local attempts died on
            # the 32K window; pin_arm = "cloud" must take effect on the next attempt).
            real = [e for e in history if e.get("outcome") not in (None, "environment", "interrupted")]
            arm = real[0].get("arm") if real else None
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
                outcome, reason, rec = self._attempt(t, task, wt, arm, rung, n, attempts, env_failures)
            except reconcile.FinalizeFailed as e:
                # The attempt is durably CLASSIFIED; finalize (restore/verify/patch) could not
                # complete. Pause the ticket with the cause and stop -- reconcile() retries
                # finalize on the next start. Never let this traceback out of the runner.
                t = state.load(tdir)
                if t.state != "paused":
                    t = state.transition(t, "paused", reason=f"finalize failed: {e}")
                    state.save(tdir, t)
                print(f"{t.key}/{task.id}: finalize failed; ticket paused: {e}", file=sys.stderr, flush=True)
                return "paused"
            finally:
                # Local arm: free the 12 GB before the lane is handed to anything else (gates,
                # the next attempt, or Cody). The spec makes local inference and Rails/Chrome
                # mutually exclusive in time; this is the unload half of that rule.
                if rung.agent == "local-worker" and getattr(self.cfg, "local_unload_after_attempt", True):
                    _unload_local_model(getattr(self.cfg, "local_model", None))
                lease.release()

            if rec is None:
                # No attempt record was ever created (e.g. attempt.create() refused a
                # symlinked task-level feedback.md) -- there is no attempt_id/rung dir to
                # write feedback into, and ladder.append_feedback would just re-read the
                # same booby-trapped feedback.md and raise uncaught. Advance the ladder by
                # hand: a synthetic history entry with no attempt/n, so the next loop
                # iteration's next_rung() sees this rung as consumed.
                print(f"task {task.id}: attempt {n} produced no record ({outcome}: {reason}); "
                      f"advancing the ladder without an attempt directory", file=sys.stderr)
                t = state.load(tdir)
                entries = t.attempts.setdefault(lineage, [])
                entries.append({"n": None, "rung": dataclasses.asdict(rung), "outcome": outcome,
                                "reason": reason, "arm": arm, "attempt_id": None})
                state.save(tdir, t)
                continue

            if outcome in ("rejected", "protocol"):
                # Defect exposed by the crash-window table (row 32, `test_cw_32`): a worker
                # can reach task_root/feedback.md (it lives under cfg.state_root, outside the
                # worktree the worker's shell runs in, but AL_TASK_DIR lets a worker compute
                # its path) and replace it with a symlink before this attempt's feedback is
                # appended. `ladder.append_feedback` already refuses (via safe_read/
                # safe_rewrite's no-follow discipline) to read through or write through it --
                # but until now that UnsafePath propagated uncaught out of implement_task,
                # crashing the whole runner process instead of the design's fail-safe
                # per-attempt handling. Catch it here: the append is skipped (never retried
                # blindly -- a booby-trapped feedback.md stays booby-trapped until an operator
                # clears it), and the loop continues so the ladder still advances on this
                # attempt's already-classified outcome.
                try:
                    ladder.append_feedback(task_root, n, reason)
                except attempt.UnsafePath as e:
                    print(f"task {task.id}: unsafe feedback.md, skipping feedback append for "
                          f"attempt {n}: {e}", file=sys.stderr)
            if outcome == "accepted":
                return "accepted"
            # For every other outcome, loop back to the top: re-load the ticket (projections
            # may have already blocked/paused it) and recompute the ladder from persisted
            # history. A "blocked"/"paused" ticket state is caught at the top of the loop.

    def _attempt(self, t, task, wt, arm, rung, n, attempts, env_failures):
        agent = agentdef.load(self.cfg.pi_agents_dir, rung.agent)
        agentdef.assert_worker_safe(agent)
        # Config contract: the runner asserts at launch that the rendered local-worker agent's
        # model equals [local].model, so there is one validated source for what actually runs.
        # Checked before attempt.create() -- a mismatch must never produce an attempt record.
        if rung.agent == "local-worker" and self.cfg.local_model and agent.model != self.cfg.local_model:
            raise RuntimeError(
                f"local-worker renders {agent.model} but [local].model is {self.cfg.local_model}")
        try:
            rec = attempt.create(self.cfg, t.key, task, wt, n, agent, arm, rung, self.run_id)
            reconcile._maybe_crash("after-created")
        except attempt.UnsafePath as e:
            print(f"task {task.id}: unsafe task-level feedback.md, refusing to create attempt {n}: {e}",
                  file=sys.stderr)
            return "protocol", f"unsafe feedback.md: {e}", None

        timeout = min(float(task.timeout_s), float(self.cfg.heavy_stage_timeout_s))
        env = {**os.environ, "AL_TASK_DIR": str(rec.path), "AL_TICKET": t.key, "AL_TASK": task.id}
        argv = agentdef.pi_argv(agent, rec.path / "prompt.md", rec.path / "session", rec.path / "body.md")

        holder = {"rec": rec}

        def on_start(pgid, pid):
            holder["rec"] = attempt.transition(holder["rec"], "RUNNING", proc=procid.capture(pid).to_dict())
            reconcile._maybe_crash("after-running")

        rec = attempt.transition(rec, "LAUNCHING", stages=[{"kind": "worker", "idx": 0, "proc": None}],
                                 model=agent.model, tier=rung.tier)
        reconcile._maybe_crash("after-launching")
        holder["rec"] = rec
        stage = self.launch(argv, wt, timeout, env, rec.path / "stdout.log", rec.path / "stderr.log",
                            on_start=on_start)
        rec = holder["rec"]
        stages = list(rec.stages)
        stages[-1] = {**stages[-1], "proc": rec.proc, "terminated": stage.terminated,
                      "timed_out": stage.timed_out, "rc": stage.returncode, "elapsed_s": stage.elapsed_s}
        # C1: keep the live group's identity on the record while its termination is unverified,
        # so a crash between this receipt and fence() still lets recovery see the live stage.
        rec = attempt.transition(rec, "STAGE_DONE", stages=stages,
                                 proc=None if stage.terminated else stages[-1]["proc"])
        reconcile._maybe_crash("after-stage-done")

        if not stage.terminated:
            reconcile.fence(self.cfg, rec, f"termination unverified pgid {stage.pgid}",
                            proc=stages[-1]["proc"])
            raise AssertionError("unreachable: fence() always raises FenceExit")

        # Peek at the worker's claim and the tree without any state transition yet -- the
        # decision of whether to run verification is made here, before ever entering
        # CLASSIFYING (attempt.py's forward-only table only allows CLASSIFYING once, after
        # every stage -- worker and verify -- has completed).
        changed = worktree.changed_paths(wt, rec.base_tree)
        violations = worktree.check_allowlist(changed, task, self.cfg.protected_paths, self.cfg.test_path_globs, wt=wt, harness_globs=getattr(self.cfg, 'harness_artifact_globs', ()))
        res = None
        symlinked_result = False
        symlinked_stderr = False
        misplaced_result = None
        evidence_only = False
        try:
            text = attempt.safe_read(rec.path / "result.md")
            res = contracts.parse_result(text)
        except FileNotFoundError:
            # Small models sometimes write the absolute result path as RELATIVE to the worktree
            # (`./Users/cody/...`). First real run: gpt-oss:20b did exactly this. Detect it so the
            # feedback names the real mistake, and treat the stray write as an allowlist
            # violation (it is one: an untracked dir inside the repo).
            rel = pathlib.Path(*rec.path.parts[1:])          # strip the leading "/"
            stray = wt / rel / "result.md"
            try:
                if stray.exists() and not stray.is_symlink():
                    misplaced_result = str(rel / "result.md")
            except OSError:
                pass
            res = None
        except contracts.ProtocolError:
            res = None
        except attempt.UnsafePath:
            symlinked_result = True
        try:
            stderr = attempt.safe_read(rec.path / "stderr.log")
        except FileNotFoundError:
            stderr = ""
        except (OSError, UnicodeDecodeError):
            stderr = ""
        except attempt.UnsafePath:
            symlinked_stderr = True
            stderr = ""
        if symlinked_result:
            outcome, reason = "protocol", "result.md is not a safe regular file (symlink?)"
        elif symlinked_stderr:
            outcome, reason = "protocol", "worker replaced stderr.log"
        else:
            outcome, reason = _classify(res, stage, violations, stderr)
            # Evidence-based classification. The worker's result.md was only ever a claim the
            # runner verifies; when the claim is MISSING but the evidence is all there -- the
            # tree changed inside the allowlist, no violations, the stage exited 0 -- run the
            # verification gate and let IT decide. First overnight run: the local model wrote
            # working code 3/3 times and a parseable result.md 0/3 times; rejecting on the
            # missing file paid the premium arm for work the cheap arm had already done.
            result_missing = not (rec.path / "result.md").exists()   # missing != malformed
            if (outcome == "protocol" and res is None and result_missing and not misplaced_result
                    and not violations and changed and stage.returncode == 0 and not stage.timed_out):
                ignored = worktree.ignored_paths(wt, changed)
                source_changes = [p for p in changed if p not in ignored
                                  and not any(worktree._match(p, g) for g in getattr(self.cfg, "harness_artifact_globs", ()))]
                if source_changes:
                    outcome, reason, evidence_only = "accepted", "none", True
            if misplaced_result and outcome in ("rejected", "protocol"):
                reason = (f"result.md was written to the WRONG place: `{misplaced_result}` inside the worktree. "
                          f"Write it to the absolute path `{rec.path / 'result.md'}` (no leading `./`). "
                          f"Other findings: {reason}")

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
                rec = attempt.transition(rec, "STAGE_DONE", stages=stages,
                                         proc=None if vs.terminated else stages[-1]["proc"])
                if not vs.terminated:
                    reconcile.fence(self.cfg, rec, f"termination unverified pgid {vs.pgid}",
                                    proc=stages[-1]["proc"])
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
            violations = worktree.check_allowlist(changed, task, self.cfg.protected_paths, self.cfg.test_path_globs, wt=wt, harness_globs=getattr(self.cfg, 'harness_artifact_globs', ()))
            if violations:
                outcome, reason = "rejected", "verification introduced forbidden change: " + "; ".join(violations)
            else:
                outcome, reason = verify_outcome, verify_reason
                if evidence_only and outcome == "accepted":
                    reason = "accepted from evidence: worker omitted result.md; allowlist clean; verification passed"
        else:
            final_tree = worktree.snapshot(wt)

        rec = attempt.transition(rec, "CLASSIFYING", observed_tree=final_tree)
        reconcile._maybe_crash("after-classifying")
        # Consecutive-environment-failure count INCLUDING this attempt (matching the parent
        # design's "environment pair" pause rule); `env_failures` as threaded in is the
        # trailing count from prior history only.
        this_env_failures = env_failures + 1 if outcome == "environment" else 0
        next_action = ladder.next_action(attempts, arm, outcome, this_env_failures)
        rec = attempt.transition(rec, "CLASSIFIED", outcome=outcome, reason=reason, changed_paths=changed,
                                 violations=violations, next_action=next_action,
                                 verify_seconds=round(verification_seconds, 2), end_utc=_now(),
                                 observed_tree=final_tree)
        reconcile._maybe_crash("after-classified")
        rec = reconcile.finalize(self.cfg, rec)
        rec = reconcile.project_all(self.cfg, rec)
        return rec.outcome, rec.reason, rec

    def status(self) -> int:
        tickets_dir = self.cfg.state_root / "tickets"; heavy = locks.owner(self.cfg.state_root / "locks" / "heavy")
        print(f"epic {self.cfg.epic}  paused={state.paused(self.cfg.state_root)}  heavy_lane={'held by ' + str(heavy['pid']) if heavy else 'free'}")
        for key in self.cfg.tickets:
            t = state.load(tickets_dir / key); human = " HUMAN" if state.human_owned(tickets_dir / key) else ""
            print(f"  {key:<10} {t.state:<14} {t.reason}{human}")
        return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="agent-loop"); ap.add_argument("--config", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True); sub.add_parser("status")
    ro = sub.add_parser("run-once", help="advance one ticket through plan -> implement -> gates -> draft PR -> CI -> ready -> bot review -> Human Gate 1")
    ro.add_argument("--ticket", required=True); ro.add_argument("--worktree", required=True)
    ro.add_argument("--max-steps", type=int, default=12); ro.add_argument("--wait-on-resource", type=int, default=0, metavar="MINUTES")
    cf = sub.add_parser("clear-fence"); cf.add_argument("--force", action="store_true")
    pl = sub.add_parser("plan", help="flagship author writes planning/<key>/tasks.toml; opposite-vendor critic reviews")
    pl.add_argument("--ticket", required=True); pl.add_argument("--worktree", required=True)
    d = sub.add_parser("dry-run"); d.add_argument("--worktree", required=True); d.add_argument("--tasks", required=True)
    d.add_argument("--scenario", default="pass")
    d.add_argument("--real", action="store_true", help="launch the real `pi` worker instead of fake_worker.py (supervised first runs; run-once is Plan 3)"); d.add_argument("--ticket", default="DRY-1"); d.add_argument("--skip-admission", action="store_true")
    d.add_argument("--no-publish", action="store_true", help="do not commit/push/open a draft PR after acceptance")
    d.add_argument("--wait-on-resource", type=int, default=0, metavar="MINUTES",
                   help="overnight mode: when a task pauses for a RESOURCE reason (admission red / heavy lane held), "
                        "sleep 2 min and retry for up to MINUTES instead of exiting. PAUSE/HUMAN still exit.")
    a = ap.parse_args(argv); cfg = config.load(a.config); cfg.ensure_dirs()
    if a.cmd == "status":
        return Runner(cfg).status()
    if a.cmd == "clear-fence":
        return reconcile.clear_fence(cfg, a.force)
    if a.cmd == "plan":
        import plan as plan_mod
        keys = list(cfg.tickets)
        idx = keys.index(a.ticket) + 1 if a.ticket in keys else 1
        stage_root = cfg.state_root / "plans" / a.ticket / _now().replace(":", "").replace("-", "")[:15]
        log = plan_mod.plan_ticket(cfg, a.ticket, a.worktree, idx, stage_root)
        (stage_root / "plan-log.json").write_text(json.dumps(log, indent=2))
        print(json.dumps(log, indent=2))
        return 0 if log.get("result") == "approved" else 1
    if a.cmd == "dry-run":
        def fake_launcher(pargv, cwd, timeout_s, env, out, err, on_start=None):
            fake_env = {**(env or {}), "AL_SCENARIO": a.scenario}
            fake_argv = [sys.executable, str(pathlib.Path(__file__).with_name("fake_worker.py")), *pargv[2:]]
            return procs.run_stage(fake_argv, cwd, timeout_s, fake_env, out, err, on_start=on_start)
        admission_override = admission.Decision(True, []) if a.skip_admission else None
        launcher = None if a.real else fake_launcher          # --real: procs.run_stage with the real pi argv
        if a.real:
            print("dry-run --real: launching the REAL pi worker (cheap arm models may bill); admission enforced" +
                  (" (SKIPPED)" if a.skip_admission else ""), file=sys.stderr)
        r = Runner(cfg, pi_launcher=launcher, admission_override=admission_override)
    else: r = Runner(cfg)
    # Every subcommand except `status` and `clear-fence` (handled above) reconciles first,
    # under the global runner lease, before any ticket selection or dispatch.
    try:
        ctx = reconcile.reconcile(cfg, r.run_id)
    except reconcile.FenceExit as e:
        print(f"fenced: {e.reason}", file=sys.stderr); return 3
    try:
        if a.cmd == "run-once":
            import lifecycle
            import time as _time
            keys = list(cfg.tickets); idx = keys.index(a.ticket) + 1 if a.ticket in keys else 1
            deadline = _time.monotonic() + 60 * a.wait_on_resource
            while True:
                steps = lifecycle.run_once(cfg, r, ctx, a.ticket, a.worktree, hopper_index=idx, max_steps=a.max_steps)
                last = steps[-1] if steps else None
                t = state.load(cfg.ticket_dir(a.ticket))
                transient = (t.state == "paused" and (t.reason.startswith("resource:") or t.reason.startswith("heavy lane"))) \
                            or (last is not None and last.stage == "implement" and t.state == "implement" and last.wait
                                and ("resource:" in (last.detail or "") or "heavy lane" in (last.detail or "")))
                external_wait = last is not None and last.wait and last.action in ("CI running", "CI rerun requested",
                                                                                    "checks (incl. claude-review) still running", "no bot comments yet")
                if (transient or external_wait) and _time.monotonic() < deadline:
                    for _ in range(18):                       # 3 min in 10 s slices; PAUSE exits
                        if state.paused(cfg.state_root): print("PAUSE appeared; exiting", file=sys.stderr); return 1
                        _time.sleep(10)
                    continue
                return 0 if t.state in ("human-gate-1", "done") else 1
        tasks = contracts.load_tasks(a.tasks); t = state.load(cfg.ticket_dir(a.ticket)); t.worktree = a.worktree
        if t.state == "queued":
            for s in ("spinup", "plan", "plan-review", "implement"): t = state.transition(t, s)
            state.save(cfg.ticket_dir(a.ticket), t)
        seen = {False: 0, True: 0}
        import time as _time
        for task in tasks:
            index = seen[task.visual]; seen[task.visual] += 1
            deadline = _time.monotonic() + 60 * a.wait_on_resource
            while True:
                t = state.load(cfg.ticket_dir(a.ticket)); t.worktree = a.worktree
                out = r.implement_task(ctx, t, task, index, a.worktree)
                print(f"{a.ticket} task {task.id}: {out}" + (f" ({state.load(cfg.ticket_dir(a.ticket)).reason})" if out == "paused" else ""), flush=True)
                if out == "accepted":
                    if not a.no_publish:
                        publish_accepted(cfg, a.ticket, a.worktree, task)
                    break
                t = state.load(cfg.ticket_dir(a.ticket))
                transient = out == "paused" and (t.reason.startswith("resource:") or t.reason.startswith("heavy lane"))
                if transient and _time.monotonic() < deadline:
                    print(f"  waiting 120s for resources ({int((deadline - _time.monotonic()) // 60)} min left)", file=sys.stderr, flush=True)
                    for _ in range(12):                       # 12 x 10s: stay responsive to PAUSE and signals
                        if state.paused(cfg.state_root):
                            print("  PAUSE appeared while waiting; exiting", file=sys.stderr, flush=True); return 1
                        _time.sleep(10)
                    continue
                return 1
        return 0
    except reconcile.FenceExit as e:
        print(f"fenced: {e.reason}", file=sys.stderr); return 3
    finally: ctx.close()


if __name__ == "__main__": sys.exit(main())
