"""run-once: advance ONE ticket by one or more lifecycle steps, persisting state after each.

    plan -> plan-review -> implement -> gates -> draft-pr -> ready -> bot-loop -> human-gate-1

Each `step()` call looks at the persisted ticket state, does the single next thing, records the
result, and returns what it did. `run_once()` loops `step()` until the ticket is waiting on
something external (CI running, bot review pending, Human Gate 1, paused, blocked) or the
budget/admission says stop. It is safe to call again at any time: every stage is idempotent
against the remote (existing PR, pushed SHA, adjudicated comment ids).

Only the runner acts. Models are consulted for the plan, its review, the implementation
attempts, and the adjudication of review comments; every decision that matters (accept an
attempt, mark ready, reply, escalate) is taken here from recorded evidence.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
import subprocess
import sys
import time
import tomllib
import types

import botreview
import ci
import cohort
import contracts
import decision
import guards
import ledger
import plan as plan_mod
import publish
import screenshots
import state

MAX_BOT_ROUNDS = 3
BOT_WAIT_S = 45 * 60          # bot review normally comments within minutes of ready_for_review


class Step:
    def __init__(self, ticket: str, stage: str, action: str, detail: str = "", wait: bool = False):
        self.ticket, self.stage, self.action, self.detail, self.wait = ticket, stage, action, detail, wait

    def __repr__(self):
        return f"{self.ticket} [{self.stage}] {self.action}" + (f": {self.detail}" if self.detail else "") + (" (waiting)" if self.wait else "")


def _save(cfg, t):
    state.save(cfg.ticket_dir(t.key), t); return t


def _pause(cfg, t, reason):
    if t.state != "paused":
        t = state.transition(t, "paused", reason=reason)
    return _save(cfg, t)


OWNER_QUESTION = re.compile(r"need(?:s)? Cody|owner question|question for the owner|tier-[34]", re.I)
RUNNER_TASK_BLOCK = re.compile(r"^task (\d{3}): (?:ladder exhausted|accepted history has no verified slug)$")
GENERIC_BLOCK = re.compile(r"^(?:owner|blocked|unstructured owner block)?$", re.I)


def _decision_or_recovery(cfg, t, wt, *, source="lifecycle") -> Step:
    """Only a complete staged ambiguity may wait on Cody; everything else is repair work."""
    import heal
    import recovery
    import repair
    tdir = cfg.ticket_dir(t.key)
    candidate = tdir / "decision-candidate.json"
    if candidate.exists():
        try:
            payload = json.loads(candidate.read_text())
            decided = decision.propose(
                cfg,
                t.key,
                payload,
                notify=lambda title, body: _notify_once(tdir, t.key, f"{t.key}: {title.lower()}", body),
            )
        except (OSError, json.JSONDecodeError, decision.DecisionError) as exc:
            t.reason = f"invalid decision candidate: {exc}; {t.reason}"[:500]
            state.save(tdir, t)
        else:
            candidate.rename(candidate.with_name(f"decision-candidate.accepted-{decided.decision_request['id']}.json"))
            return Step(t.key, decided.state, "product decision required", detail=decided.decision_request["question"], wait=True)
    current = state.load(tdir)
    if OWNER_QUESTION.search(current.reason or ""):
        # A genuine question for Cody: keep the slot and ask him. It is not a runner defect, and its text
        # (often a third-party PR comment) must never become repair-agent evidence.
        waited = _waiting_since(tdir, current)
        _notify_once(tdir, current.key, f"{current.key} needs Cody{waited}", current.reason[:160])
        if current.needs_operator != "owner question":
            current.needs_operator = "owner question"; state.save(tdir, current)
        return Step(current.key, current.state, "needs Cody: owner question", detail=current.reason, wait=True)
    active = current.recovery.get("active") if isinstance(current.recovery, dict) else None
    if active and recovery.orphaned(cfg, active):
        current.recovery["active"] = None
        state.save(tdir, current)
        active = None
    if active:
        return Step(t.key, current.state, "waiting for automatic recovery", detail=current.reason, wait=True)
    target = current.previous if current.previous in state.RESUMABLE_STATES else "implement"
    action = {"kind": "replan", "state": "plan"} if target in ("plan", "plan-review") else {"kind": "resume-stage", "state": target}
    checkpoint = {"state": target, "head_sha": _head(wt) if pathlib.Path(wt).exists() else "unknown"}
    summary = current.reason or "unstructured owner block"
    evidence = f"state={current.state} previous={current.previous} reason={summary}"
    lost = _lost_task(cfg, current, wt) if target == "implement" else None
    ladder = RUNNER_TASK_BLOCK.match(summary)
    if lost:
        # An accepted task's output is gone from the worktree: the task that needs it fails, the block says only `owner`, and resuming
        # implement dispatches that task over the same tree again (ZIP-4285). Retry the lost task first, as cohort migration does.
        task, missing = lost
        action = {"kind": "retry-task", "state": "implement", "task_id": task.id, "task_slug": task.slug}
        checkpoint = {**checkpoint, "state": "implement", "task_id": task.id, "task_slug": task.slug}
        evidence += f"; task {task.id} was accepted but its output is missing from the worktree: {', '.join(missing)}"
    elif ladder:
        # A runner-written ladder block: resuming implement would re-block at once; reset that task's ladder.
        slug = next((task.slug for task in _manifest_identities(_manifest_path(wt, current.key)) if task.id == ladder.group(1)), None)
        if slug:
            action = {"kind": "retry-task", "state": "implement", "task_id": ladder.group(1), "task_slug": slug}
            checkpoint = {**checkpoint, "state": "implement", "task_id": ladder.group(1), "task_slug": slug}
    elif target == "implement":
        # The attempt that spends the last rung blocks the ticket with ITS OWN reason (reconcile.project_lifecycle); the text
        # matched above is only what implement_task writes when re-entered. Resuming implement re-blocks at once here too.
        spent = _spent_task(cfg, current, wt)
        if spent:
            action = {"kind": "retry-task", "state": "implement", "task_id": spent.id, "task_slug": spent.slug}
            checkpoint = {**checkpoint, "state": "implement", "task_id": spent.id, "task_slug": spent.slug}
    # Generic reasons share text across tickets but not causes; one ticket must not spend another's landings.
    scope = current.key if (ladder or GENERIC_BLOCK.match(summary)) else None
    defect_id = repair.defect_id(source, summary, scope)
    contract = recovery.new_contract(current.key, checkpoint, action, (defect_id,))
    # Record first: a contract activated without its defect could never be woken.
    repair.record_defect(
        cfg,
        current.key,
        source,
        summary,
        evidence,
        recovery_ref={"ticket": current.key, "recovery_id": contract["id"],
                      "contract_fingerprint": recovery.fingerprint(contract)},
        scope=scope,
    )
    recovery.activate(cfg, current.key, contract)
    return Step(current.key, current.state, "waiting for automatic recovery", detail=summary, wait=True)


def _manifest_path(wt, key) -> pathlib.Path:
    return pathlib.Path(wt) / "planning" / key.lower() / "tasks.toml"


def _ledger_path(cfg, key) -> pathlib.Path:
    return cfg.ticket_dir(key) / "bot-ledger.json"


def _ci_state_path(cfg, key) -> pathlib.Path:
    return cfg.ticket_dir(key) / "ci.json"


def _reclaim_provider_round(cfg, key, cis, rnd, stage) -> None:
    """Keep failed provider logs while making their logical round retryable."""
    for failed in (stage, stage.with_name(stage.name + "-failover")):
        if failed.exists():
            failed.rename(failed.with_name(f"{failed.name}-provider-{time.time_ns()}"))
    cis["bot_rounds"] = max(0, rnd - 1)
    _ci_state_path(cfg, key).write_text(json.dumps(cis))


def _load_json(p: pathlib.Path, default):
    return json.loads(p.read_text()) if p.exists() else default


def _manifest_identities(manifest) -> list:
    """(id, slug) of each task, read without schema validation: the plan stage runs precisely because
    the manifest may be empty or rejected, and re-planning is how it gets replaced."""
    try:
        with open(manifest, "rb") as f:
            rows = tomllib.load(f).get("tasks", [])
    except (OSError, tomllib.TOMLDecodeError):
        return []
    return [types.SimpleNamespace(id=str(r["id"]), slug=str(r["slug"]))
            for r in rows if isinstance(r, dict) and r.get("id") and r.get("slug")]


def _task_accepted(cfg, key, task) -> bool:
    """Accepted iff an accepted attempt has the same runner-recorded slug.

    Task ids are re-used across manifests, so the slug identifies the work. Legacy entries have
    no state slug; only a safely-read, matching task.toml can establish their identity."""
    import attempt
    import ladder
    t = state.load(cfg.ticket_dir(key))
    entries = t.attempts.get(f"{key}/{task.id}", [])
    reset = max((i for i, e in enumerate(entries) if e.get("outcome") == ladder.OPERATOR_RESET), default=-1)
    for e in entries[reset + 1:]:
        if (e.get("outcome") == "accepted"
                and attempt.task_slug_for_entry(cfg, t, task.id, e) == task.slug):
            return True
    return False


class TaskDependencyError(ValueError):
    pass


def _next_task(cfg, key, tasks):
    """First unaccepted task whose explicit prerequisites have accepted matching slugs."""
    done = {task.id for task in tasks if _task_accepted(cfg, key, task)}
    pending = [task for task in tasks if task.id not in done]
    for task in pending:
        if all(dependency in done for dependency in getattr(task, "after", [])):
            return task
    if pending:
        missing = {task.id: [dep for dep in getattr(task, "after", []) if dep not in done] for task in pending}
        raise TaskDependencyError(f"no runnable task; unaccepted or unknown prerequisites: {missing}")
    return None


def _spent_task(cfg, t, wt):
    """First unaccepted task whose ladder has no rung left -- what implement_task re-blocks on -- else None."""
    import attempt
    import ladder
    for task in _manifest_identities(_manifest_path(wt, t.key)):
        if _task_accepted(cfg, t.key, task):
            continue
        history = ladder.since_reset(attempt.history_for_task(cfg, t, task.id, task.slug))
        arm = next((e.get("arm") for e in history if e.get("outcome") not in (None, "environment", "interrupted")), None)
        try:
            attempts = [ladder.Attempt(ladder.Rung(**e["rung"]), e["outcome"]) for e in history if e.get("outcome") in ladder.OUTCOMES]
        except (KeyError, TypeError):
            continue                                                 # unreadable history: not provably spent
        if attempts and ladder.next_rung(attempts, arm) is None:
            return task
    return None


def _lost_task(cfg, t, wt):
    """First accepted task whose output is gone from the worktree, with the paths that went -- else None."""
    import cohort_migration
    accepted = {task_id: (record, adir) for task_id, record, adir in cohort_migration._accepted_tasks(cfg, t)}
    for task in _manifest_identities(_manifest_path(wt, t.key)):
        record, adir = accepted.get(task.id, ({}, None))
        if record.get("slug") == task.slug and _task_accepted(cfg, t.key, task):
            missing = cohort_migration._missing_outputs(types.SimpleNamespace(worktree=str(wt)), record, adir)
            if missing:
                return task, missing
    return None


def step(cfg, runner, ctx, key: str, wt, *, hopper_index: int, pr_number: int | None = None) -> Step:
    tdir = cfg.ticket_dir(key); t = state.load(tdir); t.worktree = str(wt)
    if (tdir / "INTERACTIVE.json").exists():
        return Step(key, t.state, "interactive control; background runner yielding", wait=True)
    if (cfg.state_root / "cohort.json").exists():
        try:
            admitted = key in cohort.members(cfg)
        except cohort.CohortError as exc:
            return Step(key, t.state, "cohort unavailable", detail=str(exc), wait=True)
        if not admitted:
            return Step(key, t.state, "not admitted to cohort", wait=True)
    if wt and pathlib.Path(wt).exists():
        try:
            recovered = recover_interrupted_git_state(wt)
        except subprocess.TimeoutExpired as exc:
            # git did not answer (a thrashing machine: ZIP-8257 and ZIP-8697 crashed the runner here, and every
            # restart hit the same first call). Unknown is not clean -- no stage may read the branch before an
            # interrupted rebase/merge is dealt with -- so leave the ticket alone; the next tick recovers afresh.
            return Step(key, t.state, "git recovery timed out", detail=str(exc), wait=True)
        if recovered:
            print(f"{key}: {recovered}", flush=True)
            (tdir / "git-recovery.jsonl").open("a").write(json.dumps(
                {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ"), "state": t.state, "action": recovered}) + "\n")
    wt = pathlib.Path(wt)

    if state.paused(cfg.state_root):
        return Step(key, t.state, "operator PAUSE", wait=True)
    if (tdir / "HUMAN").exists():
        return Step(key, t.state, "HUMAN takeover file present", wait=True)
    if (tdir / "PARKED").exists():
        return Step(key, t.state, "parked: waiting for a slot", wait=True)
    if isinstance(t.recovery, dict) and t.recovery.get("active"):
        import recovery as _recovery
        if not _recovery.orphaned(cfg, t.recovery["active"]):
            return Step(key, t.state, "waiting for automatic recovery", detail=t.reason, wait=True)
        t.recovery["active"] = None                    # orphaned: fall through to normal classification
        state.save(tdir, t)
    if t.state == "paused" and t.reason.startswith("guard failed on the BASE tree") and (tdir / "bad-guards.md").exists():
        # Self-repair: the runner proved a verification command fails before the task's work exists,
        # so the manifest -- not the worker -- is wrong. The task-writer rewrites/drops the guard;
        # the ticket resumes without a human. (Top cause of lost time 2026-09-14/15.)
        bad = tdir / "bad-guards.md"
        stage = cfg.state_root / "guards" / key / time.strftime("%Y%m%dT%H%M%S")
        prompt = "\n".join([f"# Repair verification guards for {key}\n",
                            f"Bad guards (each FAILS ON THE BASE TREE, so it tests nothing about the task's work): `{bad}`",
                            f"Manifest: `{_manifest_path(wt, key)}`   Plan: `{_manifest_path(wt, key).with_name('plan.md')}`   Worktree: `{wt}`",
                            "For each listed command: rewrite it so it inspects ONLY files the task creates or edits (drop repo-wide checks like",
                            "i18n normalization, generated-file byte equality, or anything unrelated), or remove it. Do not touch other tasks.",
                            "Use ABSOLUTE paths when writing. End with `REVISION: applied <n> blockers` or `REVISION: blocked — <one line>`."])
        plan_mod._run_agent(cfg, plan_mod.WRITER_DEF, prompt, stage, wt, 900)
        viol = plan_mod.validate_manifest(_manifest_path(wt, key))
        for n in range(2, GUARD_FIX_PASSES + 1):
            if not viol:
                break
            # Tell the writer what the schema rejected; without it ZIP-4288 rewrote the same `ruby -e`.
            fix = prompt + ("\n\nYour last edit left the manifest INVALID; the runner rejected it with:\n"
                            + "\n".join(f"- {v}" for v in viol[:6])
                            + "\nRewrite only those commands with the repository's tools (grep, test, bin/rails test, "
                              "rubocop, reek, yarn lint) -- never inline interpreter code such as `ruby -e` or `node -e`.")
            plan_mod._run_agent(cfg, plan_mod.WRITER_DEF, fix, stage.with_name(f"{stage.name}-fix{n}"), wt, 900)
            viol = plan_mod.validate_manifest(_manifest_path(wt, key))
        if viol:
            return _pause_step(cfg, t, "implement", f"guard repair left the manifest invalid: {viol[:2]}")
        bad.rename(bad.with_name(f"bad-guards.repaired-{time.strftime('%Y%m%dT%H%M%S')}.md"))
        t = state.transition(t, t.previous, reason="guards repaired by task-writer"); _save(cfg, t)
        return Step(key, t.state, "bad verification guard repaired; resuming")
    if t.state == "paused":
        # SELF-HEALING. Every pause reason has a recovery policy with a per-reason budget (heal.py);
        # a human is consulted only when the policy says so or the budget is spent.
        import heal
        action, why = heal.decide(tdir, t.reason)
        import repair
        repair.on_heal_verdict(cfg, key, t.reason, action, why)
        if action == "human":
            return _decision_or_recovery(cfg, t, wt, source="heal")
        if action == "fix-task":
            try:
                ft = _lint_fix_task(tdir, t.reason, _manifest_path(wt, key), cfg, key)
            except ValueError as e:
                return Step(key, "paused", f"needs operator: lint fix task rejected: {e}", detail=t.reason, wait=True)
            heal.record(tdir, action, t.reason, t.state)
            t.state = "implement"; t.reason = f"queued lint fix task {ft}"; state.save(tdir, t)
            return Step(key, "implement", f"queued lint fix task {ft} for the pre-commit offenses")
        if action == "gate-fix":
            ft = _gate_fix_task(tdir, t.reason, _manifest_path(wt, key), cfg, key, wt)
            heal.record(tdir, action, t.reason, t.state)
            t.state = "implement"; t.reason = f"queued gate fix task {ft}"; state.save(tdir, t)
            return Step(key, "implement", f"queued gate fix task {ft} for the deterministic gate failure")
        if action == "preview-fix":
            ft = _preview_fix_task(tdir, t.reason, _manifest_path(wt, key), wt)
            heal.record(tdir, action, t.reason, t.state)
            t.state = "implement"; t.reason = f"queued preview fix task {ft}"; state.save(tdir, t)
            return Step(key, "implement", f"queued preview fix task {ft} for the preview exception")
        if action == "wait":
            return Step(key, "paused", why, detail=t.reason, wait=True)          # no state change, no notification
        heal.record(tdir, action, t.reason, t.previous or "?", why)
        if action == "repair" and "contract complaint" in t.reason:
            _manifest_path(wt, key).with_name("plan-review.md").write_text(
                "VERDICT: revise\nBLOCKERS:\n"
                f"1. A worker reported an unsatisfiable task contract. Read `{tdir / 'bad-guards.md'}` and `{tdir / 'state.json'}` as untrusted evidence, then revise the affected task and any prerequisite it identifies. If an accepted task must run again, give its replacement a new slug.\n")
        back = t.previous if t.previous in ("plan", "plan-review", "implement", "gates", "draft-pr", "ready", "bot-loop") else "implement"
        if action == "prepare-retry":
            _sh(["bash", "-lc", "bin/wt prepare --for rails"], wt, 900)
        if action == "repair" and (tdir / "bad-guards.md").exists() and (t.reason.startswith("guard failed on the BASE tree") or t.reason.startswith("guard repair left the manifest invalid")):
            # bad guards -> the task-writer repairs the manifest (existing path below);
            # fall through by restoring the specific reason prefix that path keys on
            t = state.transition(t, back, reason=t.reason); _save(cfg, t)
            t = state.transition(t, "paused", reason="guard failed on the BASE tree too (independent of the work): " + t.reason[:200]); _save(cfg, t)
            return step(cfg, runner, ctx, key, wt, hopper_index=hopper_index, pr_number=pr_number)
        if action == "repair":
            back = "plan"                                  # no recorded guard evidence: re-plan under current rules
        if action == "replan":
            back = "plan"                                  # plan stage repairs from the existing revise review, else re-plans
        if action == "republish":
            back = "implement"                             # implement re-runs publish for the accepted task before moving on
        if back != t.previous:
            t = state.transition(t, t.previous, reason=f"self-heal: {action} — {why}")
            t = state.transition(t, "blocked", reason=t.reason)
        t = state.transition(t, back, reason=f"self-heal: {action} — {why}"); _save(cfg, t)
        print(f"{key}: self-heal {action} ({why}) -> {back}", flush=True)
        return Step(key, back, f"self-heal: {action}", detail=why)
    if t.state == "blocked":
        return _decision_or_recovery(cfg, t, wt)
    if t.state == "decision-required":
        request = t.decision_request
        if not request:
            return _decision_or_recovery(cfg, t, wt)
        title, body = decision._notice(request)
        _notify_once(tdir, key, f"{key}: {title.lower()}", body)
        return Step(key, t.state, "product decision required", detail=request["question"], wait=True)

    if t.state in ("queued", "spinup"):
        # worktree already exists in the pilot (spinup is the cmux chain); advance
        while t.state != "plan":
            t = state.transition(t, {"queued": "spinup", "spinup": "plan"}[t.state])
        return Step(key, "spinup", "worktree present", detail=str(wt)) if _save(cfg, t) else None

    if t.state in ("plan", "plan-review"):
        manifest = _manifest_path(wt, key)
        review = manifest.with_name("plan-review.md")
        # The verdict must judge THIS plan. A stale `approve` left by an earlier round skipped the
        # required opposite-vendor review entirely -- the mirror of the stale-`revise` bug fixed on
        # 2026-09-21, and the more dangerous direction (branch review 2026-09-23).
        if (manifest.exists() and plan_mod.review_verdict(review) == "approve"
                and plan_mod.review_is_current(review, manifest.with_name("plan.md"))
                and plan_mod.review_is_current(review, manifest)):
            # Guards are checked on the BASE tree before any worker runs. guards.precheck has been
            # documented as a plan-approval step since it was written but had no caller, so a guard
            # that cannot run at all (missing tool, typo) was discovered only by spending worker
            # rungs on it (branch review 2026-09-23).
            problems = []
            for task in contracts.load_tasks(manifest):
                problems += guards.precheck(task, wt)
            if problems:
                (tdir / "bad-guards.md").open("a").write(
                    f"\n## guards unrunnable on the base tree at plan approval -- {time.strftime('%Y-%m-%dT%H:%M:%SZ')}\n\n"
                    + "\n".join(f"- {p}" for p in problems)
                    + "\n\nRewrite these verification commands so they run on the pre-work tree.\n")
                return _pause_step(cfg, t, "plan-review",
                                   f"guard failed on the BASE tree too (independent of the work): "
                                   f"{len(problems)} unrunnable guard(s) at plan approval")
            t = state.transition(t, "plan-review") if t.state == "plan" else t
            t = state.transition(t, "implement"); _save(cfg, t)
            return Step(key, "plan-review", "approved manifest present", detail=str(manifest))
        stage_root = cfg.state_root / "plans" / key / time.strftime("%Y%m%dT%H%M%S")
        # Spend is advisory; repeated mechanical failures use the bounded recovery path, not an owner ruling.
        rows = [r for r in ledger.collect(cfg.state_root) if r.get("ticket") == key]
        baseline = _ruling_baseline(cfg, key, wt)
        spent = sum(r["billed_usd"] for r in rows if (r.get("first_ts") or "") >= baseline)
        has_work = any(_task_accepted(cfg, key, tk) for tk in _manifest_identities(manifest))
        warning = float(getattr(cfg.budgets, "preimplementation_usd", 30.0))
        if not has_work and spent >= warning:
            _notify_once(tdir, key, f"{key} planning spend warning", f"${spent:.2f} spent with no accepted task; continuing automatic plan repair")
        repairs_file = tdir / "plan-repairs.n"
        repairs = int(repairs_file.read_text() or 0) if repairs_file.exists() else 0
        if manifest.exists() and plan_mod.review_verdict(review) == "revise" and repairs < 2:
            # A failed plan with a concrete `revise` review is REPAIRED (task-writer + one critic round), not
            # re-planned from scratch: ~$3 instead of $10-20, and the critic's findings are kept. Two repairs
            # max per ticket; then a full re-plan.
            repairs_file.write_text(str(repairs + 1))
            log = plan_mod.repair_plan(cfg, key, wt, hopper_index, stage_root)
        else:
            if repairs_file.exists(): repairs_file.unlink()
            log = plan_mod.plan_ticket(cfg, key, wt, hopper_index, stage_root)
        stage_root.mkdir(parents=True, exist_ok=True)
        (stage_root / "plan-log.json").write_text(json.dumps(log, indent=2))
        t = state.load(tdir); t.author_vendor = log.get("author_model", ""); t.critic_vendor = log.get("critic_model", "")
        if log.get("result") == "approved":
            if t.state == "plan": t = state.transition(t, "plan-review")
            t = state.transition(t, "implement"); _save(cfg, t)
            # planning/<key>/ is a worktree-local artifact (MEMORY.md: never committed, excluded from
            # PR diff/history). Make sure git ignores it for every worktree of this repo.
            _exclude_planning(wt)
            return Step(key, "plan", "approved", detail=f"{len(log['rounds'])} round(s)")
        if log.get("result") == "blocked":
            t = state.transition(t, "blocked", reason=f"plan: {log.get('reason')}"); _save(cfg, t)
            return Step(key, "plan", "blocked (owner question)", detail=log.get("reason", ""), wait=True)
        return _pause_step(cfg, t, "plan", f"plan failed: {log.get('reason')}")

    if t.state == "implement":
        manifest = _manifest_path(wt, key)
        if not manifest.exists():
            t = state.transition(t, "paused", reason="implement: no manifest"); _save(cfg, t)
            return Step(key, "implement", "no manifest", wait=True)
        try:
            tasks = contracts.load_tasks(manifest)
        except contracts.ManifestError as e:
            return _pause_step(cfg, t, "implement", f"plan failed: manifest invalid: {str(e)[:300]}")
        # Accepted work that never got committed (a publish defect, later fixed) is published now,
        # before anything else runs: the branch must be coherent at every step.
        import runner as runner_mod
        # A hook rejection produced a lint-fix task for the SAME files as accepted-but-unpublished
        # work. Republishing first just re-hits the hook and heals into another identical task
        # (ZIP-7874 span 908/909/910 in nine minutes). Let the fix land, then publish.
        pending_lint_fix = any(tk.slug.startswith("lint-fix-") and not _task_accepted(cfg, key, tk) for tk in tasks)
        for tk in tasks:
            if pending_lint_fix:
                break
            if _task_accepted(cfg, key, tk) and runner_mod._uncommitted_allowlisted(wt, tk, getattr(cfg, "harness_artifact_globs", ())):
                runner_mod.publish_accepted(cfg, key, str(wt), tk, ensure_pr=False)
                t = state.load(tdir)
                if t.state == "paused":
                    return Step(key, "implement", f"republish of task {tk.id} failed", detail=t.reason, wait=True)
                return Step(key, "implement", f"task {tk.id}: published previously accepted work")
        try:
            task = _next_task(cfg, key, tasks)
        except TaskDependencyError as exc:
            (tdir / "bad-guards.md").write_text(f"Task dependencies cannot be satisfied:\n{exc}\n")
            return _pause_step(cfg, t, "implement", f"contract complaint: task dependencies: {exc}")
        if task is None:
            t = state.transition(t, "gates"); _save(cfg, t)
            return Step(key, "implement", "all tasks accepted")
        # Keep the ticket branch current BEFORE each task: guards on generated files (rubocop todo
        # lists, i18n normalization) and rebase-sensitive tests go stale as origin/main moves, and a
        # premium worker then rightly refuses to hand-edit generated output (ZIP-7872/005 x3).
        behind = ci.behind_base(wt)
        if behind and _tree_clean(wt):
            err = _rebase_onto_main(cfg, key, wt)
            if err:
                return _pause_step(cfg, t, "implement", f"{err} (before task {task.id})")
            try:
                publish.push_rebased(wt, publish.current_branch(wt))
            except publish.PublishError:
                pass                                            # no remote yet, or nothing to push: fine
            print(f"{key}: rebased onto origin/main ({behind} commit(s) behind) before task {task.id}", flush=True)
        import runner as runner_mod
        if getattr(cfg, "tasks_parallel", False):          # intra-ticket parallelism: off until implement_task is thread-safe per ticket
            import parallel
            def publish_one(tk, paths):
                runner_mod.publish_accepted(cfg, key, str(wt), tk, ensure_pr=False)
            out = parallel.implement_parallel(cfg, runner, ctx, t, tasks, pathlib.Path(wt),
                                              is_done=lambda tk: _task_accepted(cfg, key, tk), publish_one=publish_one,
                                              log=lambda m: print(m, flush=True))
            if out == "accepted":
                t = state.load(tdir); t = state.transition(t, "gates"); _save(cfg, t)
                return Step(key, "implement", "all tasks accepted (parallel)")
            t = state.load(tdir)
            return Step(key, "implement", f"parallel implement -> {out}", detail=t.reason, wait=True)
        seen = sum(1 for x in tasks if x.visual == task.visual and tasks.index(x) < tasks.index(task))
        out = runner.implement_task(ctx, t, task, seen, str(wt))
        if out == "accepted":
            runner_mod.publish_accepted(cfg, key, str(wt), task, ensure_pr=False)   # the draft-pr stage owns PR creation
            return Step(key, "implement", f"task {task.id} accepted + published")
        t = state.load(tdir)
        return Step(key, "implement", f"task {task.id} -> {out}", detail=t.reason, wait=True)

    if t.state == "gates":
        # Deterministic gates beyond per-task verification: worktree prepared, whole ZUI component
        # test dir, rubocop on the branch's Ruby diff, and -- when any task is visual -- Lookbook
        # captures of every scenario of the component (policy: screenshots before leaving draft).
        _sh(["bin/wt", "prepare", "--for", "rails"], wt, 900)
        # `bin/rails test <path>` skips test:prepare, so the webpack CSS/JS build under app/assets/builds
        # can be stale; system tests and the Lookbook captures would then render old styles
        # (found by the plan critic, 2026-09-14). Build explicitly before anything browser-backed.
        rb = _sh(["bash", "-c", "NODE_ENV=development yarn build"], wt, 900)
        if rb.returncode:
            (tdir / "gates.out").write_text((rb.stdout + rb.stderr)[-6000:])
            return _pause_step(cfg, t, "gates", f"gates failed: yarn build rc={rb.returncode}")
        r1 = _sh(["bin/rails", "test", "test/views/components/zui/"], wt, 1800)
        # Committed work AND anything still in the tree: linting only `origin/main...HEAD` let an
        # uncommitted file reach the pre-commit hook unlinted, and publish died on the hook instead
        # (ZIP-7875: Rails/FindEach + Metrics/AbcSize in a system test, found only at commit time).
        changed = sorted(set(
            _sh(["git", "diff", "--name-only", "origin/main...HEAD", "--", "*.rb"], wt, 60).stdout.split()
            + _sh(["git", "diff", "--name-only", "HEAD", "--", "*.rb"], wt, 60).stdout.split()
            + _sh(["git", "ls-files", "--others", "--exclude-standard", "--", "*.rb"], wt, 60).stdout.split()))
        # --force-exclusion: honour AllCops Exclude (e.g. linters/**) exactly as CI and lefthook do;
        # naming files explicitly would lint excluded paths (gate false-failed on 13 pre-existing offenses).
        r2 = _sh(["bin/agent_run", "rubocop", "--cache", "false", "--force-exclusion", *changed], wt, 600) if changed else None
        ok = r1.returncode == 0 and (r2 is None or r2.returncode == 0)
        (tdir / "gates.out").write_text((r1.stdout + r1.stderr)[-6000:] + ("\n\n" + (r2.stdout + r2.stderr)[-4000:] if r2 else ""))
        if not ok:
            return _pause_step(cfg, t, "gates", f"gates failed: rails test rc={r1.returncode}" + (f", rubocop rc={r2.returncode}" if r2 else ""))
        tasks = contracts.load_tasks(_manifest_path(wt, key)) if _manifest_path(wt, key).exists() else []
        component = _component_name(wt)
        if any(x.visual for x in tasks) and component:
            try:
                base = screenshots.ensure_dev_server(wt)
                shots = screenshots.capture(base, component, tdir / "screenshots" / _head(wt)[:10])
                (tdir / "screenshots.json").write_text(json.dumps({k: str(v) for k, v in shots.items()}))
            except screenshots.VisualGateError as e:
                if e.detail:
                    (tdir / "preview-error.txt").write_text(_preview_error_text(e.detail))
                return _pause_step(cfg, t, "gates", f"visual gate failed: {e}")
        t.evidence_sha = _head(wt); t = state.transition(t, "draft-pr"); _save(cfg, t)
        return Step(key, "gates", "passed", detail=f"evidence_sha={t.evidence_sha[:10]}")

    if t.state == "draft-pr":
        try:
            branch = publish.guard_branch(wt, key); publish.push(wt, branch)
            pr = publish.existing_pr(wt, branch)
            title, body = _pr_house_style(cfg, key, wt, t)
            if not pr:
                pr = publish.ensure_draft_pr(wt, branch, title, body)
                publish.record_pr_on_worktree(wt, pr["number"])
            else:
                publish.github_write("pr-edit-body", ["gh", "pr", "edit", str(pr["number"]), "--repo", publish.REPO, "--title", title, "--body", body], wt)
            cis = _load_json(_ci_state_path(cfg, key), {}); cis["pr"] = pr["number"]; _ci_state_path(cfg, key).write_text(json.dumps(cis))
            t = state.transition(t, "ready"); _save(cfg, t)
            return Step(key, "draft-pr", "draft PR present", detail=f"#{pr['number']}")
        except publish.PublishError as e:
            return _pause_step(cfg, t, "draft-pr", f"publish failed: {e}")

    if t.state == "ready":
        cis = _load_json(_ci_state_path(cfg, key), {}); pr = cis.get("pr") or pr_number
        checks = ci.fetch_checks(pr, publish.REPO, wt)
        verdict = ci.classify(checks, behind=ci.behind_base(wt), attempts=cis.get("attempts", 0),
                              failure_text=_failure_text(checks, wt), prior_actions=tuple(cis.get("actions", [])))
        cis.setdefault("actions", [])
        if verdict.action == "wait":
            _ci_state_path(cfg, key).write_text(json.dumps(cis)); return Step(key, "ready", "CI running", detail=verdict.reason, wait=True)
        if verdict.action == "green":
            if cis.get("ready_marked_sha") != _head(wt):
                # Tier-3 design decisions (`human_confirm_before_ready`) are NOT a gate on mark-ready:
                # the loop never assigns reviewers, so "ready" only triggers the repo's Claude review
                # bots, and Human Gate 1 -- where Cody confirms these decisions -- still precedes any
                # colleague. They are recorded here and surfaced in the Gate 1 report and the PR body.
                if _manifest_flag(wt, key, "human_confirm_before_ready"):
                    cis["tier3_pending"] = _manifest_header_comment(wt, key)
                try:
                    botreview.mark_ready(pr, wt)
                except publish.PublishError as e:
                    return _pause_step(cfg, t, "ready", f"mark ready failed: {e}")
                cis["ready_marked_sha"] = _head(wt)
            _ci_state_path(cfg, key).write_text(json.dumps(cis))
            t = state.transition(t, "bot-loop"); _save(cfg, t)
            return Step(key, "ready", "CI green; PR marked ready", detail=f"#{pr}")
        cis["attempts"] = cis.get("attempts", 0) + 1; cis["actions"].append(verdict.action); _ci_state_path(cfg, key).write_text(json.dumps(cis))
        if verdict.action == "escalate":
            return _pause_step(cfg, t, "ready", verdict.reason)
        if verdict.action == "rerun":
            for c in checks:
                if c.name in verdict.failing and "actions/runs" in c.link: ci.rerun_gha(c.link, publish.REPO, wt)
                elif c.name in verdict.failing and "circleci.com" in c.link: ci.rerun_circleci(c.link)
            return Step(key, "ready", "CI rerun requested", detail=verdict.reason, wait=True)
        if verdict.action == "rebase":
            err = _rebase_onto_main(cfg, key, wt)
            if err:
                return _pause_step(cfg, t, "ready", err)
            try:
                publish.push_rebased(wt, publish.current_branch(wt))
            except publish.PublishError as e:
                return _pause_step(cfg, t, "ready", f"publish failed: {e}")
            t = state.transition(t, "gates"); _save(cfg, t)
            return Step(key, "ready", "rebased onto origin/main; re-running gates")
        # fix: write a scoped task and go back to implement
        fix = {"id": f"8{cis['attempts']:02d}", "slug": f"ci-fix-{cis['attempts']}", "summary": f"Fix the CI failure in {', '.join(verdict.failing)} (see feedback)",
               "allowed_files": _sh(["git", "diff", "--name-only", "origin/main...HEAD"], wt, 60).stdout.split()
                                or sorted({g for x in contracts.load_tasks(_manifest_path(wt, key)) for g in x.allowed_files}),
               "may_edit_tests": True, "visual": False,
               "verification_commands": ["bin/rails test test/views/components/zui/"], "acceptance": ["AC-1: the failing CI check's command passes locally"],
               "invariants": ["Do not weaken any test or lint rule"], "stop_when": ["the verification commands pass"]}
        _append_task(_manifest_path(wt, key), fix)
        (cfg.state_root / "attempts" / key / fix["id"]).mkdir(parents=True, exist_ok=True)
        (cfg.state_root / "attempts" / key / fix["id"] / "feedback.md").write_text(f"# CI failure\n\n```\n{_failure_text(checks, wt)[-4000:]}\n```\n")
        t = state.transition(t, "implement", reason=f"ci-fix {fix['id']} queued"); _save(cfg, t)
        return Step(key, "ready", "CI code failure -> fix task queued", detail=fix["id"])

    if t.state == "bot-loop":
        cis = _load_json(_ci_state_path(cfg, key), {}); pr = cis.get("pr") or pr_number
        checks = ci.fetch_checks(pr, publish.REPO, wt)
        if ci.pr_mergeable(pr, wt) == "CONFLICTING":
            err = _rebase_onto_main(cfg, key, wt)
            if err:
                return _pause_step(cfg, t, "bot-loop", err)
            try:
                publish.push_rebased(wt, publish.current_branch(wt))
            except publish.PublishError as e:
                return _pause_step(cfg, t, "bot-loop", f"publish failed: {e}")
            for stale in ("ready_marked_sha", "bot_wait_since", "bot_retriggered"):
                cis.pop(stale, None)
            _ci_state_path(cfg, key).write_text(json.dumps(cis))
            t = state.transition(t, "gates"); _save(cfg, t)
            return Step(key, "bot-loop", "PR conflicted with main; rebased, re-running gates")
        if not checks or any(c.bucket == "pending" for c in checks):
            return Step(key, "bot-loop", "checks (incl. claude-review) still running", wait=True)
        ledger_p = _ledger_path(cfg, key); seen_comments = botreview.load_ledger(ledger_p)   # NOT `ledger`: shadows the ledger module for the whole function
        comments = botreview.fetch_bot_comments(pr, wt); todo = botreview.unhandled(comments, seen_comments)
        answered = {str(i) for i in cis.get("owner_answered", [])}
        if any(str(c["id"]) in answered for c in todo):
            # Cody answered these questions (`runner.py answer`); never ask them again.
            for c in todo:
                if str(c["id"]) in answered:
                    seen_comments[str(c["id"])] = c["body_hash"]
            botreview.save_ledger(ledger_p, seen_comments)
            todo = botreview.unhandled(comments, seen_comments)
        if not todo:
            rounds = cis.get("bot_rounds", 0)
            if rounds == 0 and not comments:
                since = cis.setdefault("bot_wait_since", time.time())
                if time.time() - since < BOT_WAIT_S:
                    _ci_state_path(cfg, key).write_text(json.dumps(cis))
                    return Step(key, "bot-loop", "no bot comments yet", wait=True)
                if not cis.get("bot_retriggered"):
                    try:
                        botreview.retrigger(pr, wt)
                    except publish.PublishError as e:
                        print(f"{key}: bot review retrigger failed: {e}", flush=True)
                    cis["bot_retriggered"] = True; cis["bot_wait_since"] = time.time()
                    _ci_state_path(cfg, key).write_text(json.dumps(cis))
                    return Step(key, "bot-loop", "no bot review after the deadline; re-requested it", wait=True)
                cis["bot_review"] = "did not run"                # advisory; Cody's review is the gate
                _ci_state_path(cfg, key).write_text(json.dumps(cis))
            unmet = _unmet_review_promises(cfg, key, wt)
            if unmet:
                return _pause_step(cfg, t, "bot-loop",
                                   "review fixes were promised in replies but never implemented: "
                                   + ", ".join(f"comment {cid} -> task {tid}" for cid, tid in unmet.items()))
            t = state.transition(t, "human-gate-1"); _save(cfg, t)
            return Step(key, "bot-loop", "all bot comments adjudicated; CI green", detail="HUMAN GATE 1", wait=True)
        rounds = cis.get("bot_rounds", 0)
        if rounds >= MAX_BOT_ROUNDS:
            failed = cfg.state_root / "botreview" / key / f"round-{rounds}"
            out = "" if (failed / "adjudication.json").exists() else (plan_mod.provider_exhausted(failed) or plan_mod.launch_failure(failed))
            if out:
                model = (failed / "model.txt").read_text().strip() if (failed / "model.txt").exists() else t.critic_vendor
                if model:
                    import providers
                    providers.note_run(cfg.state_root, model, failed)
                _reclaim_provider_round(cfg, key, cis, rounds, failed)
                return _pause_step(cfg, t, "bot-loop", f"provider unavailable: {out}")
            return _pause_step(cfg, t, "bot-loop", f"{len(todo)} bot comments outstanding after {MAX_BOT_ROUNDS} rounds")
        # Reuse a paid-for adjudication if the previous round's file already covers every open comment
        # (a later step failed after the critic ran -- do not pay the critic twice for the same comments).
        rnd = rounds
        prev = cfg.state_root / "botreview" / key / f"round-{rnd}" / "adjudication.json" if rnd else None
        decisions = None
        if prev and prev.exists():
            try:
                cand = botreview.parse_adjudication(prev)
                # ...but only if its replies can actually be POSTED. A cached adjudication whose
                # replies fail the reply lint can never succeed, and replaying it pauses the ticket
                # forever (ZIP-7874 stalled on a cached reply citing the plan, 2026-09-22).
                unpostable = [d.get("id") for d in cand
                              if botreview.workflow_vocabulary(d.get("reply") or "")
                              or any(p in (d.get("reply") or "").lower() for p in botreview.PLATITUDES)]
                bad_tasks = _fix_task_violations(_manifest_path(wt, key), cand)
                if unpostable:
                    print(f"{key}: discarding cached adjudication; replies for {unpostable} cannot be posted", flush=True)
                elif bad_tasks:
                    print(f"{key}: discarding cached adjudication; its fix tasks break the schema: {bad_tasks[:2]}", flush=True)
                elif {c["id"] for c in todo} <= {d.get("id") for d in cand}:
                    decisions = cand; stage = prev.parent
            except RuntimeError:
                decisions = None
        if decisions is None:
            rnd += 1; cis["bot_rounds"] = rnd; _ci_state_path(cfg, key).write_text(json.dumps(cis))
            stage = cfg.state_root / "botreview" / key / f"round-{rnd}"; stage.mkdir(parents=True, exist_ok=True)
            botreview.write_comments_file(todo, stage / "comments.md")
            prompt = botreview.ADJUDICATION_PROMPT.format(key=key, pr=pr, out=stage / "adjudication.json", schema=stage / "schema.toml",
                                                          wt=wt, plan=_manifest_path(wt, key).with_name("plan.md"), comments=stage / "comments.md")
            (stage / "schema.toml").write_text(plan_mod.SCHEMA_EXAMPLE)
            # A dedicated role: reusing the critic definition made Astra review the plan and refuse the
            # adjudication as out of role (ZIP-7872 round 1, 2026-09-16). Model = the ticket's critic vendor.
            _res, used, out = plan_mod.run_agent_with_failover(cfg, "review-adjudicator", prompt, stage, wt, 1800,
                                                               model=t.critic_vendor or None, log=lambda m: print(m, flush=True))
            if out:
                # The provider refused to run the model (usage limit / 429 / overload). Nothing is wrong
                # with the plan, the PR or the comments: wait and try again (heal: retry).
                _reclaim_provider_round(cfg, key, cis, rnd, stage)
                return _pause_step(cfg, t, "bot-loop", f"provider unavailable: {out}")
            try:
                decisions = botreview.parse_adjudication(stage / "adjudication.json")
            except RuntimeError as e:
                return _pause_step(cfg, t, "bot-loop", f"adjudication unusable: {e}")
            # Fix tasks must pass the manifest schema BEFORE any reply promises them. A rejected task used
            # to surface after the replies, and every re-adjudication produced the same one (ZIP-4281).
            for n in range(1, ADJUDICATION_FIX_PASSES + 1):
                viol = _fix_task_violations(_manifest_path(wt, key), decisions)
                if not viol:
                    break
                fix_prompt = prompt + ("\n\n## Schema violations in your fix tasks (rewrite the tasks, keep the decisions)\n"
                                       + "\n".join(f"- {v}" for v in viol))
                plan_mod.run_agent_with_failover(cfg, "review-adjudicator", fix_prompt, stage / f"fix-{n}", wt, 1800,
                                                 model=t.critic_vendor or None, log=lambda m: print(m, flush=True))
                try:
                    decisions = botreview.parse_adjudication(stage / "adjudication.json")
                except RuntimeError as e:
                    return _pause_step(cfg, t, "bot-loop", f"adjudication unusable: {e}")
            else:
                viol = _fix_task_violations(_manifest_path(wt, key), decisions)
                if viol:
                    return _pause_step(cfg, t, "bot-loop", f"adjudication unusable: fix tasks still violate the schema: {viol[:2]}")
        by_id = {c["id"]: c for c in todo}; fixes = []; questions = []
        model_short = (t.critic_vendor or "critic").split("/")[-1]
        for i, d in enumerate(decisions, 1):
            c = by_id.get(d.get("id"))
            if not c: continue
            if d["decision"] == "question":
                questions.append(f"comment {c['id']}: {d.get('reply','')}"); continue
            if d["decision"] == "fix":
                # Number from the MANIFEST, not a loop counter: a 9nn id already in the plan (an
                # operator-queued follow-up, or a fix from an earlier round) collided and the whole
                # batch was rejected as "duplicate id 901" (ZIP-7874, 2026-09-21).
                ft = botreview.fix_task_from(d, int(_next_runner_task_id(_manifest_path(wt, key))) - 900 + len(fixes))
                if ft: fixes.append(ft)
            # Record the intent to reply BEFORE the POST and flush it immediately: a crash between a
            # successful POST and the end-of-batch save left the comment unhandled, so the next round
            # replayed the cached adjudication and posted the reply a second time, publicly
            # (branch review 2026-09-23). A duplicate reply is worse than a missing one.
            seen_comments[str(c["id"])] = c["body_hash"]
            botreview.save_ledger(ledger_p, seen_comments)
            try:
                botreview.reply(pr, c, botreview.clean_reply(d.get("reply", "") or f"Addressed in a follow-up commit.", model_short), wt)
            except ValueError as e:
                seen_comments.pop(str(c["id"]), None)     # never posted: leave it unanswered
                botreview.save_ledger(ledger_p, seen_comments)
                return _pause_step(cfg, t, "bot-loop", f"reply rejected: {e}")
            except publish.PublishError as e:
                botreview.save_ledger(ledger_p, seen_comments)          # keep what was already answered
                return _pause_step(cfg, t, "bot-loop", f"reply failed: {e}")
            if d["decision"] == "fix":
                # The promise exists as soon as the reply is public -- NOT when a task is queued. A
                # replay of a cached adjudication re-posted four "I'll fix this" replies and queued
                # nothing, and the gate opened over them (ZIP-7874, 2026-09-22).
                _record_promise(cfg, key, str(c["id"]))
        botreview.save_ledger(ledger_p, seen_comments)
        if questions:
            return _pause_step(cfg, t, "bot-loop", "reviewer questions need Cody: " + " | ".join(questions)[:600])
        if fixes:
            try:
                for ft in fixes: _append_task(_manifest_path(wt, key), ft)
            except ValueError as e:
                # The replies for these comments PROMISED code. If the fix tasks cannot enter the
                # manifest, un-answer them so the next round re-adjudicates and actually implements
                # them: leaving them "answered" let ZIP-7874 reach Human Gate 1 with six promised
                # fixes and zero commits (2026-09-21).
                for ft_ in fixes:
                    seen_comments.pop(str(ft_.get("comment_id", "")), None)
                for d_ in decisions:
                    if d_.get("decision") == "fix":
                        seen_comments.pop(str(d_.get("id")), None)
                botreview.save_ledger(ledger_p, seen_comments)
                (tdir / "bad-guards.md").open("a").write(f"\n## review fix tasks rejected by the schema -- {time.strftime('%Y-%m-%dT%H:%M:%SZ')}\n\n{e}\n\nRewrite them to satisfy the schema (repo tools only, --force-exclusion on rubocop/reek, <=3 bespoke commands).\n")
                return _pause_step(cfg, t, "bot-loop", f"guard failed on the BASE tree too (independent of the work): {e}")
            # A reply that says "fixed" is a PROMISE: record it so the gate can refuse to open until
            # the code exists (ZIP-7874 reached Human Gate 1 with 6 promised fixes and 0 commits).
            for d_, ft_ in zip([x for x in decisions if x.get("decision") == "fix"], fixes):
                _record_promise(cfg, key, str(d_["id"]), ft_["id"])
            t = state.transition(t, "implement", reason=f"{len(fixes)} review fix task(s) queued"); _save(cfg, t)
            return Step(key, "bot-loop", f"{len(fixes)} fix task(s) queued from review; back to implement")
        return Step(key, "bot-loop", f"{len(decisions)} comment(s) answered; none required code changes")

    if t.state == "human-gate-1":
        pr = _load_json(_ci_state_path(cfg, key), {}).get("pr")
        _notify_once(tdir, key, f"{key} ready for review", f"PR #{pr}" if pr else "at Human Gate 1")
        return Step(key, "human-gate-1", "waiting for Cody", wait=True)
    return Step(key, t.state, "no handler", wait=True)


def run_once(cfg, runner, ctx, key: str, wt, *, hopper_index: int, max_steps: int = 12) -> list[Step]:
    steps = []
    for _ in range(max_steps):
        s = step(cfg, runner, ctx, key, wt, hopper_index=hopper_index)
        steps.append(s); print(s, flush=True)
        if s.wait:
            break
    return steps


# ----------------------------------------------------------------------------- helpers

def _pause_step(cfg, t, stage, reason) -> Step:
    _pause(cfg, t, reason); return Step(t.key, stage, "paused", detail=reason, wait=True)


def _sh(cmd, cwd, timeout):
    return subprocess.run(["bash", "-lc", " ".join(__import__("shlex").quote(c) for c in cmd)], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def _head(wt) -> str:
    return _sh(["git", "rev-parse", "HEAD"], wt, 30).stdout.strip()


def _failure_text(checks, wt) -> str:
    parts = []
    for c in checks:
        if c.bucket not in ("fail", "cancel"):
            continue
        if "actions/runs" in c.link:
            parts.append(ci.gha_failure_excerpt(c.link, publish.REPO, wt))
        elif "circleci.com" in c.link:
            parts.append(ci.circleci_failure_excerpt(c.link))
    return "\n".join(parts)


def _manifest_flag(wt, key, flag) -> bool:
    p = _manifest_path(wt, key)
    if not p.exists(): return False
    import tomllib
    return bool(tomllib.loads(p.read_text()).get(flag))


def _append_task(manifest: pathlib.Path, task: dict) -> None:
    """Appends and VALIDATES: a generated task that fails the schema would crash every later start
    (ZIP-7875: adjudicator fix tasks lacked --force-exclusion; the runner raised ManifestError on each
    launchd restart -- a crash loop no heal policy can see)."""
    botreview.write_fix_manifest([task], manifest.with_name("_append.toml"))
    body = manifest.with_name("_append.toml").read_text().split("\n", 1)[1]
    before = manifest.read_text()
    with open(manifest, "a") as f:
        f.write("\n" + body)
    manifest.with_name("_append.toml").unlink()
    _validate_or_revert(manifest, before)


def _component_name(wt) -> str | None:
    """`app/views/components/zui/<name>/` added on this branch."""
    out = _sh(["git", "diff", "--name-only", "origin/main...HEAD", "--", "app/views/components/zui/"], wt, 60).stdout.split()
    names = sorted({p.split("/")[4] for p in out if p.count("/") >= 5})
    return names[0] if names else None


def _plan_section(plan_md: str, heading: str) -> str:
    m = re.search(rf"^## {re.escape(heading)}\s*\n(.*?)(?=^## |\Z)", plan_md, re.M | re.S)
    return m.group(1).strip() if m else ""


def _ticket_summary(cfg, key) -> str:
    for d in sorted((cfg.state_root / "plans" / key).glob("*/ticket.json"), reverse=True):
        try:
            return json.loads(d.read_text())["fields"]["summary"]
        except Exception:  # noqa: BLE001
            continue
    return key


def insert_task_before(manifest: pathlib.Path, task: dict, before_id: str) -> None:
    """Insert a prerequisite fix task immediately before `before_id` in manifest order. The runner
    dispatches tasks in file order, so a contract defect found in a LATER task (007 found 001's
    tag enum wrong, 2026-09-14) is repaired before that later task runs again."""
    botreview.write_fix_manifest([task], manifest.with_name("_insert.toml"))
    body = manifest.with_name("_insert.toml").read_text().split("\n", 1)[1].strip("\n")
    manifest.with_name("_insert.toml").unlink()
    text = manifest.read_text()
    marker = f'\n[[tasks]]\nid = {json.dumps(before_id)}'
    i = text.find(marker)
    if i < 0:
        raise ValueError(f"task {before_id} not found in {manifest}")
    manifest.write_text(text[:i] + "\n" + body + "\n" + text[i:])
    _validate_or_revert(manifest, text)


def _manifest_header_comment(wt, key) -> str:
    """The planner's owner/design questions live as a comment block at the top of tasks.toml."""
    p = _manifest_path(wt, key)
    if not p.exists():
        return ""
    lines = []
    for ln in p.read_text().splitlines():
        if ln.startswith("#"):
            lines.append(ln.lstrip("# ").rstrip())
        elif ln.strip() and not ln.startswith("human_confirm_before_ready"):
            break
    return " ".join(lines).strip()


