"""Durable ownership of the four tickets admitted to autonomous work."""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import pathlib

import locks
import state

SCHEMA_VERSION = 1
MAX_HARD_CAPACITY = 7
PHASES = frozenset(("empty", "reserved", "active"))


class CohortError(ValueError):
    pass


@dataclasses.dataclass
class Slot:
    slot: int
    ticket: str | None = None
    phase: str = "empty"
    admitted_utc: str = ""
    admission_sequence: int = 0
    source: str = ""
    last_reconciled_utc: str = ""


@dataclasses.dataclass
class Cohort:
    schema_version: int
    capacity: int
    hard_capacity: int
    generation: int
    next_admission_sequence: int
    slots: list[Slot]
    history: list[dict] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class CohortReport:
    retained: list[str] = dataclasses.field(default_factory=list)
    released: list[str] = dataclasses.field(default_factory=list)
    resumed: list[str] = dataclasses.field(default_factory=list)
    parked: list[str] = dataclasses.field(default_factory=list)
    reserved: list[str] = dataclasses.field(default_factory=list)
    admitted: list[str] = dataclasses.field(default_factory=list)
    errors: list[str] = dataclasses.field(default_factory=list)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _path(cfg) -> pathlib.Path:
    return cfg.state_root / "cohort.json"


def _lease(cfg) -> locks.Lease:
    return locks.Lease(cfg.state_root / "locks" / "cohort", "cohort")


