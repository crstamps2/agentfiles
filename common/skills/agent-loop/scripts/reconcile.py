"""Reconcile part 1: finalization and the three idempotent projections (history, metrics,
lifecycle). Per the design's "one rule": `attempt.json` is the single source of truth;
these functions recompute each projection idempotently from a CLASSIFIED/FINALIZED
`attempt.Record` and are safe to re-run after a crash at any point.
"""
from __future__ import annotations
import dataclasses
import json
import os
import pathlib
import stat
import subprocess
import sys
import tomllib

import attempt as attempt_mod
import contracts
import ladder as ladder_mod
import locks
import metrics as metrics_mod
import procid as procid_mod
import procs as procs_mod
import state as state_mod
import worktree as worktree_mod


class FinalizeFailed(RuntimeError):
    """Raised when a restore-required outcome does not converge to base_tree."""
    pass


# ---------------------------------------------------------------------------
# Test-only crash-simulation seam (Plan 1c Task 9). `_CRASH_HOOK` is None in every
# production path and is only ever set by a test via `unittest.mock.patch("reconcile._CRASH_HOOK",
# "<boundary-name>")`; `_maybe_crash` is a pure no-op unless that name matches, so this seam is
# unreachable outside tests. It lets test_crash_windows.py kill the runner/reconcile at an exact
# instruction boundary (named below) instead of a real process kill, then run `reconcile()` (or
# the `clear-fence` CLI) and assert the post-condition -- see the design's crash-window
# acceptance table. Boundaries instrumented: after-created, after-launching, after-running,
# after-stage-done, after-classifying, after-classified (all in runner.py's `_attempt`),
# after-restore, after-finalized, after-history, after-metrics, after-lifecycle, after-fencing,
# after-fence-write, after-orphaned (all in this module), and clear-fence-after-transition (in
# `clear_fence`).
_CRASH_HOOK = None


class SimulatedCrash(BaseException):
    """Raised by `_maybe_crash` when the test-only hook name matches `_CRASH_HOOK`. Subclasses
    BaseException (not Exception) so it is never accidentally swallowed by an `except Exception`
    in the code under test -- exactly like a real process kill, it must propagate all the way
    out to the test."""
    pass


def _maybe_crash(name: str) -> None:
    if _CRASH_HOOK is not None and _CRASH_HOOK == name:
        raise SimulatedCrash(f"simulated crash at {name!r}")


RESTORE_OUTCOMES = {"rejected", "protocol", "blocked"}


def _ticket_key(rec: attempt_mod.Record) -> str:
    r"""`lineage` is `<ticket>/<task.id>`; the ticket key is everything before the LAST `/`.
    `task.id` is validated as `^\d{3}$` (always the final path component), but a ticket
    key is not forbidden from containing `/` itself, so split from the right."""
    return rec.lineage.rsplit("/", 1)[0]


def _task_id(rec: attempt_mod.Record) -> str:
    _, _, rest = rec.lineage.rpartition("/")
    return rest or rec.lineage


def _diff_patch(wt, base_tree: str, observed_tree: str) -> str:
    """Text patch for the attempt record. `--binary` keeps binary blobs (a worker committed PNG
    screenshot fixtures, 2026-09-14) as printable base85 so the patch stays UTF-8 and re-applyable;
    decoding is lenient regardless so a stray byte can never crash finalize."""
    r = subprocess.run(["git", "-C", str(wt), "diff", "--no-color", "--binary", base_tree, observed_tree],
                       capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"git diff failed: {r.stderr.decode('utf-8', 'replace').strip()}")
    return r.stdout.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# finalize(): CLASSIFIED -> FINALIZED.
# ---------------------------------------------------------------------------