def _exclude_planning(wt) -> None:
    """Add `planning/` to the repo's shared info/exclude (common git dir, so linked worktrees share it)."""
    common = _sh(["git", "rev-parse", "--git-common-dir"], wt, 30).stdout.strip()
    if not common:
        return
    p = pathlib.Path(common) if pathlib.Path(common).is_absolute() else pathlib.Path(wt) / common
    ex = p / "info" / "exclude"; ex.parent.mkdir(parents=True, exist_ok=True)
    cur = ex.read_text() if ex.exists() else ""
    lines = cur.splitlines()
    add = [e for e in ("planning/", ".pi/") if e not in lines]     # .pi/: the coordinator pane's own pi session state
    if add:
        ex.write_text(cur.rstrip("\n") + "\n" + "\n".join(add) + "\n")


HOUSE_STYLE_EXAMPLE_PR = 47860     # a PR of Cody's whose structure/voice is the target


def _pr_house_style(cfg, key, wt, t) -> tuple[str, str]:
    import prbody
    summary = _ticket_summary(cfg, key)
    # Title from the TICKET summary ("ZUI Component Library - ViewComponent - Card - Header Start"
    # -> "Add ZUI Card Header Start"), not from the component directory: a slot added to Card is not
    # "Add ZUI Card component" (PR #47940).
    parts = [p.strip() for p in summary.split(" - ")]
    tail = " ".join(parts[2:]) if len(parts) >= 3 and parts[0].lower().startswith("zui") else (_component_name(wt) or "component").replace("_", " ").title()
    title = publish.pr_title(key, f"Add ZUI {tail}")
    shots_p = cfg.ticket_dir(key) / "screenshots.json"; shots_md = None
    if shots_p.exists():
        shots = {k: pathlib.Path(v) for k, v in json.loads(shots_p.read_text()).items()}
        shots_md = screenshots.screenshots_table(shots, screenshots.upload(list(shots.values()), wt))
    gates_out = (cfg.ticket_dir(key) / "gates.out").read_text(errors="replace") if (cfg.ticket_dir(key) / "gates.out").exists() else ""
    example = _sh(["gh", "pr", "view", str(HOUSE_STYLE_EXAMPLE_PR), "--repo", publish.REPO, "--json", "body", "--jq", ".body"], wt, 60).stdout
    stage = cfg.state_root / "prbody" / key / time.strftime("%Y%m%dT%H%M%S")
    try:
        body = prbody.write_body(cfg, key, wt, t, summary, example, shots_md, gates_out, stage)
    except prbody.BodyRejected as e:
        print(f"{key}: PR body rejected twice ({e}); using the deterministic fallback", file=sys.stderr)
        files = _sh(["git", "diff", "--name-only", "origin/main...HEAD"], wt, 60).stdout.split()
        body = prbody.fallback_body(key, summary, [f for f in files if f.startswith("app/")], shots_md)
    return title, body


