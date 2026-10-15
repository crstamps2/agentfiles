"""Structured, durable recovery contracts for interrupted ticket work."""
from __future__ import annotations

import copy
import datetime as dt
import uuid

import state

SCHEMA_VERSION = 1
STATUSES = frozenset((
    "waiting-repair", "waiting-provider", "ready-to-resume", "resuming",
    "manual-safety-review", "resolved", "obsolete",
))
ACTION_TARGETS = {
    "republish": "implement",
    "replan": "plan",
    "retry-spinup": "spinup",
}


class RecoveryError(ValueError):
    pass


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _nonempty(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RecoveryError(f"{name} must be a non-empty string")
    return value.strip()


def validate_contract(payload) -> dict:
    """Return a defensive copy of one valid version-1 recovery contract."""
    if not isinstance(payload, dict):
        raise RecoveryError("recovery contract must be an object")
    value = copy.deepcopy(payload)
    if value.get("schema_version") != SCHEMA_VERSION:
        raise RecoveryError(f"unsupported recovery schema {value.get('schema_version')!r}")
    _nonempty(value.get("id"), "id")
    _nonempty(value.get("ticket"), "ticket")
    if value.get("status") not in STATUSES:
        raise RecoveryError(f"invalid recovery status {value.get('status')!r}")

    checkpoint = value.get("checkpoint")
    resume = value.get("resume")
    if not isinstance(checkpoint, dict) or not isinstance(resume, dict):
        raise RecoveryError("checkpoint and resume must be objects")
    checkpoint_state = _nonempty(checkpoint.get("state"), "checkpoint.state")
    target = _nonempty(resume.get("state"), "resume.state")
    if checkpoint_state not in state.RESUMABLE_STATES or target not in state.RESUMABLE_STATES:
        raise RecoveryError("checkpoint and resume states must be pre-review resumable states")

    kind = _nonempty(resume.get("kind"), "resume.kind")
    if kind == "retry-task":
        task_id = _nonempty(resume.get("task_id"), "resume.task_id")
        task_slug = _nonempty(resume.get("task_slug"), "resume.task_slug")
        if target != "implement":
            raise RecoveryError("retry-task must resume implement")
        if checkpoint.get("task_id") != task_id or checkpoint.get("task_slug") != task_slug:
            raise RecoveryError("retry-task identity must match its checkpoint")
    elif kind in ACTION_TARGETS:
        if target != ACTION_TARGETS[kind]:
            raise RecoveryError(f"{kind} must resume {ACTION_TARGETS[kind]}")
    elif kind in ("resume-stage", "wait-provider"):
        if target != checkpoint_state:
            raise RecoveryError(f"{kind} must resume the checkpoint state")
    else:
        raise RecoveryError(f"unknown recovery action {kind!r}")

    defect_ids = value.get("defect_ids")
    if not isinstance(defect_ids, list) or any(not isinstance(v, str) or not v for v in defect_ids):
        raise RecoveryError("defect_ids must be a list of non-empty strings")
    if not isinstance(value.get("last_event_id", ""), str):
        raise RecoveryError("last_event_id must be a string")
    for field in ("created_utc", "updated_utc"):
        _nonempty(value.get(field), field)
    return value


def new_contract(key, checkpoint, resume, defect_ids=(), *, contract_id=None, status=None) -> dict:
    now = _now()
    kind = resume.get("kind") if isinstance(resume, dict) else None
    value = {
        "schema_version": SCHEMA_VERSION,
        "id": contract_id or f"recovery-{uuid.uuid4()}",
        "ticket": key,
        "status": status or ("waiting-provider" if kind == "wait-provider" else "waiting-repair"),
        "checkpoint": copy.deepcopy(checkpoint),
        "resume": copy.deepcopy(resume),
        "defect_ids": list(defect_ids),
        "created_utc": now,
        "updated_utc": now,
        "last_event_id": "",
    }
    return validate_contract(value)


def _ticket(cfg, key):
    return cfg.ticket_dir(key), state.load(cfg.ticket_dir(key))


def _normalize(container) -> dict:
    if not isinstance(container, dict):
        return {"active": None, "queued": []}
    active = container.get("active")
    queued = container.get("queued")
    return {"active": active, "queued": list(queued) if isinstance(queued, list) else []}


def activate(cfg, key, contract) -> state.Ticket:
    value = validate_contract(contract)
    if value["ticket"] != key:
        raise RecoveryError("contract ticket does not match target ticket")
    tdir, ticket = _ticket(cfg, key)
    ticket.recovery = _normalize(ticket.recovery)
    active = ticket.recovery["active"]
    if active is not None and active.get("id") != value["id"]:
        raise RecoveryError(f"{key} already has active recovery {active.get('id')}")
    if active is None:
        ticket.recovery["active"] = value
        state.save(tdir, ticket)
    return ticket


def queue(cfg, key, contract) -> state.Ticket:
    value = validate_contract(contract)
    if value["ticket"] != key:
        raise RecoveryError("contract ticket does not match target ticket")
    tdir, ticket = _ticket(cfg, key)
    ticket.recovery = _normalize(ticket.recovery)
    ids = {row.get("id") for row in ticket.recovery["queued"] if isinstance(row, dict)}
    active = ticket.recovery["active"]
    if active is None:
        ticket.recovery["active"] = value
    elif active.get("id") != value["id"] and value["id"] not in ids:
        ticket.recovery["queued"].append(value)
    state.save(tdir, ticket)
    return ticket


def mark_ready(cfg, key, event_id) -> state.Ticket:
    event_id = _nonempty(event_id, "event_id")
    tdir, ticket = _ticket(cfg, key)
    ticket.recovery = _normalize(ticket.recovery)
    active = ticket.recovery["active"]
    if active is None:
        raise RecoveryError(f"{key} has no active recovery")
    active = validate_contract(active)
    if active.get("last_event_id") == event_id and active.get("status") == "ready-to-resume":
        return ticket
    active["status"] = "ready-to-resume"
    active["last_event_id"] = event_id
    active["updated_utc"] = _now()
    ticket.recovery["active"] = active
    state.save(tdir, ticket)
    return ticket


def _promote(ticket: state.Ticket) -> None:
    queued = ticket.recovery["queued"]
    ticket.recovery["active"] = queued.pop(0) if queued else None


def _task_identity_still_matches(ticket: state.Ticket, contract: dict) -> bool:
    resume = contract["resume"]
    if resume["kind"] != "retry-task":
        return True
    entries = ticket.attempts.get(f"{ticket.key}/{resume['task_id']}", [])
    slugs = [entry.get("slug") for entry in entries if isinstance(entry, dict) and entry.get("slug")]
    return not slugs or slugs[-1] == resume["task_slug"]


def resume_active(cfg, key, *, retry_task, ensure_supervisor) -> str:
    tdir, ticket = _ticket(cfg, key)
    ticket.recovery = _normalize(ticket.recovery)
    active = ticket.recovery["active"]
    if active is None:
        return "no-active"
    contract = validate_contract(active)
    if contract["status"] != "ready-to-resume":
        return "waiting"
    if not _task_identity_still_matches(ticket, contract):
        _promote(ticket)
        state.save(tdir, ticket)
        return "obsolete"

    event_id = contract["last_event_id"]
    action = contract["resume"]
    note = f"automatic recovery {event_id}"
    if action["kind"] == "retry-task":
        retry_task(cfg, key, action["task_id"], note=note)
        ticket = state.load(tdir)
    else:
        ticket = state.resume_target(ticket, action["state"], reason=note)

    ticket.recovery = _normalize(ticket.recovery)
    active_after_action = ticket.recovery["active"]
    if active_after_action and active_after_action.get("id") == contract["id"]:
        _promote(ticket)
    state.save(tdir, ticket)
    ensure_supervisor(key, ticket.worktree, cfg.state_root)
    return "resumed"
