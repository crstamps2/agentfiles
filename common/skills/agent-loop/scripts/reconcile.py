"""Reconcile part 1: finalization and the three idempotent projections (history, metrics,
lifecycle). Per the design's "one rule": `attempt.json` is the single source of truth;
these functions recompute each projection idempotently from a CLASSIFIED/FINALIZED
`attempt.Record` and are safe to re-run after a crash at any point.
"""
from __future__ import annotations
import pathlib
import subprocess

import attempt as attempt_mod
import metrics as metrics_mod
import state as state_mod
import worktree as worktree_mod


class FinalizeFailed(RuntimeError):
    """Raised when a restore-required outcome does not converge to base_tree."""
    pass


RESTORE_OUTCOMES = {"rejected", "protocol", "blocked"}


def _ticket_key(rec: attempt_mod.Record) -> str:
    """`lineage` is `<ticket>/<task.id>`; the ticket key is everything before the first
    `/`. Task ids and ticket keys are both single path components (validated elsewhere)."""
    return rec.lineage.split("/", 1)[0]


def _task_id(rec: attempt_mod.Record) -> str:
    _, _, rest = rec.lineage.partition("/")
    return rest or rec.lineage


def _diff_patch(wt, base_tree: str, observed_tree: str) -> str:
    r = subprocess.run(["git", "-C", str(wt), "diff", "--no-color", base_tree, observed_tree],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git diff failed: {r.stderr.strip()}")
    return r.stdout


# ---------------------------------------------------------------------------
# finalize(): CLASSIFIED -> FINALIZED.
# ---------------------------------------------------------------------------

def finalize(cfg, rec: attempt_mod.Record) -> attempt_mod.Record:
    """Restore the worktree to base_tree iff `rec.outcome` is one that discards the
    worker's tree (rejected/protocol/blocked); otherwise keep it. `diff.patch` is always
    regenerated as `git diff base_tree observed_tree` (regenerable, so overwriting it is
    always safe) before the record transitions to FINALIZED."""
    if rec.status != "CLASSIFIED":
        raise attempt_mod.IllegalAttemptTransition(
            f"finalize requires CLASSIFIED, got {rec.status}")

    wt = pathlib.Path(rec.worktree)
    observed = rec.observed_tree
    if observed is None:
        # Should already be set at CLASSIFYING (before any restore); computed here only
        # as a defensive fallback for records built by hand (e.g. tests) that skipped it.
        observed = worktree_mod.snapshot(wt)

    if rec.outcome in RESTORE_OUTCOMES:
        worktree_mod.restore(wt, rec.base_tree)
        if not worktree_mod.verify_restored(wt, rec.base_tree):
            raise FinalizeFailed(
                f"{rec.attempt_id}: worktree did not converge to base_tree after restore")
        tree = "restored"
    else:
        tree = "kept"

    patch = _diff_patch(wt, rec.base_tree, observed)
    attempt_mod.safe_rewrite(pathlib.Path(rec.path) / "diff.patch", patch)

    fields = {"tree": tree}
    if rec.observed_tree is None:
        fields["observed_tree"] = observed
    return attempt_mod.transition(rec, "FINALIZED", **fields)


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
    return attempt_mod.set_flags(rec, history=True)


def _metrics_row(rec: attempt_mod.Record, stage: dict) -> dict:
    return {
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
    return attempt_mod.set_flags(rec, published=True)


def project_lifecycle(cfg, rec: attempt_mod.Record) -> attempt_mod.Record:
    """Replay the persisted `next_action` onto the ticket's lifecycle state: `block` ->
    blocked, `pause-env`/`fence` -> paused, `none` -> nothing. Idempotent: never
    re-transitions a ticket that is already there (avoids IllegalTransition on replay).
    Short-circuits entirely if `lifecycle` is already true."""
    if rec.lifecycle:
        return rec
    tdir = cfg.ticket_dir(_ticket_key(rec))
    if rec.next_action == "block":
        t = state_mod.load(tdir)
        if t.state != "blocked":
            t = state_mod.transition(t, "blocked", reason=rec.reason or "")
            state_mod.save(tdir, t)
    elif rec.next_action in ("pause-env", "fence"):
        t = state_mod.load(tdir)
        if t.state != "paused":
            t = state_mod.transition(t, "paused", reason=rec.reason or "")
            state_mod.save(tdir, t)
    # "none" -> nothing to replay.
    return attempt_mod.set_flags(rec, lifecycle=True)


def project_all(cfg, rec: attempt_mod.Record) -> attempt_mod.Record:
    """Run all three projections and then, if all flags are now true and the record is
    FINALIZED, advance it to PROJECTED. Order among the three does not matter (each is
    independently idempotent and `next_action` makes lifecycle order-free)."""
    rec = project_history(cfg, rec)
    rec = project_metrics(cfg, rec)
    rec = project_lifecycle(cfg, rec)
    return attempt_mod.maybe_project(rec)