def finalize(cfg, rec: attempt_mod.Record) -> attempt_mod.Record:
    """Restore the worktree to base_tree iff `rec.outcome` is one that discards the
    worker's tree (rejected/protocol/blocked); otherwise keep it. `diff.patch` is always
    regenerated as `git diff base_tree observed_tree` (regenerable, so overwriting it is
    always safe) before the record transitions to FINALIZED.

    Requires `rec.observed_tree` to already be set (recorded at CLASSIFYING, before any
    restore, per the in-run path in Task 6; the recovery sweep in Task 5 must record it
    before classifying). Snapshotting it here -- after a possible crash on a prior
    finalize() attempt that already restored the tree -- would silently diff base_tree
    against itself and lose the worker's pre-restore diff forever, so we refuse instead of
    guessing: no restore, no patch write, and the record stays CLASSIFIED."""
    if rec.status != "CLASSIFIED":
        raise attempt_mod.IllegalAttemptTransition(
            f"finalize requires CLASSIFIED, got {rec.status}")
    if rec.observed_tree is None:
        raise FinalizeFailed(
            f"{rec.attempt_id}: observed_tree missing; record must be classified with "
            f"observed_tree set before finalize")

    wt = pathlib.Path(rec.worktree)
    observed = rec.observed_tree

    if rec.outcome in RESTORE_OUTCOMES:
        worktree_mod.restore(wt, rec.base_tree)
        if not worktree_mod.verify_restored(wt, rec.base_tree):
            raise FinalizeFailed(
                f"{rec.attempt_id}: worktree did not converge to base_tree after restore")
        tree = "restored"
        _maybe_crash("after-restore")
    else:
        tree = "kept"

    patch = _diff_patch(wt, rec.base_tree, observed)
    attempt_mod.safe_rewrite(pathlib.Path(rec.path) / "diff.patch", patch)

    rec = attempt_mod.transition(rec, "FINALIZED", tree=tree)
    _maybe_crash("after-finalized")
    return rec


# ---------------------------------------------------------------------------
# Projections. Each is a no-op if its flag is already set (recovery only calls a
# projection whose flag is false; in-run callers may call all three unconditionally).
# ---------------------------------------------------------------------------

def project_history(cfg, rec: attempt_mod.Record) -> attempt_mod.Record:
    """`state.json.attempts[lineage]` gets `{n, rung, outcome, reason, arm, attempt_id,
    generation}` iff no entry with this `attempt_id` already exists. Idempotent by
    attempt_id dedupe; short-circuits entirely if `history` is already true."""
    if rec.history:
        return rec
    tdir = cfg.ticket_dir(_ticket_key(rec))
    t = state_mod.load(tdir)
    entries = t.attempts.setdefault(rec.lineage, [])
    if not any(e.get("attempt_id") == rec.attempt_id for e in entries):
        entries.append({
            "n": rec.n,
            "rung": rec.rung,
            "outcome": rec.outcome,
            "reason": rec.reason,
            "arm": rec.arm,
            "attempt_id": rec.attempt_id,
            "generation": rec.generation,
        })
        state_mod.save(tdir, t)
    _maybe_crash("after-history")
    return attempt_mod.set_flags(rec, history=True)


def _metrics_row(rec: attempt_mod.Record, stage: dict) -> dict:
    # Worker stages carry the attempt's token/cost usage (from the pi session log) so metrics.jsonl
    # is a usable ledger on its own; verify stages run no model and get zeros.
    usage = {}
    if stage.get("kind") == "worker":
        try:
            import ledger
            usage = ledger.attempt_usage(pathlib.Path(rec.path))
        except Exception:  # noqa: BLE001 -- the ledger must never break projection
            usage = {}
    return {**usage,
        "attempt_id": rec.attempt_id,
        "stage_kind": stage.get("kind"),
        "idx": stage.get("idx"),
        "run_id": rec.run_id,
        "ticket": _ticket_key(rec),
        "task_id": _task_id(rec),
        "agent": rec.agent,
        "model": rec.model,
        "tier": rec.tier or (rec.rung or {}).get("tier"),
        "arm": rec.arm,
        "attempt": rec.n,
        "outcome": rec.outcome,
        "reason": rec.reason,
        "elapsed_s": stage.get("elapsed_s"),
        "terminated": stage.get("terminated"),
        "timed_out": stage.get("timed_out"),
        "evidence_path": str(rec.path),
    }


def project_metrics(cfg, rec: attempt_mod.Record) -> attempt_mod.Record:
    """One `metrics.jsonl` row per entry in `rec.stages`, keyed by (attempt_id, stage_kind,
    idx); appended iff absent. Idempotent per stage; short-circuits entirely if `published`
    is already true."""
    if rec.published:
        return rec
    for stage in rec.stages:
        kind, idx = stage.get("kind"), stage.get("idx")
        if not metrics_mod.has(cfg.state_root, rec.attempt_id, kind, idx):
            metrics_mod.append(cfg.state_root, _metrics_row(rec, stage))
    _maybe_crash("after-metrics")
    return attempt_mod.set_flags(rec, published=True)


