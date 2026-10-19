"""Per-ticket lifecycle state, persisted as state.json with atomic writes."""
from __future__ import annotations
import copy
import dataclasses
import datetime as dt
import json
import os
import pathlib

STATES = ("queued", "spinup", "plan", "plan-review", "implement", "gates", "draft-pr", "ready",
          "bot-loop", "human-gate-1", "colleague-loop", "human-gate-2", "done", "blocked", "paused",
          "decision-required")
_FORWARD = ("queued", "spinup", "plan", "plan-review", "implement", "gates", "draft-pr", "ready",
            "bot-loop", "human-gate-1", "colleague-loop", "human-gate-2", "done")
RESUMABLE_STATES = frozenset(("spinup", "plan", "plan-review", "implement", "gates", "draft-pr", "ready", "bot-loop"))

EDGES = {s: set() for s in STATES}
for a, b in zip(_FORWARD, _FORWARD[1:]):
    EDGES[a].add(b)
for s in STATES:
    if s not in ("done", "blocked", "paused", "decision-required", "human-gate-1", "human-gate-2"):
        EDGES[s] |= {"blocked", "paused", "decision-required"}
EDGES["blocked"] |= {"implement", "plan"}
for s in ("ready", "bot-loop", "colleague-loop", "human-gate-1", "human-gate-2"):
    EDGES[s].add("gates")            # any new SHA invalidates evidence
EDGES["plan-review"].add("plan")     # critic sends the plan back
for s_ in ("gates", "ready", "bot-loop"):
    EDGES[s_].add("implement")       # a failed gate / CI code failure / accepted review finding becomes a fix task
EDGES["paused"] = set()              # resolved dynamically: only `previous`
EDGES["decision-required"] = set()  # resolved through `resume_target` after durable guidance


class IllegalTransition(ValueError):
    pass


@dataclasses.dataclass
class Ticket:
    key: str
    state: str = "queued"
    previous: str = ""
    worktree: str = ""
    branch: str = ""
    workspace: str = ""
    head_sha: str = ""
    evidence_sha: str = ""
    author_vendor: str = ""
    critic_vendor: str = ""
    attempts: dict = dataclasses.field(default_factory=dict)
    attempt_slugs: dict = dataclasses.field(default_factory=dict)
    recovery: dict = dataclasses.field(default_factory=lambda: {"active": None, "queued": []})
    decision_request: dict | None = None
    decisions: list = dataclasses.field(default_factory=list)
    reason: str = ""
    spinup_failures: int = 0
    needs_operator: str = ""
    updated_utc: str = ""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load(ticket_dir) -> Ticket:
    ticket_dir = pathlib.Path(ticket_dir)
    p = ticket_dir / "state.json"
    if not p.exists():
        return Ticket(key=ticket_dir.name, updated_utc=_now())
    data = json.loads(p.read_text())
    fields = {f.name for f in dataclasses.fields(Ticket)}
    return Ticket(**{k: v for k, v in data.items() if k in fields})


def save(ticket_dir, t: Ticket) -> None:
    ticket_dir = pathlib.Path(ticket_dir)
    ticket_dir.mkdir(parents=True, exist_ok=True)
    t.updated_utc = _now()
    tmp = ticket_dir / "state.json.tmp"
    tmp.write_text(json.dumps(dataclasses.asdict(t), indent=2))
    os.replace(tmp, ticket_dir / "state.json")


def _replace(t: Ticket, **changes) -> Ticket:
    return dataclasses.replace(
        t,
        attempts=copy.deepcopy(t.attempts),
        attempt_slugs=dict(t.attempt_slugs),
        recovery=copy.deepcopy(t.recovery),
        decision_request=copy.deepcopy(t.decision_request),
        decisions=copy.deepcopy(t.decisions),
        **changes,
    )


def transition(t: Ticket, to: str, reason: str = "") -> Ticket:
    if to not in STATES:
        raise IllegalTransition(f"unknown state {to!r}")
    allowed = {t.previous} if t.state == "paused" else EDGES[t.state]
    if to not in allowed:
        raise IllegalTransition(f"{t.key}: {t.state} -> {to} not allowed (allowed: {sorted(allowed)})")
    return _replace(t, state=to, previous=t.state, reason=reason, updated_utc=_now(), needs_operator="")


def resume_target(t: Ticket, target: str, reason: str = "") -> Ticket:
    """Resume controller-owned pre-review work from a durable checkpoint."""
    if target not in RESUMABLE_STATES:
        raise IllegalTransition(f"invalid recovery target {target!r}")
    if t.state in ("human-gate-1", "human-gate-2", "done"):
        raise IllegalTransition(f"{t.key}: cannot recover terminal state {t.state!r}")
    return _replace(t, state=target, previous=t.state, reason=reason, updated_utc=_now(), needs_operator="")


def paused(state_root) -> bool:
    return (pathlib.Path(state_root) / "PAUSE").exists()


def human_owned(ticket_dir) -> bool:
    root = pathlib.Path(ticket_dir)
    return (root / "HUMAN").exists() or (root / "INTERACTIVE.json").exists()


def assign_vendors(index: int) -> tuple:
    return ("fable", "astra") if index % 2 == 1 else ("astra", "fable")
