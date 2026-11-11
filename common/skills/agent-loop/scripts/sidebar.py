"""Shared status, stall and verified-review policy over app-neutral workspace snapshots.

Cosmetic and best-effort: failures never change lifecycle, ownership or supervision.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import pathlib
import re
import subprocess
import typing

import locks
import state
import workspace_binding
import workspaces

PRODUCTION_ROOT = pathlib.Path("~/.local/state/agent-loop").expanduser()
GREEN, YELLOW, ORANGE, BLUE, GRAY, RED = "#34C759", "#FFCC00", "#FF9500", "#4C8DFF", "#8E8E93", "#FF3B30"
STALL_S = 4 * 3600
STAGE_TEXT = {"queued": "starting", "spinup": "starting", "plan": "planning", "plan-review": "planning",
              "implement": "implementing", "gates": "gates", "draft-pr": "CI", "ready": "CI", "bot-loop": "bot review"}
PAST_REVIEW = frozenset(("human-gate-1", "colleague-loop", "human-gate-2", "done"))
OWNER_QUESTION = re.compile(r"need(?:s)? Cody|owner question|question for the owner|tier-[34]", re.I)


class Label(typing.NamedTuple):
    spinner: bool
    text: str
    color: str
    icon: str


def stalled_hours(t: state.Ticket) -> int:
    try:
        then = dt.datetime.fromisoformat((t.updated_utc or "").replace("Z", "+00:00"))
    except ValueError:
        return 0
    age = (dt.datetime.now(dt.UTC) - then).total_seconds()
    return int(age // 3600) if age >= STALL_S else 0


def label(cfg, t: state.Ticket, queued: int | None = None) -> Label:
    tdir = cfg.ticket_dir(t.key)
    if (tdir / "INTERACTIVE.json").exists():
        return Label(False, "interactive", BLUE, "person.circle")
    if t.state in PAST_REVIEW:
        try:
            pr = json.loads((tdir / "ci.json").read_text()).get("pr")
        except (OSError, ValueError):
            pr = None
        return Label(False, f"ready · PR #{pr}" if pr else "ready for review", BLUE, "checkmark.circle")
    if queued is not None:
        return Label(False, f"queued #{queued}", GRAY, "clock")
    if (tdir / "HUMAN").exists() or t.state == "decision-required" or OWNER_QUESTION.search(t.reason or ""):
        return Label(False, "needs Cody", ORANGE, "exclamationmark.bubble")
    hours = stalled_hours(t)
    if hours:
        return Label(False, f"stalled {hours}h", RED, "exclamationmark.triangle")
    active = t.recovery.get("active") if isinstance(t.recovery, dict) else None
    if isinstance(active, dict):
        if active.get("status") == "waiting-provider":
            return Label(False, "quota wait", YELLOW, "hourglass")
        if active.get("status") == "manual-safety-review":
            return Label(False, "needs approval", ORANGE, "exclamationmark.bubble")
        return Label(True, "self-healing", YELLOW, "wrench")
    if t.state in ("paused", "blocked"):
        return Label(True, "self-healing", YELLOW, "wrench")
    return Label(True, STAGE_TEXT.get(t.state, t.state), GREEN, "hammer")


def _routing_snapshot(cfg, key):
    import cohort

    tdir = cfg.ticket_dir(key)
    if not (tdir / "state.json").is_file() or state.human_owned(tdir) or (tdir / "PARKED").exists():
        return None
    ticket = state.load(tdir)
    if ticket.key != key or ticket.state != "human-gate-1" or not ticket.worktree:
        return None
    if not pathlib.Path(ticket.worktree).is_dir():
        return None
    return ticket, cohort._ci(tdir), cohort._head(ticket.worktree)


def _route_review_ready(cfg, key, adapter, observed, routes, persist):
    """Eligibility and rechecks are shared; adapters only translate the final move."""
    import cohort
    import watch

    if observed.lane != "in-progress":
        return
    before = _routing_snapshot(cfg, key)
    if before is None:
        return
    signature = [observed.binding.backend, observed.identity, before[2]]
    completed = routes.get(key)
    if (completed == signature or (isinstance(completed, dict) and completed.get("pending") == signature)
            or (observed.binding.backend == "cmux" and completed == signature[1:])):
        return
    recorded = workspace_binding.read(cfg.ticket_dir(key))
    if recorded is not None and recorded.phase != "ready":
        return
    evidence = cohort._review_evidence(cfg, key, pr_lookup=watch.pr_state, head_lookup=lambda _: before[2])
    if evidence is None or evidence[1] != "verified-human-gate-1":
        return

    def unchanged():
        if _routing_snapshot(cfg, key) != before or workspace_binding.read(cfg.ticket_dir(key)) != recorded:
            return False
        current = adapter.inspect(observed.binding)
        return current.identity == observed.identity and current.lane == "in-progress"

    def record_move():
        routes[key] = {"pending": signature}
        try:
            persist()
        except Exception:
            if completed is None:
                routes.pop(key, None)
            else:
                routes[key] = completed
            raise

    adapter.before_move = unchanged
    adapter.record_move = record_move
    if not unchanged():
        return
    try:
        moved = adapter.move_to_review(observed.binding, observed)
    except workspaces.DefiniteFailure:
        moved = False
    # Unknown errors deliberately retain the durable pending intent: never replay a lost move.
    if moved:
        routes[key] = signature
    elif completed is None:
        routes.pop(key, None)
    else:
        routes[key] = completed
    persist()


def _desired(cfg) -> dict[str, Label]:
    import cohort
    import drip

    root = cfg.state_root / "tickets"
    tickets = {d.name: state.load(d) for d in sorted(root.iterdir()) if (d / "state.json").is_file()} if root.exists() else {}
    members = set(cohort.members(cfg)) if drip.cohort_enabled(cfg) else set(tickets)
    order = {key: n for n, key in enumerate((key for key in drip.parked(cfg) if not state.human_owned(cfg.ticket_dir(key))), 1)}
    want = {}
    for key, t in tickets.items():
        if t.state in PAST_REVIEW or key in members or (cfg.ticket_dir(key) / "INTERACTIVE.json").exists():
            want[key] = label(cfg, t)
        elif key in order:
            want[key] = label(cfg, t, queued=order[key])
    return want


def sync(cfg, *, run=subprocess.run, force: bool = False) -> None:
    """Serialize presentation journals without acquiring any lifecycle/ownership lease."""
    lease = None
    try:
        if pathlib.Path(cfg.state_root).resolve() != PRODUCTION_ROOT.resolve():
            return
        lease = locks.Lease(cfg.state_root / "locks" / "sidebar", "workspace presentation")
        if lease.acquire(hold=True):
            _sync(cfg, run=run)
    except Exception as exc:  # noqa: BLE001 -- cosmetic failure cannot affect execution
        logging.getLogger(__name__).debug("Workspace presentation unavailable (%s)", type(exc).__name__)
    finally:
        if lease is not None:
            lease.release()


def _sync(cfg, *, run):
    """Best effort, with per-workspace isolation and success-only presentation caching."""
    try:
        if pathlib.Path(cfg.state_root).resolve() != PRODUCTION_ROOT.resolve():
            return
        cache_path = cfg.state_root / "sidebar.json"
        try:
            cache = json.loads(cache_path.read_text())
        except (OSError, ValueError):
            cache = {}
        if not isinstance(cache, dict):
            cache = {}
        routes = cache.get("review_routes", {})
        if not isinstance(routes, dict):
            routes = {}
        def persist():
            if routes:
                cache["review_routes"] = routes
            else:
                cache.pop("review_routes", None)
            workspaces.write_record(cache_path, cache)
        for key, want in _desired(cfg).items():
            try:
                adapter, observed = workspaces.observe(cfg, state.load(cfg.ticket_dir(key)), run=run)
                with contextlib.suppress(Exception):
                    _route_review_ready(cfg, key, adapter, observed, routes, persist)
                signature = [observed.binding.backend, observed.identity, *want]
                if cache.get(key) == signature:
                    continue
                adapter.present(observed.binding, workspaces.Presentation(*want))
                cache[key] = signature
            except Exception as exc:  # noqa: BLE001 -- one app failure cannot hide the rest of the fleet
                logging.getLogger(__name__).debug("Workspace %s presentation unavailable (%s)", key, type(exc).__name__)
                continue
        persist()
    except Exception:  # noqa: BLE001 -- presentation must never affect execution
        return


def watchdog(cfg) -> None:
    """Notify Cody when an owned ticket has made no progress for STALL_S."""
    try:
        import cohort
        import drip
        import lifecycle

        keys = cohort.members(cfg) if drip.cohort_enabled(cfg) else []
        for key in keys:
            tdir = cfg.ticket_dir(key)
            if not (tdir / "state.json").is_file():
                continue
            t = state.load(tdir)
            got = label(cfg, t)
            if got.text.startswith("stalled"):
                lifecycle._notify_once(tdir, key, f"{key} {got.text}", (t.reason or t.state)[:160])
    except Exception:  # noqa: BLE001 -- observability must never affect the loop
        return