def project_lifecycle(cfg, rec: attempt_mod.Record) -> attempt_mod.Record:
    """Replay the persisted `next_action` onto the ticket's lifecycle state: `block` ->
    blocked, `pause-env`/`fence` -> paused, `none` -> nothing. Idempotent: never
    re-transitions a ticket that is already there (avoids IllegalTransition on replay).
    Short-circuits entirely if `lifecycle` is already true.

    `paused` only accepts a transition back to `t.previous` (see state.EDGES), so a
    `block` replayed onto a paused ticket must first unwind to `previous` in memory before
    moving on to `blocked`; both transitions are saved as a single `save()` so a crash
    between them can't leave a `previous`-unwound-but-unsaved ticket on disk. A `blocked`
    ticket never gets an env-pause -- it's already the more severe state -- so pause-env/
    fence on a blocked ticket is a no-op, not an IllegalTransition."""
    if rec.lifecycle:
        return rec
    tdir = cfg.ticket_dir(_ticket_key(rec))
    if rec.next_action == "block":
        t = state_mod.load(tdir)
        if t.state != "blocked":
            if t.state == "paused" and t.previous:
                t = state_mod.transition(t, t.previous, reason=t.reason)
            t = state_mod.transition(t, "blocked", reason=rec.reason or "")
            state_mod.save(tdir, t)
    elif rec.next_action in ("pause-env", "fence"):
        t = state_mod.load(tdir)
        if t.state not in ("blocked", "paused"):
            t = state_mod.transition(t, "paused", reason=rec.reason or "")
            state_mod.save(tdir, t)
    # "none" -> nothing to replay.
    _maybe_crash("after-lifecycle")
    return attempt_mod.set_flags(rec, lifecycle=True)


def project_all(cfg, rec: attempt_mod.Record) -> attempt_mod.Record:
    """Run all three projections and then, if all flags are now true and the record is
    FINALIZED, advance it to PROJECTED. Order among the three does not matter (each is
    independently idempotent and `next_action` makes lifecycle order-free)."""
    rec = project_history(cfg, rec)
    rec = project_metrics(cfg, rec)
    rec = project_lifecycle(cfg, rec)
    return attempt_mod.maybe_project(rec)


# ---------------------------------------------------------------------------
# Reconcile part 2: FenceExit, fence()/interrupt(), the global sweep, and the
# RunContext produced by reconcile(). See the design's "Recovery: global, before
# any dispatch" section.
# ---------------------------------------------------------------------------

class FenceExit(SystemExit):
    """Raised whenever recovery cannot proceed without an operator: exits the process with
    code 3 (fail closed). `.reason` carries the human-readable cause; `.code` is always 3
    regardless of the reason text, so callers can distinguish "this is a fence exit" from an
    ordinary SystemExit purely by `isinstance`, and every caller can assert `.code == 3`."""
    def __init__(self, reason: str):
        super().__init__(3)
        self.reason = reason


def _fence_path(cfg) -> pathlib.Path:
    return pathlib.Path(cfg.state_root) / "locks" / "heavy.fence"


def fence(cfg, rec: attempt_mod.Record, reason: str, proc: dict | None = None) -> None:
    """Transition the attempt FENCING -> write the fence file (safe_write: O_EXCL, never
    overwrites) -> transition ORPHANED -> raise FenceExit. The two transitions bracket the
    single admitted cross-file ordering (fence-file-then-ORPHANED); a crash between them
    leaves FENCING with no fence file, which the sweep (and the global fence check) both
    treat as "exit 3" on the next run -- never silently resolved.

    `proc`, if given, is written into the fence file instead of `rec.proc`. This matters
    because `_attempt` transitions a stage to STAGE_DONE with `proc=None` (the stage's own
    proc dict is preserved on the stage entry, not on the record) before ever calling
    fence() on an unverified termination -- without this parameter, the fence would
    serialize `rec.proc` (already null at that point) and the next reconcile would treat
    the fence as corrupt ("proc must be a dict, not null") forever, instead of fencing the
    actual process group."""
    # C1: the fenced identity lives on the record too, not only in the fence file -- so an
    # ORPHANED record whose fence file is lost still classifies its real group (not None→dead).
    rec = attempt_mod.transition(rec, "FENCING", proc=(proc if proc is not None else rec.proc))
    _maybe_crash("after-fencing")
    fp = _fence_path(cfg)
    fp.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"attempt_dir": str(rec.path), "proc": proc if proc is not None else rec.proc, "reason": reason})
    try:
        attempt_mod.safe_write(fp, payload)
    except OSError as e:
        # The FENCING record already guarantees the next run exits 3 (see
        # _reconcile_one's FENCING-with-no-fence-file branch), so a write failure here is
        # safe to surface as an ordinary FenceExit rather than a raw exception.
        raise FenceExit(f"fence write failed: {e}")
    _maybe_crash("after-fence-write")
    attempt_mod.transition(rec, "ORPHANED")
    _maybe_crash("after-orphaned")
    raise FenceExit(reason)