def _tree_clean(wt) -> bool:
    out = _sh(["git", "status", "--porcelain"], wt, 60).stdout
    return not any(l and not l.startswith("??") for l in out.splitlines())


GENERATED = {".rubocop/zui_todo.yml": "bin/agent_run bundle exec ruby linters/generate_zui_todo.rb"}

# Files whose rebase conflicts can be resolved by taking HEAD for each conflicting hunk
# and then normalizing.  Unlike GENERATED files, we do NOT replace the whole file with
# --ours because the commit being replayed may have already applied non-conflicting
# additions (e.g. new locale keys) that must be preserved.  The normalize command
# re-sorts the result so the file stays canonical.
NORMALIZED = {"config/locales/en.yml": "bundle exec i18n-tasks normalize"}


def _resolve_normalized_conflict(path: str, wt: str, normalize_cmd: str) -> bool:
    """Resolve conflict markers in a normalizable file by taking HEAD for each
    conflicting hunk while keeping non-conflicting content, then re-normalizing.
    Returns True when the file is cleanly resolved and staged."""
    import os
    full = os.path.join(wt, path)
    try:
        content = open(full).read()
    except OSError:
        return False
    if "<<<<<<< HEAD" not in content:
        return True  # nothing to do
    resolved, in_conflict, take = [], False, True
    for line in content.splitlines(keepends=True):
        if line.startswith("<<<<<<<"):
            in_conflict, take = True, True   # "ours" during rebase = origin/main = HEAD side
        elif line.startswith("======="):
            take = False
        elif line.startswith(">>>>>>>"):
            in_conflict = False
        elif not in_conflict or take:
            resolved.append(line)
    open(full, "w").writelines(resolved)
    r = _sh(["bash", "-lc", normalize_cmd], wt, 300)
    if r.returncode:
        return False
    _sh(["git", "add", path], wt, 60)
    return True


