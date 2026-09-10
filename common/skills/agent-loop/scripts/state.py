"""Per-ticket lifecycle state, persisted as state.json with atomic writes."""
from __future__ import annotations
import dataclasses
import datetime as dt
import json
import os
import pathlib

STATES = ("queued", "spinup", "plan", "plan-review", "implement", "gates", "draft-pr", "ready",
          "bot-loop", "human-gate-1", "colleague-loop", "human-gate-2", "done", "blocked", "paused")
_FORWARD = ("queued", "spinup", "plan", "plan-review", "implement", "gates", "draft-pr", "ready",
            "bot-loop", "human-gate-1", "colleague-loop", "human-gate-2", "done")

EDGES = {s: set() for s in STATES}
for a, b in zip(_FORWARD, _FORWARD[1:]):
    EDGES[a].add(b)
for s in STATES:
    if s not in ("done", "blocked", "paused"):
        EDGES[s] |= {"blocked", "paused"}
EDGES["blocked"] |= {"implement", "plan"}
for s in ("ready", "bot-loop", "colleague-loop", "human-gate-1", "human-gate-2"):
    EDGES[s].add("gates")            # any new SHA invalidates evidence
EDGES["plan-review"].add("plan")     # critic sends the plan back
EDGES["paused"] = set()              # resolved dynamically: only `previous`


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
    decisions: list = dataclasses.field(default_factory=list)
    reason: str = ""
    updated_utc: str = ""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load(ticket_dir) -> Ticket:
    ticket_dir = pathlib.Path(ticket_dir)
    p = ticket_dir / "state.json"
    if not p.exists():
        return Ticket(key=ticket_dir.name, updated_utc=_now())
    return Ticket(**json.loads(p.read_text()))


def save(ticket_dir, t: Ticket) -> None:
    ticket_dir = pathlib.Path(ticket_dir)
    ticket_dir.mkdir(parents=True, exist_ok=True)
    t.updated_utc = _now()
    tmp = ticket_dir / "state.json.tmp"
    tmp.write_text(json.dumps(dataclasses.asdict(t), indent=2))
    os.replace(tmp, ticket_dir / "state.json")


def transition(t: Ticket, to: str, reason: str = "") -> Ticket:
    if to not in STATES:
        raise IllegalTransition(f"unknown state {to!r}")
    allowed = {t.previous} if t.state == "paused" else EDGES[t.state]
    if to not in allowed:
        raise IllegalTransition(f"{t.key}: {t.state} -> {to} not allowed (allowed: {sorted(allowed)})")
    return dataclasses.replace(t, state=to, previous=t.state, reason=reason, updated_utc=_now())


def paused(state_root) -> bool:
    return (pathlib.Path(state_root) / "PAUSE").exists()


def human_owned(ticket_dir) -> bool:
    return (pathlib.Path(ticket_dir) / "HUMAN").exists()


def assign_vendors(index: int) -> tuple:
    return ("fable", "astra") if index % 2 == 1 else ("astra", "fable")