def interrupt(cfg, rec: attempt_mod.Record) -> attempt_mod.Record:
    """Drive a non-terminal attempt found with no live group to INTERRUPTED: verify repo_id
    (a mismatch means the worktree at this path is no longer the repo this attempt belongs
    to -- exit 3, nothing restored), snapshot the tree as found, restore to base_tree, and
    verify the restore converged before recording anything.

    Sequencing note: a single INTERRUPTED transition carries `observed_tree`, `outcome`,
    `next_action`, and `tree="restored"` together, written only AFTER restore+verify
    succeed in memory -- two INTERRUPTED transitions are illegal (attempt.NEXT has no
    INTERRUPTED -> INTERRUPTED edge), so the observed/outcome/tree fields cannot be split
    across two writes. The one accepted loss this creates: if the process crashes after a
    successful restore+verify but before this transition is durably written, the pre-restore
    diff for that specific window is lost on the next run (the tree is re-snapshotted, which
    now equals base_tree, restore/verify are harmless no-ops, and the transition proceeds --
    but the pre-restore diff itself is gone). This is distinct from -- and narrower than --
    the CLASSIFIED/FINALIZED window that `observed_tree` already fixes for the normal path
    (finalize() requires observed_tree to be set before it ever runs)."""
    wt = pathlib.Path(rec.worktree)
    try:
        actual_repo_id = attempt_mod._repo_id(wt)
    except ValueError as e:
        raise FenceExit(f"{rec.attempt_id}: repo_id check failed: {e}")
    if actual_repo_id != rec.repo_id:
        raise FenceExit(f"{rec.attempt_id}: repo_id mismatch: recorded {rec.repo_id!r} "
                        f"!= actual {actual_repo_id!r} for {wt}")
    observed = worktree_mod.snapshot(wt)
    worktree_mod.restore(wt, rec.base_tree)
    if not worktree_mod.verify_restored(wt, rec.base_tree):
        raise FenceExit(f"{rec.attempt_id}: restore did not converge to base_tree")
    _maybe_crash("after-restore")
    rec = attempt_mod.transition(rec, "INTERRUPTED", observed_tree=observed, outcome="interrupted",
                                 next_action="none", tree="restored")
    _ensure_diff_patch(rec)
    return project_all(cfg, rec)


def _ensure_diff_patch(rec: attempt_mod.Record) -> None:
    """Any INTERRUPTED record found without a diff.patch (an interrupt() written before this
    fix existed, or a hand-built test record) gets it regenerated -- it is always derivable
    from base_tree/observed_tree, so writing it late is always safe. However, if observed_tree
    is None (legacy record), the patch cannot be regenerated safely -- an operator must inspect."""
    patch_path = pathlib.Path(rec.path) / "diff.patch"
    if patch_path.exists():
        return
    if not rec.observed_tree:
        raise FenceExit(
            f"cannot regenerate diff.patch for {rec.attempt_id}: observed_tree missing")
    wt = pathlib.Path(rec.worktree)
    patch = _diff_patch(wt, rec.base_tree, rec.observed_tree)
    attempt_mod.safe_rewrite(patch_path, patch)


def _load_task(adir: pathlib.Path) -> contracts.Task:
    try:
        text = attempt_mod.safe_read(adir / "task.toml")
        d = tomllib.loads(text)
    except (attempt_mod.UnsafePath, tomllib.TOMLDecodeError, OSError) as e:
        raise FenceExit(f"corrupt task.toml: {adir}: {e}")
    field_names = {f.name for f in dataclasses.fields(contracts.Task)}
    return contracts.Task(**{k: v for k, v in d.items() if k in field_names})


def _ladder_history(cfg, rec: attempt_mod.Record) -> tuple[list, int]:
    """Prior attempts on this lineage, as ladder.Attempt objects, plus the trailing run of
    `environment` outcomes -- the same shape the in-run path threads through `next_action`."""
    tdir = cfg.ticket_dir(_ticket_key(rec))
    t = state_mod.load(tdir)
    entries = t.attempts.get(rec.lineage, [])
    history = [ladder_mod.Attempt(ladder_mod.Rung(**e["rung"]), e["outcome"])
               for e in entries if e.get("outcome") in ladder_mod.OUTCOMES]
    env_failures = 0
    for e in reversed(entries):
        if e.get("outcome") == "environment":
            env_failures += 1
        else:
            break
    return history, env_failures