def _from_dict(raw) -> Cohort:
    if not isinstance(raw, dict):
        raise CohortError("cohort record must be an object")
    try:
        value = Cohort(
            schema_version=raw["schema_version"],
            capacity=raw["capacity"],
            hard_capacity=raw["hard_capacity"],
            generation=raw["generation"],
            next_admission_sequence=raw["next_admission_sequence"],
            slots=[Slot(**row) for row in raw["slots"]],
            history=list(raw.get("history", [])),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CohortError(f"malformed cohort record: {exc}") from exc
    validate(value)
    return value


def validate(value: Cohort) -> None:
    if not isinstance(value, Cohort):
        raise CohortError("cohort value must be a Cohort")
    if value.schema_version != SCHEMA_VERSION:
        raise CohortError(f"unsupported cohort schema {value.schema_version!r}")
    if not isinstance(value.capacity, int) or value.capacity < 1:
        raise CohortError("capacity must be a positive integer")
    if not isinstance(value.hard_capacity, int) or not value.capacity <= value.hard_capacity <= MAX_HARD_CAPACITY:
        raise CohortError(f"hard capacity must be between capacity and {MAX_HARD_CAPACITY}")
    if len(value.slots) != value.capacity:
        raise CohortError("slot count must equal capacity")
    if [row.slot for row in value.slots] != list(range(1, value.capacity + 1)):
        raise CohortError("slots must be consecutively numbered")
    tickets = []
    for row in value.slots:
        if row.phase not in PHASES:
            raise CohortError(f"invalid slot phase {row.phase!r}")
        if row.ticket is None:
            if row.phase != "empty":
                raise CohortError("an empty ticket must have empty phase")
            continue
        if not isinstance(row.ticket, str) or not row.ticket:
            raise CohortError("ticket must be a non-empty string or null")
        if row.phase == "empty":
            raise CohortError("an occupied slot cannot have empty phase")
        if row.admission_sequence < 1:
            raise CohortError("occupied slots need an admission sequence")
        tickets.append(row.ticket)
    if len(tickets) != len(set(tickets)):
        raise CohortError("a ticket may own only one slot")
    if not isinstance(value.generation, int) or value.generation < 1:
        raise CohortError("generation must be positive")
    if not isinstance(value.next_admission_sequence, int) or value.next_admission_sequence < 1:
        raise CohortError("next admission sequence must be positive")
    if not isinstance(value.history, list):
        raise CohortError("history must be a list")


def _load_unlocked(cfg) -> Cohort:
    try:
        raw = json.loads(_path(cfg).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise CohortError(f"cannot load cohort: {exc}") from exc
    return _from_dict(raw)


def load(cfg) -> Cohort:
    return _load_unlocked(cfg)


def _save_unlocked(cfg, value: Cohort) -> None:
    validate(value)
    cfg.state_root.mkdir(parents=True, exist_ok=True)
    target = _path(cfg)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(dataclasses.asdict(value), indent=2))
    os.replace(tmp, target)


def save(cfg, value: Cohort) -> None:
    with _lease(cfg) as acquired:
        if not acquired:
            raise CohortError("cohort lease is already held")
        _save_unlocked(cfg, value)


def initialize(cfg, capacity: int | None = None, hard_capacity: int | None = None) -> Cohort:
    capacity = cfg.cohort_capacity if capacity is None else capacity
    hard_capacity = cfg.hard_capacity if hard_capacity is None else hard_capacity
    with _lease(cfg) as acquired:
        if not acquired:
            raise CohortError("cohort lease is already held")
        if _path(cfg).exists():
            return _load_unlocked(cfg)
        value = Cohort(
            schema_version=SCHEMA_VERSION,
            capacity=capacity,
            hard_capacity=hard_capacity,
            generation=1,
            next_admission_sequence=1,
            slots=[Slot(slot=n) for n in range(1, capacity + 1)],
        )
        _save_unlocked(cfg, value)
        return value


def _find(value: Cohort, key: str) -> Slot | None:
    return next((row for row in value.slots if row.ticket == key), None)


def reserve(cfg, key: str, source: str) -> Slot:
    if not isinstance(key, str) or not key or not isinstance(source, str) or not source:
        raise CohortError("ticket and source must be non-empty strings")
    with _lease(cfg) as acquired:
        if not acquired:
            raise CohortError("cohort lease is already held")
        value = _load_unlocked(cfg)
        existing = _find(value, key)
        if existing:
            return existing
        slot = next((row for row in value.slots if row.ticket is None), None)
        if slot is None:
            raise CohortError(f"cohort is full ({value.capacity}/{value.capacity})")
        now = _now()
        slot.ticket = key
        slot.phase = "reserved"
        slot.admitted_utc = now
        slot.admission_sequence = value.next_admission_sequence
        slot.source = source
        slot.last_reconciled_utc = now
        value.next_admission_sequence += 1
        value.generation += 1
        _save_unlocked(cfg, value)
        return dataclasses.replace(slot)


def activate(cfg, key: str) -> Slot:
    with _lease(cfg) as acquired:
        if not acquired:
            raise CohortError("cohort lease is already held")
        value = _load_unlocked(cfg)
        slot = _find(value, key)
        if slot is None:
            raise CohortError(f"{key} has no cohort reservation")
        if slot.phase != "active":
            slot.phase = "active"
            slot.last_reconciled_utc = _now()
            value.generation += 1
            _save_unlocked(cfg, value)
        return dataclasses.replace(slot)


def members(cfg) -> list[str]:
    return [row.ticket for row in load(cfg).slots if row.ticket is not None]


def available(cfg) -> int:
    return sum(row.ticket is None for row in load(cfg).slots)


def _ci(ticket_dir: pathlib.Path) -> dict:
    try:
        value = json.loads((ticket_dir / "ci.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def release_if_review_ready(cfg, key: str, *, pr_lookup, head_lookup) -> bool:
    with _lease(cfg) as acquired:
        if not acquired:
            raise CohortError("cohort lease is already held")
        value = _load_unlocked(cfg)
        slot = _find(value, key)
        if slot is None:
            return False
        ticket_dir = cfg.ticket_dir(key)
        ticket = state.load(ticket_dir)
        if ticket.state != "human-gate-1" or ticket.decision_request:
            return False
        active_recovery = ticket.recovery.get("active") if isinstance(ticket.recovery, dict) else None
        if active_recovery is not None:
            return False
        worktree = pathlib.Path(ticket.worktree)
        if not ticket.worktree or not worktree.is_dir():
            return False
        ci = _ci(ticket_dir)
        pr = ci.get("pr")
        if not isinstance(pr, int) or isinstance(pr, bool) or pr < 1:
            return False
        try:
            head = head_lookup(str(worktree))
            pr_status = pr_lookup(pr)
        except Exception:
            return False
        if not isinstance(head, str) or not head or pr_status != "ready":
            return False
        if ticket.evidence_sha != head and ci.get("ready_marked_sha") != head:
            return False

        value.history.append({
            "event": "released",
            "ticket": key,
            "slot": slot.slot,
            "pr": pr,
            "reason": "verified-human-gate-1",
            "utc": _now(),
        })
        value.slots[slot.slot - 1] = Slot(slot=slot.slot)
        value.generation += 1
        _save_unlocked(cfg, value)
        return True
