"""Durable ownership of the four tickets admitted to autonomous work."""
from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import json
import os
import pathlib
import subprocess
import threading

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
    waits: dict[str, str] = dataclasses.field(default_factory=dict)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _path(cfg) -> pathlib.Path:
    return cfg.state_root / "cohort.json"


_HELD = threading.local()


@contextlib.contextmanager
def _lease(cfg):
    key = str(cfg.state_root.resolve())
    held = getattr(_HELD, "roots", set())
    if key in held:
        yield True
        return
    lease = locks.Lease(cfg.state_root / "locks" / "cohort", "cohort")
    acquired = lease.acquire(hold=True)
    if acquired:
        _HELD.roots = held | {key}
    try:
        yield acquired
    finally:
        if acquired:
            _HELD.roots = held
            lease.release()


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


def _review_evidence(cfg, key, *, pr_lookup, head_lookup):
    ticket_dir = cfg.ticket_dir(key)
    ticket = state.load(ticket_dir)
    if ticket.state != "human-gate-1" or ticket.decision_request:
        return None
    if not isinstance(ticket.recovery, dict) or ticket.recovery.get("active") is not None or ticket.recovery.get("queued"):
        return None
    if not ticket.worktree or not pathlib.Path(ticket.worktree).is_dir():
        return None
    ci = _ci(ticket_dir)
    pr = ci.get("pr")
    if not isinstance(pr, int) or isinstance(pr, bool) or pr < 1:
        return None
    try:
        head = head_lookup(ticket.worktree)
        pr_status = pr_lookup(pr)
    except Exception:
        return None
    if not isinstance(head, str) or not head or pr_status != "ready":
        return None
    if ticket.evidence_sha != head and ci.get("ready_marked_sha") != head:
        return None
    return pr


def _release(value, slot, pr):
    value.history.append({"event": "released", "ticket": slot.ticket, "slot": slot.slot,
                          "pr": pr, "reason": "verified-human-gate-1", "utc": _now()})
    value.slots[slot.slot - 1] = Slot(slot=slot.slot)
    value.generation += 1


def _park_slot(value, slot):
    value.history.append({"event": "parked", "ticket": slot.ticket, "slot": slot.slot,
                          "admission_sequence": slot.admission_sequence, "reason": "operator park", "utc": _now()})
    value.slots[slot.slot - 1] = Slot(slot=slot.slot)
    value.generation += 1


def park_member(cfg, key: str) -> bool:
    """Release a member that the operator explicitly parked; its durable marker keeps its admission order."""
    with _lease(cfg) as acquired:
        if not acquired:
            raise CohortError("cohort lease is already held")
        value = _load_unlocked(cfg)
        slot = _find(value, key)
        if slot is None:
            return False
        _park_slot(value, slot)
        _save_unlocked(cfg, value)
        return True


def release_if_review_ready(cfg, key: str, *, pr_lookup, head_lookup) -> bool:
    with _lease(cfg) as acquired:
        if not acquired:
            raise CohortError("cohort lease is already held")
        value = _load_unlocked(cfg)
        slot = _find(value, key)
        if slot is None:
            return False
        pr = _review_evidence(cfg, key, pr_lookup=pr_lookup, head_lookup=head_lookup)
        if pr is None:
            return False
        _release(value, slot, pr)
        _save_unlocked(cfg, value)
        return True