def _reap_proc(cfg, rec: attempt_mod.Record) -> None:
    """For a LAUNCHING/RUNNING/STAGE_DONE record with a recorded proc: classify it. Only
    `dead` (either immediately, or after we kill an `ours-alive` group and it dies) lets the
    caller proceed; `unknown`, or `ours-alive` that survives the kill attempt, is fenced --
    never signaled twice, never assumed dead without re-checking."""
    status = procid_mod.classify(procid_mod.ProcId.from_dict(rec.proc))
    if status == "ours-alive":
        procs_mod.kill_group(rec.proc["pgid"])
        status = procid_mod.classify(procid_mod.ProcId.from_dict(rec.proc))
    if status != "dead":
        fence(cfg, rec, f"{rec.attempt_id}: proc is {status} after recovery attempt")


def _reconcile_stage_done(cfg, rec: attempt_mod.Record) -> attempt_mod.Record:
    """A STAGE_DONE record whose last stage timed out with a clean allowlist keeps the
    parent spec's timeout-keeps-tree rule: classify it as `timeout` ourselves (recovery, not
    the runner, is doing the classifying here) and finalize -- the tree is KEPT, not
    restored. Anything else in STAGE_DONE (no timeout, or a timeout with violations) becomes
    INTERRUPTED like any other non-terminal record."""
    last_stage = rec.stages[-1] if rec.stages else None
    if last_stage and last_stage.get("timed_out"):
        wt = pathlib.Path(rec.worktree)
        changed = worktree_mod.changed_paths(wt, rec.base_tree)
        task = _load_task(pathlib.Path(rec.path))
        violations = worktree_mod.check_allowlist(changed, task, cfg.protected_paths, cfg.test_path_globs, wt=wt, harness_globs=getattr(cfg, 'harness_artifact_globs', ()))
        if not violations:
            observed = worktree_mod.snapshot(wt)
            rec = attempt_mod.transition(rec, "CLASSIFYING", observed_tree=observed)
            history, env_failures = _ladder_history(cfg, rec)
            na = ladder_mod.next_action(history, rec.arm, "timeout", env_failures)
            rec = attempt_mod.transition(rec, "CLASSIFIED", outcome="timeout",
                                         reason="stage timeout (recovered)",
                                         changed_paths=changed, violations=violations,
                                         next_action=na)
            rec = finalize(cfg, rec)
            return project_all(cfg, rec)
    return interrupt(cfg, rec)


def _reconcile_orphaned(cfg, rec: attempt_mod.Record) -> attempt_mod.Record:
    """By the time the sweep reaches an ORPHANED record, the global fence check (step 1 of
    reconcile()) has already resolved (or exited on) any fence file that exists -- so an
    ORPHANED record encountered here has no fence protecting it. Re-fence unless the group
    has since died, in which case complete the INTERRUPTED path directly."""
    status = procid_mod.classify(procid_mod.ProcId.from_dict(rec.proc) if rec.proc else None)
    if status == "dead":
        return interrupt(cfg, rec)
    fence(cfg, rec, f"{rec.attempt_id}: ORPHANED re-fenced (fence file missing)")


def _reconcile_one(cfg, rec: attempt_mod.Record) -> attempt_mod.Record:
    if rec.status == "FENCING":
        raise FenceExit(f"{rec.attempt_id}: found FENCING with no fence file (torn fence write)")
    if rec.status in ("LAUNCHING", "RUNNING", "STAGE_DONE") and rec.proc:
        _reap_proc(cfg, rec)   # raises FenceExit unless the group is now provably dead
    if rec.status == "STAGE_DONE":
        return _reconcile_stage_done(cfg, rec)
    if rec.status in ("CREATED", "LAUNCHING", "RUNNING", "CLASSIFYING"):
        return interrupt(cfg, rec)
    if rec.status == "CLASSIFIED":
        rec = finalize(cfg, rec)
        return project_all(cfg, rec)
    if rec.status == "FINALIZED":
        return project_all(cfg, rec)
    if rec.status == "ORPHANED":
        return _reconcile_orphaned(cfg, rec)
    if rec.status == "INTERRUPTED":
        _ensure_diff_patch(rec)
        return project_all(cfg, rec)
    if rec.status == "PROJECTED":
        return project_all(cfg, rec)
    raise FenceExit(f"{rec.attempt_id}: unhandled status {rec.status}")