def _resolve_generated_conflicts(wt) -> bool:
    """During a rebase, a conflict confined to GENERATED or NORMALIZED files is resolved
    automatically, then the rebase continues -- for every remaining commit. Any conflict in
    a hand-written file returns False (the caller aborts and pauses for a human).
    GENERATED files are replaced wholesale (--ours) and regenerated.
    NORMALIZED files have their conflict markers resolved hunk-by-hunk (HEAD wins) and are
    renormalized, preserving non-conflicting additions the commit already applied.
    (ZIP-7872: .rubocop/zui_todo.yml; ZIP-7875: config/locales/en.yml)"""
    resolvable = set(GENERATED) | set(NORMALIZED)
    for _ in range(50):                                  # bounded: one iteration per rebased commit at most
        conflicted = _sh(["git", "diff", "--name-only", "--diff-filter=U"], wt, 60).stdout.split()
        if not conflicted:
            return True
        if any(p not in resolvable for p in conflicted):
            return False
        for p in conflicted:
            if p in NORMALIZED:
                if not _resolve_normalized_conflict(p, wt, NORMALIZED[p]):
                    return False
            else:
                _sh(["git", "checkout", "--ours", p], wt, 60)  # during rebase, --ours = the upstream side
                g = _sh(["bash", "-lc", GENERATED[p]], wt, 900)
                if g.returncode:
                    return False
                _sh(["git", "add", p], wt, 60)
        r = _sh(["git", "-c", "core.editor=true", "rebase", "--continue"], wt, 600)
        if r.returncode == 0 and not _sh(["git", "diff", "--name-only", "--diff-filter=U"], wt, 60).stdout.strip():
            return True
    return False