def _head(worktree):
    result = subprocess.run(["git", "-C", worktree, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30, check=True)
    return result.stdout.strip()


def reconcile(cfg, *, dry_run=False, pr_lookup=None, supervisor=None, head_lookup=None) -> CohortReport:
    import supervise
    import watch
    report = CohortReport()
    pr_lookup = pr_lookup or watch.pr_state
    head_lookup = head_lookup or _head
    supervisor = supervisor or supervise
    if dry_run:
        try:
            return _reconcile_locked(cfg, report, True, pr_lookup, head_lookup, supervisor)
        except (CohortError, OSError, ValueError, TypeError) as exc:
            report.errors.append(str(exc))
            return report
    with _lease(cfg) as acquired:
        if not acquired:
            report.errors.append("cohort lease is already held; retry next cycle")
            return report
        try:
            return _reconcile_locked(cfg, report, False, pr_lookup, head_lookup, supervisor)
        except (CohortError, OSError, ValueError, TypeError) as exc:
            report.errors.append(str(exc))
            return report


def _reconcile_locked(cfg, report, dry_run, pr_lookup, head_lookup, supervisor):
    import decision
    import drip
    import providers
    import recovery
    import repair
    import runner
    value = _load_unlocked(cfg)
    if state.paused(cfg.state_root):
        report.retained = [row.ticket for row in value.slots if row.ticket]
        report.waits = {key: "operator PAUSE" for key in report.retained}
        return report

    def can_resume(key):
        tdir = cfg.ticket_dir(key)
        return _find(value, key) is not None and not (tdir / "HUMAN").exists() and not (tdir / "PARKED").exists()

    if not dry_run:
        try:
            applied = recovery.consume_events(cfg, retry_task=runner.retry_task,
                                             ensure_supervisor=supervisor.ensure_running, can_resume=can_resume)
            report.resumed.extend(row.rsplit(":", 1)[0] for row in applied if row.endswith(":resumed"))
        except (recovery.RecoveryError, OSError, ValueError, TypeError) as exc:
            report.errors.append(f"repair events: {exc}")

    for slot in list(value.slots):
        key = slot.ticket
        if not key:
            continue
        tdir = cfg.ticket_dir(key)
        if (tdir / "PARKED").exists():
            _park_slot(value, slot)
            if not dry_run:
                _save_unlocked(cfg, value)
            report.parked.append(key)
            continue
        if (tdir / "HUMAN").exists():
            report.retained.append(key); report.waits[key] = "HUMAN takeover; slot retained"
            continue
        if slot.phase == "reserved":
            report.reserved.append(key)
            continue
        ticket_lease = locks.Lease(cfg.state_root / "locks" / f"runner-{key.lower()}", "cohort-ticket")
        if not dry_run and not ticket_lease.acquire(hold=True):
            report.retained.append(key); report.waits[key] = "runner live"
            continue
        try:
            if not (tdir / "state.json").is_file():
                raise CohortError("missing state.json")
            ticket = state.load(tdir)
            if ticket.key != key or ticket.state not in state.STATES:
                raise CohortError("invalid ticket identity or lifecycle state")
            if not ticket.worktree or not pathlib.Path(ticket.worktree).is_dir():
                raise CohortError("missing worktree")
            if (tdir / "PARKED").exists():
                raise CohortError("occupied active slot has a PARKED marker")
            if not isinstance(ticket.recovery, dict):
                raise CohortError("malformed recovery container")
            active = ticket.recovery.get("active")
            if active is not None:
                active = recovery.validate_contract(active)
                if active["status"] == "waiting-provider":
                    model = active["checkpoint"].get("model")
                    if not model:
                        raise CohortError("provider wait missing checkpoint model")
                    selected = providers.resolve(cfg.state_root, model)
                    if not providers.is_exhausted(cfg.state_root, providers.provider_of(selected)):
                        if not dry_run:
                            recovery.mark_ready(cfg, key, f"provider-ready-{active['id']}")
                        active["status"] = "ready-to-resume"
                if active["status"] in ("ready-to-resume", "resuming"):
                    if dry_run:
                        report.resumed.append(key)
                    else:
                        outcome = recovery.resume_active(cfg, key, retry_task=runner.retry_task, ensure_supervisor=supervisor.ensure_running)
                        if outcome == "resumed" and key not in report.resumed:
                            report.resumed.append(key)
                    ticket = state.load(tdir)
                else:
                    report.retained.append(key); report.waits[key] = f"recovery {active['status']}: {active['resume']['kind']}"
                    continue
            if ticket.state == "decision-required":
                request = decision.validate_request(ticket.decision_request)
                answered = next((row for row in ticket.decisions if isinstance(row, dict) and row.get("id") == request["id"]), None)
                if answered and not dry_run:
                    ticket = decision.answer(cfg, key, answered["guidance"], ensure_supervisor=supervisor.ensure_running)
                    report.resumed.append(key)
                else:
                    report.retained.append(key); report.waits[key] = f"product decision {request['id']}"
                    continue
            pr = _review_evidence(cfg, key, pr_lookup=pr_lookup, head_lookup=head_lookup)
            if pr is not None:
                _release(value, slot, pr)
                if not dry_run:
                    _save_unlocked(cfg, value)
                report.released.append(key)
                continue
            report.retained.append(key)
            if not dry_run:
                supervisor.ensure_running(key, ticket.worktree, cfg.state_root)
                slot.last_reconciled_utc = _now()
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            report.retained.append(key)
            report.errors.append(f"{key}: {exc}")
            report.waits[key] = f"diagnostic recovery: {exc}"
            if not dry_run:
                repair.record_defect(cfg, key, "cohort", f"invalid controller state: {exc}")
        finally:
            if not dry_run:
                ticket_lease.release()

    if not dry_run:
        _save_unlocked(cfg, value)
        occupied = {row.ticket for row in value.slots if row.ticket}
        for entry in value.history:
            if entry.get("event") == "released" and entry.get("ticket") not in occupied and not entry.get("supervisor_stopped"):
                supervisor.stop(entry["ticket"])
                entry["supervisor_stopped"] = True
                _save_unlocked(cfg, value)
        started = drip._tick_locked(cfg, log=lambda message: None, supervisor=supervisor)
        report.admitted.extend(started)
    else:
        free = sum(row.ticket is None for row in value.slots)
        occupied = {row.ticket for row in value.slots if row.ticket}
        waiting = [key for key in drip.parked(cfg) if key not in occupied and not (cfg.ticket_dir(key) / "HUMAN").exists()]
        if len(waiting) < free:
            waiting.extend(row["key"] for row in drip.candidates(cfg, log=lambda message: None) if row["key"] not in occupied and row["key"] not in waiting)
        report.admitted.extend(waiting[:free])
    return report