class CorruptFence(ValueError):
    """Raised by `_parse_fence` for any fence file that fails schema validation: unparseable
    JSON, not an object, a missing required key, an `attempt_dir` that isn't a non-empty str
    resolving under `attempts/`, a `proc` that isn't a dict shaped like a `ProcId`, or a
    `reason` that isn't a str. Normalizes every malformed-fence failure mode so callers
    never see a bare TypeError/KeyError escape from deep inside path or proc handling."""
    pass


def _parse_fence(cfg, text: str) -> tuple[pathlib.Path, dict, str]:
    """Parse and strictly schema-validate a fence file's contents into `(resolved
    attempt_dir, proc dict, reason str)`. Shared by `_check_global_fence` and `clear_fence`
    so both paths fail closed identically on a malformed fence rather than one of them
    letting a TypeError escape past the fail-closed contract."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise CorruptFence(f"unparseable: {e}")
    if not isinstance(data, dict):
        raise CorruptFence("not an object")
    required = ("attempt_dir", "proc", "reason")
    if any(k not in data for k in required):
        raise CorruptFence(f"missing required key(s), need {required}")
    attempt_dir = data["attempt_dir"]
    if not isinstance(attempt_dir, str) or not attempt_dir:
        raise CorruptFence(f"attempt_dir must be a non-empty str, got {attempt_dir!r}")
    proc = data["proc"]
    if not isinstance(proc, dict):
        raise CorruptFence(f"proc must be a dict, got {type(proc).__name__}")
    try:
        procid_mod.ProcId.from_dict(proc)
    except ValueError as e:
        raise CorruptFence(f"invalid proc shape: {e}")
    reason = data["reason"]
    if not isinstance(reason, str):
        raise CorruptFence(f"reason must be a str, got {type(reason).__name__}")
    attempts_root = (pathlib.Path(cfg.state_root) / "attempts").resolve()
    try:
        adir = pathlib.Path(attempt_dir).resolve()
    except OSError as e:
        raise CorruptFence(f"unresolvable attempt_dir: {e}")
    try:
        adir.relative_to(attempts_root)
    except ValueError:
        raise CorruptFence(f"attempt_dir {adir} is outside {attempts_root}")
    return adir, proc, reason


def _check_global_fence(cfg) -> None:
    """Step 1 of recovery. If `locks/heavy.fence` exists: classify its proc. `ours-alive` or
    `unknown` -> exit 3 (an operator must run clear-fence). `dead` -> the referenced attempt
    (which must still be ORPHANED) is driven through INTERRUPTED and only then is the fence
    file removed -- the fence is never simply deleted out from under a still-ORPHANED
    record. Any malformed field (bad JSON, missing attempt_dir, an attempt_dir that doesn't
    resolve under `attempts/`, or a referenced attempt that isn't ORPHANED or is unreadable)
    is corrupt -> exit 3."""
    fp = _fence_path(cfg)
    try:
        text = fp.read_text()
    except FileNotFoundError:
        return
    try:
        adir, proc, reason = _parse_fence(cfg, text)
    except CorruptFence as e:
        raise FenceExit(f"corrupt fence: {e}")
    status = procid_mod.classify(procid_mod.ProcId.from_dict(proc))
    if status in ("ours-alive", "unknown"):
        raise FenceExit(f"fenced: referenced proc is {status}")
    try:
        rec = attempt_mod.load(adir)
    except attempt_mod.UnreadableRecord as e:
        raise FenceExit(f"corrupt fence: unreadable referenced attempt: {e}")
    if rec.status == "ORPHANED":
        interrupt(cfg, rec)
    elif rec.status == "INTERRUPTED":
        # Idempotent: a crash between the INTERRUPTED write and the fence unlink in a prior
        # run leaves exactly this state on the next run. Project (no-op if already done)
        # and unlink the fence; do not reject an already-recovered attempt.
        project_all(cfg, rec)
    else:
        raise FenceExit("corrupt fence: referenced attempt is neither ORPHANED nor "
                        f"INTERRUPTED (status={rec.status})")
    fp.unlink()


def _leaf_dirs(attempts_root) -> list:
    r"""Every attempt leaf directory under `attempts_root`, found by walking the tree rather
    than assuming a fixed `*/*/*` depth (a ticket key may itself contain `/`, per
    `_ticket_key`'s docstring, which would otherwise make a fixed-depth glob miss it -- or
    miss an attempt dir created before `attempt.json` existed at all, since a glob on
    `attempt.json` can't see a dir that never got one).

    A leaf is any all-digits-named directory (an attempt number `n`) that has no further
    all-digits-named subdirectory to descend into -- this is what distinguishes the actual
    `n` dir from the task-id dir one level up (task ids are also all-digits, matching
    `^\d{3}$`, so naming alone can't tell them apart; the task-id dir always has a further
    digit-named child -- the `n` dir -- while the `n` dir itself never does)."""
    attempts_root = pathlib.Path(attempts_root)
    if not attempts_root.is_dir():
        return []
    leaves = []
    for dirpath, dirnames, _filenames in os.walk(attempts_root):
        p = pathlib.Path(dirpath)
        if p == attempts_root:
            continue
        if not p.name.isdigit():
            continue
        if any(d.isdigit() for d in dirnames):
            continue  # a digit-named subdir remains (the real n dir); this is the task-id dir
        leaves.append(p)
    return leaves


def _sweep(cfg) -> None:
    """Step 2 of recovery: every attempt leaf dir under `attempts/`, all tickets, all tasks,
    sorted by leaf mtime. A record this recovery cannot understand (unreadable/corrupt/
    legacy), or a leaf directory with no `attempt.json` at all (or a non-regular one -- e.g.
    a crash between mkdir and the first write, or a booby-trapped symlink), is never
    skipped -- it exits 3 and waits for an operator."""
    attempts_root = pathlib.Path(cfg.state_root) / "attempts"
    leaves = _leaf_dirs(attempts_root)
    for leaf in leaves:
        record_path = leaf / "attempt.json"
        try:
            st = os.lstat(record_path)
        except FileNotFoundError:
            raise FenceExit(f"attempt dir without record: {leaf}")
        if not stat.S_ISREG(st.st_mode):
            raise FenceExit(f"attempt dir without record: {leaf}")
    leaves.sort(key=lambda p: p.stat().st_mtime)
    for adir in leaves:
        try:
            rec = attempt_mod.load(adir)
        except attempt_mod.UnreadableRecord as e:
            raise FenceExit(f"unreadable record: {adir}: {e}")
        _reconcile_one(cfg, rec)


_TOKEN = object()
"""Sentinel identifying a RunContext actually produced by reconcile(). A hand-built
RunContext(cfg, run_id, lease) leaves `_token` at its default (None), which never matches
this object -- implement_task uses that to refuse any RunContext it did not itself obtain
from reconcile()."""


@dataclasses.dataclass
class RunContext:
    """The only way to obtain a *valid* one is `reconcile(cfg, run_id)`, which stamps
    `_token` with the module-level `_TOKEN` sentinel. Holds the global runner lease for the
    life of the run; the caller releases it via `close()` (or the lease's own
    context-manager protocol) once done. `closed` is set by `close()` so a caller that
    stashes a released RunContext and reuses it against `implement_task` is refused."""
    cfg: object
    run_id: str
    lease: locks.Lease
    closed: bool = False
    _token: object = None

    def close(self) -> None:
        self.closed = True
        self.lease.release()


def reconcile(cfg, run_id) -> RunContext:
    """The single global recovery entry point, run under the global runner lease before any
    ticket selection, admission, or dispatch. `_recover` no longer exists: this is the one
    `reconcile(state_root)` the design calls for, and `implement_task` requires a
    `RunContext` produced by it."""
    lease = locks.Lease(pathlib.Path(cfg.state_root) / "locks" / "runner", "runner")
    if not lease.acquire(hold=True):
        raise FenceExit("runner live")
    try:
        _check_global_fence(cfg)
        _sweep(cfg)
    except BaseException:
        lease.release()
        raise
    return RunContext(cfg, run_id, lease, closed=False, _token=_TOKEN)


# ---------------------------------------------------------------------------
# clear-fence: the CLI's operator-facing break-glass path. Runs under the global
# runner lease (never concurrently with a live runner) and implements the design's
# `clear-fence` semantics exactly -- see "## `clear-fence`" in the design doc.
# ---------------------------------------------------------------------------

def clear_fence(cfg, force: bool) -> int:
    """CLI entry point for `clear-fence`. Acquires the global runner lease with `hold=True`
    -- the same lease `reconcile()` takes -- so this can never run concurrently with a live
    runner: if the lease is held by a live process, print the owner (pid, heartbeat) and
    exit 3 without touching the fence (the break-glass sequence documented in SKILL.md is:
    verify the runner is wedged, kill it, re-run `clear-fence`).

    With the lease held: read the fence via `attempt.safe_read` (no-follow) and
    schema-validate it. A corrupt fence (unreadable, unparseable, wrong shape, an
    unresolvable/outside-attempts attempt_dir, or a proc that doesn't parse) is only
    clearable with `--force` -- nothing else can be done for it, since there is no attempt
    record to safely act on. Otherwise classify the proc:

    - `dead` -> load the referenced attempt and drive it to INTERRUPTED (via `interrupt()`,
      which also projects) if it is ORPHANED, or just project it if it is already
      INTERRUPTED (idempotent: a crash between that transition and the fence unlink on a
      prior run leaves exactly this state). Any other status is unexpected -- refuse.
    - `ours-alive` / `unknown` without `--force` -> refuse, printing the proc.
    - `ours-alive` / `unknown` with `--force` -> promote the attempt from FENCING to
      ORPHANED first if a crash landed it between the fence write and that transition
      (`fence()`'s only admitted torn window), then mark it `operator_forced=True` (status
      stays ORPHANED; NOT restored, NOT projected -- a live/unknown group may still be
      writing) and remove the fence. A referenced attempt in any status other than FENCING/
      ORPHANED has no recovery story here -- force-clear the fence file only, leaving the
      record untouched. The next runner start sees an ORPHANED record with no fence and
      re-fences it (fail closed) unless the group is by then dead.
    """
    lease = locks.Lease(pathlib.Path(cfg.state_root) / "locks" / "runner", "runner")
    if not lease.acquire(hold=True):
        rec = locks.owner(pathlib.Path(cfg.state_root) / "locks" / "runner")
        pid = rec.get("pid") if rec else "?"
        hb = rec.get("heartbeat_utc") if rec else "?"
        print(f"runner live: pid {pid} since {hb}; kill it and retry (break-glass)", file=sys.stderr)
        return 3
    try:
        fp = _fence_path(cfg)
        try:
            text = attempt_mod.safe_read(fp)
        except FileNotFoundError:
            print("no fence present", file=sys.stderr)
            return 0
        except attempt_mod.UnsafePath:
            if not force:
                print("corrupt fence; use --force", file=sys.stderr)
                return 3
            fp.unlink()
            return 0

        try:
            adir, proc, reason = _parse_fence(cfg, text)
            # "The record-integrity part of the sweep" for the fence's referenced attempt:
            # load it with worktree validation on (the default), same as _sweep would --
            # an unreadable/inconsistent record is a corrupt-fence situation, not a live/
            # unknown/dead proc classification.
            rec = attempt_mod.load(adir)
        except (CorruptFence, attempt_mod.UnreadableRecord, json.JSONDecodeError,
                OSError, UnicodeDecodeError) as e:
            if not force:
                print(f"corrupt fence; use --force ({e})", file=sys.stderr)
                return 3
            fp.unlink()
            return 0

        status = procid_mod.classify(procid_mod.ProcId.from_dict(proc))
        if status == "dead":
            if rec.status == "ORPHANED":
                interrupt(cfg, rec)
            elif rec.status == "INTERRUPTED":
                project_all(cfg, rec)
            else:
                print(f"{rec.attempt_id}: referenced attempt is neither ORPHANED nor "
                     f"INTERRUPTED (status={rec.status}); refusing to clear fence", file=sys.stderr)
                return 3
            fp.unlink()
            return 0

        # ours-alive or unknown.
        if not force:
            print(f"refusing to clear fence: proc is {status}: {json.dumps(proc, sort_keys=True)}",
                 file=sys.stderr)
            return 3
        # A crash between fence() writing the fence file and its ORPHANED transition leaves
        # the record at FENCING with a live fence referencing it -- promote it to ORPHANED
        # first so --force always leaves a record in the one status the next reconcile
        # (and this function's own dead-proc branch) knows how to handle. Any other status
        # means the fence references a record recovery has no story for; force-clear the
        # fence file only and leave the record untouched (corrupt-fence semantics).
        if rec.status == "FENCING":
            rec = attempt_mod.transition(rec, "ORPHANED")
            _maybe_crash("clear-fence-after-transition")
        elif rec.status != "ORPHANED":
            print(f"{rec.attempt_id}: referenced attempt is neither FENCING nor ORPHANED "
                 f"(status={rec.status}); corrupt fence, clearing fence file only",
                 file=sys.stderr)
            fp.unlink()
            return 0
        # Stamp `proc` onto the record too, not just the (now-deleted) fence file: STAGE_DONE
        # already nulled rec.proc (only the stage entry keeps it) before fence() was ever
        # called, so without this the next reconcile's ORPHANED-with-no-fence path would
        # classify(None) as "dead" and wrongly interrupt/restore under a still-live group.
        attempt_mod.set_flags(rec, operator_forced=True, proc=proc)
        fp.unlink()
        return 0
    finally:
        lease.release()