RENOTIFY_S = 6 * 3600          # a ticket waiting on a human says so again every 6 hours


def _notify_once(tdir, key: str, title: str, body: str) -> None:
    """One macOS + cmux notification per (ticket, state, reason) -- the only two moments a human is
    needed: a ticket at Human Gate 1, or a pause/block the heal policy will not retry."""
    import hashlib, subprocess
    sig = hashlib.sha256(f"{title}|{body}".encode()).hexdigest()[:12]
    marker = pathlib.Path(tdir) / f".notified-{sig}"
    # Re-raise a standing request every RENOTIFY_S. One notification is a moment; a ticket that is
    # BLOCKED ON A HUMAN is a standing state, and going silent hides it (ZIP-7874 waited 3 days and
    # 1627 respawns after a single alert). Same signature, so it never becomes a stream.
    if marker.exists() and (time.time() - marker.stat().st_mtime) < RENOTIFY_S:
        return
    marker.touch()
    for cmd in (["cmux", "notify", "--title", title, "--body", body],
                ["osascript", "-e", f'display notification {json.dumps(body)} with title {json.dumps(title)}']):
        try:
            subprocess.run(cmd, capture_output=True, timeout=10)
        except Exception:  # noqa: BLE001
            pass


def _ruling_baseline(cfg, key: str, wt) -> str:
    """UTC baseline for spend warnings, refreshed when the owner supplies new planning evidence."""
    import hashlib
    plan_p = _manifest_path(wt, key).with_name("plan.md")
    marker = cfg.ticket_dir(key) / "ruling-baseline.json"
    cur = json.loads(marker.read_text()) if marker.exists() else {"hash": "", "ts": ""}
    if not plan_p.exists():
        return cur.get("ts", "")
    rulings = [b for b in plan_p.read_text().split("\n## ") if b.startswith("Owner rulings")]
    h = hashlib.sha256("\n".join(rulings).encode()).hexdigest()[:16] if rulings else ""
    if h and h != cur.get("hash"):
        cur = {"hash": h, "ts": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        marker.parent.mkdir(parents=True, exist_ok=True); marker.write_text(json.dumps(cur))
    return cur.get("ts", "")


ADJUDICATION_FIX_PASSES = 2
GUARD_FIX_PASSES = 3


def _fix_task_violations(manifest: pathlib.Path, decisions: list) -> list:
    """Schema violations the adjudicator's fix tasks would cause if appended to the manifest."""
    import contracts as _contracts
    base = tomllib.loads(manifest.read_text()) if manifest.exists() else {"tasks": []}
    start = int(_next_runner_task_id(manifest)) - 900
    tasks = [ft for i, d in enumerate([d for d in decisions if d.get("decision") == "fix"])
             if (ft := botreview.fix_task_from(d, start + i))]
    if not tasks:
        return []
    errs = _contracts.validate_tasks({"tasks": list(base.get("tasks", [])) + tasks})
    n = len(base.get("tasks", []))
    return [e for e in errs if any(e.startswith(f"tasks[{i}]") for i in range(n, n + len(tasks)))]


def _validate_or_revert(manifest: pathlib.Path, before: str) -> None:
    """Keep the manifest always-valid: an invalid append is rolled back and raised, so the caller
    records a repairable failure instead of leaving a manifest every future start will choke on."""
    import plan as _plan
    viol = _plan.validate_manifest(manifest)
    if viol:
        manifest.write_text(before)
        raise ValueError(f"generated task rejected by the schema (manifest unchanged): {viol[:3]}")


def _rebase_finished(wt) -> bool:
    """No rebase in progress, no conflict markers staged, and origin/main is an ancestor of HEAD."""
    gitdir = _sh(["git", "rev-parse", "--git-dir"], wt, 60).stdout.strip()
    gitdir = pathlib.Path(gitdir if pathlib.Path(gitdir).is_absolute() else pathlib.Path(wt) / gitdir)
    if (gitdir / "rebase-merge").exists() or (gitdir / "rebase-apply").exists():
        return False
    if _sh(["git", "diff", "--name-only", "--diff-filter=U"], wt, 60).stdout.strip():
        return False
    if _sh(["git", "grep", "-l", "-E", "^(<<<<<<<|>>>>>>>) ", "HEAD"], wt, 120).stdout.strip():
        return False
    return _sh(["git", "merge-base", "--is-ancestor", "origin/main", "HEAD"], wt, 60).returncode == 0


REBASE_PROMPT = """# Finish a rebase for {key}

A `git rebase origin/main` is in progress in `{wt}` and stopped on conflicts in hand-written files.
Resolve every conflict so that BOTH sides' intent survives: main's change and this ticket's change.
Then `git add` the files and `git -c core.editor=true rebase --continue`; repeat until the rebase completes.
Touch only conflicted files. Do not abort, skip commits, push, or edit anything else.
Conflicted now: {files}
End with `REBASE: done` or `REBASE: blocked — <one line>`."""


DEPENDENCY_MANIFESTS = ("package.json", "yarn.lock", "Gemfile", "Gemfile.lock", ".ruby-version", ".node-version")


def _after_rebase(wt) -> None:
    """A rebase can move dependency manifests (main added a JS package) and leaves incremental build
    caches describing the old tree; both failed the pre-push typecheck (ZIP-4287, ZIP-4288)."""
    publish.clear_build_caches(wt)
    moved = _sh(["git", "diff", "--name-only", "ORIG_HEAD..HEAD"], wt, 60).stdout.split()
    if any(path in DEPENDENCY_MANIFESTS for path in moved):
        _sh(["bash", "-lc", "bin/wt prepare --for rails"], wt, 1800)


def _rebase_onto_main(cfg, key: str, wt) -> str:
    """Rebase onto origin/main. Generated files regenerate; hand-written conflicts go to a worker.
    Returns "" on success, else a reason (the rebase is aborted). Never a human question."""
    r = _sh(["git", "rebase", "origin/main"], wt, 600)
    if r.returncode == 0:
        _after_rebase(wt)
        return ""
    if _resolve_generated_conflicts(wt) and _rebase_finished(wt):
        _after_rebase(wt)
        return ""
    files = _sh(["git", "diff", "--name-only", "--diff-filter=U"], wt, 60).stdout.split()
    stage = cfg.state_root / "rebase" / key / time.strftime("%Y%m%dT%H%M%S")
    stage.mkdir(parents=True, exist_ok=True)
    try:
        plan_mod._run_agent(cfg, "premium-worker", REBASE_PROMPT.format(key=key, wt=wt, files=", ".join(files) or "see git status"),
                            stage, pathlib.Path(wt), 1800)
    except Exception as exc:  # noqa: BLE001
        print(f"{key}: rebase worker failed to run: {exc}", flush=True)
    if _rebase_finished(wt):
        _after_rebase(wt)
        return ""
    _sh(["git", "rebase", "--abort"], wt, 60)
    return f"rebase onto origin/main left conflicts the worker could not resolve: {', '.join(files)[:200]}"


def recover_interrupted_git_state(wt, sh=None) -> str:
    """A rebase/merge interrupted by a restart leaves a DETACHED HEAD, and every later stage misreads the
    worktree (ZIP-7875: a ManifestError crash loop killed the runner mid-rebase; publish then refused to
    publish "from branch ''"). Finish what can be finished, otherwise abort so the branch ref -- and every
    commit on it -- comes back. Returns a description, or "" when nothing was wrong.

    Safe by construction: `rebase --abort` restores the pre-rebase branch tip; no commit is lost."""
    sh = sh or (lambda argv, timeout=120: _sh(argv, wt, timeout))
    gd = (sh(["git", "rev-parse", "--git-dir"], 30).stdout or "").strip() or ".git"
    root = pathlib.Path(gd) if pathlib.Path(gd).is_absolute() else pathlib.Path(wt) / gd
    in_rebase = any((root / d).exists() for d in ("rebase-merge", "rebase-apply"))
    in_merge = (root / "MERGE_HEAD").exists()
    if not (in_rebase or in_merge):
        return ""
    unmerged = [f for f in (sh(["git", "diff", "--name-only", "--diff-filter=U"], 60).stdout or "").split() if f]
    # Resolve exactly what the normal rebase path resolves, the same way. This used to accept ANY
    # config/locales/*.yml and take `--theirs`, which during a rebase is the replayed side -- the
    # opposite of the hunk-by-hunk "HEAD wins" rule used everywhere else, and broader than the
    # declared NORMALIZED set. Two answers to one conflict is how a locale file silently loses
    # upstream content (branch review 2026-09-23; ZIP-7875 lost 38 lines to a hand resolution).
    resolvable = set(GENERATED) | set(NORMALIZED)
    if unmerged and all(f in resolvable for f in unmerged):
        ok = True
        for f in unmerged:
            if f in GENERATED:
                if sh(["git", "checkout", "--ours", "--", f], 60).returncode: ok = False; break
                if sh(["bash", "-lc", GENERATED[f]], 900).returncode: ok = False; break
            elif not _resolve_normalized_conflict(f, wt, NORMALIZED[f]):
                ok = False; break
            if sh(["git", "add", "--", f], 60).returncode:
                ok = False; break
        if not ok:
            sh(["git", "rebase", "--abort"] if in_rebase else ["git", "merge", "--abort"], 120)
            return (f"aborted an interrupted {'rebase' if in_rebase else 'merge'}: its conflicts could "
                    f"not be resolved the way the normal path resolves them (branch restored)")
        cont = ["git", "-c", "core.editor=true", "rebase", "--continue"] if in_rebase else ["git", "-c", "core.editor=true", "commit", "--no-edit"]
        if sh(cont, 900).returncode == 0:
            return f"finished an interrupted {'rebase' if in_rebase else 'merge'} ({len(unmerged)} generated/locale conflict(s) regenerated)"
    sh(["git", "rebase", "--abort"] if in_rebase else ["git", "merge", "--abort"], 120)
    return f"aborted an interrupted {'rebase' if in_rebase else 'merge'} left by a restart (branch restored; no commit lost)"


def _is_locale(path: str) -> bool:
    return path.startswith("config/locales/") and path.endswith((".yml", ".yaml"))


def _preview_error_text(page: str) -> str:
    """The exception and the app-frame backtrace lines from a Rails error page."""
    import html as _html
    text = _html.unescape(re.sub(r"<[^>]+>", "\n", page or ""))
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    keep = [ln for ln in lines if re.search(r"Error|Exception|uninitialized|undefined|^(app|test|lib|config)/\S+:\d+", ln)]
    return (screenshots.exception_summary(page) + "\n\n" + "\n".join(dict.fromkeys(keep))[:3000]).strip()


def _gate_fix_task(tdir: pathlib.Path, reason: str, manifest: pathlib.Path, cfg, key: str, wt) -> str:
    """Turn a deterministic gate failure into a bounded task over the ticket's changed files."""
    gates_out = tdir / "gates.out"
    evidence = gates_out.read_text(errors="replace") if gates_out.exists() else reason
    evidence = evidence or reason
    existing = contracts.load_tasks(manifest)
    files = _sh(["git", "diff", "--name-only", "origin/main...HEAD"], wt, 60).stdout.split()
    if not files:
        files = sorted({path for task in existing for path in task.allowed_files})
    if not files:
        raise ValueError("the ticket has no changed or task-owned files to fix")
    for task in existing:
        if task.slug.startswith("gate-fix-") and task.allowed_files == files and not _task_accepted(cfg, key, task):
            return task.id
    tid = _next_runner_task_id(manifest)
    cmd = "bin/rails test test/views/components/zui/"
    _append_task(manifest, {
        "id": tid, "slug": f"gate-fix-{tid}",
        "summary": "Fix the deterministic gate failure (see feedback)",
        "allowed_files": files, "may_edit_tests": True, "visual": False,
        "verification_commands": [cmd],
        "acceptance": [f"AC-1: `{cmd}` passes"],
        "invariants": ["Fix the code that failed the deterministic gate.",
                       "Do not weaken any test or lint rule."],
        "stop_when": ["the verification commands pass"],
    })
    feedback = cfg.state_root / "attempts" / key / tid / "feedback.md"
    feedback.parent.mkdir(parents=True, exist_ok=True)
    feedback.write_text(f"# Deterministic gate failure\n\n```\n{evidence[-6000:]}\n```\n")
    return tid


def _preview_fix_task(tdir: pathlib.Path, reason: str, manifest: pathlib.Path, wt) -> str:
    """A Lookbook preview raised: one bounded worker task over the ticket's own files, with the
    exception as evidence. The visual gate re-renders it when gates run again."""
    error = (tdir / "preview-error.txt").read_text() if (tdir / "preview-error.txt").exists() else reason
    files = [f for f in _sh(["git", "diff", "--name-only", "origin/main...HEAD"], wt, 60).stdout.split()
             if f.startswith(("app/", "test/", "config/locales/"))]
    if not files:
        raise ValueError("the ticket has no changed application files to fix")
    tid = _next_runner_task_id(manifest)
    tests = sorted({f for f in files if f.startswith("test/") and f.endswith("_test.rb")})
    cmd = "bin/rails test " + (" ".join(tests) if tests else "test/views/components/zui/")
    _append_task(manifest, {
        "id": tid, "slug": f"preview-fix-{tid}",
        "summary": "Fix the exception the component's Lookbook preview raised",
        "allowed_files": files, "may_edit_tests": True, "visual": False,
        "verification_commands": [cmd],
        "acceptance": ["AC-1: the preview renders without raising (the visual gate re-checks it)",
                       f"AC-2: `{cmd}` passes"],
        "invariants": ["The Lookbook preview raised:\n" + error[:1500],
                       "Fix the cause in the component or preview code; add a test that would have caught it.",
                       "Do not weaken or delete existing tests."],
    })
    return tid


def _lint_fix_task(tdir: pathlib.Path, reason: str, manifest: pathlib.Path, cfg=None, key: str = "") -> str:
    """Turn named pre-commit offenses into one bounded fix task: allowlist is exactly the offending
    files, and the gate is the same lint the hook runs. Runner-assigned id (9nn), like review fixes."""
    import re as _re
    # Prefer the full captured output over the truncated reason string.
    full = (tdir / "hook-reject.out")
    if full.exists() and len(full.read_text()) > len(reason):
        reason = reason + "\n" + full.read_text()
    files = sorted({m.group(1) for m in _re.finditer(r"([\w./-]+\.(?:rb|rake)):\d+:\d+:", reason)})
    if not files:
        # Prettier names each offending file on its own `[warn] <path>` line. Its other output is not a
        # file list: the command line names every staged file, and the non-TTY progress lines run
        # checked paths together with no separator (ZIP-4281/901).
        files = sorted({m.group(1) for m in _re.finditer(
            r"\[warn\] ((?:app|test|lib|config|linters)/[\w./-]+\.(?:ts|js|tsx|jsx|scss|css))\s*$", reason, _re.M)})
    if not files:
        # Other formatters name the file without line:col.
        files = sorted({m.group(1) for m in _re.finditer(
            r"\b((?:app|test|lib|config|linters)/[\w./-]+\.(?:ts|js|tsx|jsx|scss|css|rb|rake|slim))\b", reason)})
    if not files:
        raise ValueError("no offending file could be parsed from the hook output")
    reek_warning = r"\[\d+(?:,\s*\d+)*\]:\w+:"
    offenses = "\n".join(f"  {ln.strip()}" for ln in reason.splitlines()
                         if _re.search(r":\d+:\d+:|" + reek_warning, ln))[:1500]
    import contracts as _contracts
    import worktree as _worktree
    existing = _contracts.load_tasks(manifest) if manifest.exists() else []
    for tk in existing:
        if tk.slug.startswith("lint-fix-") and tk.allowed_files == files and not (cfg and _task_accepted(cfg, key, tk)):
            return tk.id                                  # the same fix is already queued
    tid = _next_runner_task_id(manifest)
    if (all(f.endswith((".ts", ".js", ".tsx", ".jsx")) for f in files)
            and _re.search(r"^\s*\U0001f94a\s+eslint(?:\s|$)", reason, _re.M)):
        cmd = "bin/agent_run yarn eslint " + " ".join(files) + " --no-warn-ignored --max-warnings=0"
    elif all(f.endswith((".ts", ".js", ".tsx", ".jsx", ".scss", ".css")) for f in files):
        cmd = "bin/agent_run yarn prettier --check " + " ".join(files)
    elif _re.search(reek_warning, reason):
        cmd = "bin/agent_run bundle exec reek --force-exclusion " + " ".join(files)
    else:
        cmd = "bin/agent_run rubocop --cache false --force-exclusion " + " ".join(files)
    _append_task(manifest, {
        "id": tid, "slug": f"lint-fix-{tid}", "size": "small",
        "summary": "Fix the lint offenses the pre-commit hook rejected",
        "allowed_files": files,
        "may_edit_tests": bool(cfg and any(_worktree._match(f, g) for f in files for g in cfg.test_path_globs)),
        "verification_commands": [cmd],
        "acceptance": [f"AC-1: `{cmd}` reports no offenses"],
        "invariants": [
            "The pre-commit hook rejected the commit with these offenses:\n" + offenses,
            *( ["`.lefthook/pre-commit/jquery-limit` greps only ADDED lines (`^+`) for `$(`, so an "
                "EXISTING jQuery line that you reformatted or moved now counts as new. Preserving that "
                "line byte-for-byte satisfies the hook; rewriting legacy jQuery in an untouched file is "
                "not this task's job."] if "jquery" in reason.lower() else [] ),
            "Fix the code so the lint passes. Prefer the repo's existing idiom for the cop; an inline "
            "disable is acceptable ONLY where the cop is genuinely inapplicable (e.g. Rails/FindEach on "
            "a Capybara collection, which has no find_each) and must carry a `--` reason.",
            "Do not change behaviour or assertions: this task only satisfies the linter.",
        ],
    })
    return tid


def _next_runner_task_id(manifest: pathlib.Path) -> str:
    import re as _re
    used = {int(m) for m in _re.findall(r'^id = "(9\d\d)"', manifest.read_text(), _re.M)} if manifest.exists() else set()
    n = 901
    while n in used:
        n += 1
    return str(n)


def _waiting_since(tdir: pathlib.Path, t) -> str:
    """' (waiting 3d)' -- a standing block should read as older every time it is raised."""
    p = pathlib.Path(tdir) / "state.json"
    if not p.exists():
        return ""
    hours = (time.time() - p.stat().st_mtime) / 3600
    if hours < 1:
        return ""
    return f" (waiting {int(hours)}h)" if hours < 48 else f" (waiting {int(hours // 24)}d)"


def _unmet_review_promises(cfg, key: str, wt) -> dict:
    """Comment -> fix task for every reply that promised code the tree does not contain yet. A reply
    is a public commitment; the gate must not open on an unkept one (ZIP-7874, 2026-09-21)."""
    p = cfg.ticket_dir(key) / "pending-fixes.json"
    if not p.exists():
        return {}
    promised = json.loads(p.read_text())
    if not promised:
        return {}
    manifest = _manifest_path(wt, key)
    tasks = {t.id: t for t in (contracts.load_tasks(manifest) if manifest.exists() else [])}
    unmet = {cid: (tid or "<no task queued>") for cid, tid in promised.items()
             if not tid or tid not in tasks or not _task_accepted(cfg, key, tasks[tid])}
    if not unmet:
        p.write_text("{}")
    return unmet


def _record_promise(cfg, key: str, comment_id: str, task_id: str = "") -> None:
    """Remember that a public reply promised code for `comment_id`. Written at reply time with no
    task, then bound to the fix task when one is queued; the gate refuses while any is unbound or
    unaccepted, and a promise is only cleared by accepted work."""
    p = cfg.ticket_dir(key) / "pending-fixes.json"
    cur = json.loads(p.read_text()) if p.exists() else {}
    if task_id or comment_id not in cur:
        cur[comment_id] = task_id or cur.get(comment_id, "")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cur, indent=2))

