"""Structured, durable recovery contracts for interrupted ticket work."""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import os
import pathlib
import uuid

import heal
import locks
import state

SCHEMA_VERSION = 1
STATUSES = frozenset((
    "waiting-repair", "waiting-provider", "ready-to-resume", "resuming",
    "manual-safety-review", "resolved", "obsolete",
))
EVENT_OUTCOMES = frozenset(("landed", "not-needed", "needs-approval", "diagnostic-retry"))
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


def fingerprint(contract: dict) -> str:
    value = validate_contract(contract)
    stable = {key: value[key] for key in ("ticket", "checkpoint", "resume", "defect_ids")}
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()[:20]


def orphaned(cfg, contract: dict) -> bool:
    """A repair wait whose defect record exists nowhere can never be woken by a repair event."""
    if contract.get("status") != "waiting-repair" or contract.get("last_event_id"):
        return False
    root = pathlib.Path(cfg.state_root) / "defects"
    mine = (contract.get("ticket"), contract.get("id"))
    for defect_id in contract.get("defect_ids", []):
        for queue in ("open", "needs-approval", "failed", "landed"):
            for path in [root / queue / f"{defect_id}.json", *(root / queue).glob(f"{defect_id}-*.json")]:
                try:
                    record = json.loads(path.read_text())
                except (OSError, json.JSONDecodeError):
                    continue
                # Signatures are shared across tickets: only a record that lists this contract can wake it.
                if any((row.get("ticket"), row.get("recovery_id")) == mine
                       for row in record.get("affected_tickets", []) if isinstance(row, dict)):
                    return False
    return True


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
    if contract["status"] not in ("ready-to-resume", "resuming"):
        return "waiting"
    if ticket.state in ("human-gate-1", "human-gate-2", "done") or not _task_identity_still_matches(ticket, contract):
        _promote(ticket)
        state.save(tdir, ticket)
        return "obsolete"

    event_id = contract["last_event_id"]
    action = contract["resume"]
    note = f"automatic recovery {event_id}"
    if not contract.get("action_applied"):
        contract["status"] = "resuming"
        ticket.recovery["active"] = contract
        state.save(tdir, ticket)
        if action["kind"] == "retry-task":
            try:
                retry_task(cfg, key, action["task_id"], note=note)
            except SystemExit:
                # The task no longer exists in this ticket's history: nothing to retry.
                ticket = state.load(tdir)
                ticket.recovery = _normalize(ticket.recovery)
                _promote(ticket)
                state.save(tdir, ticket)
                return "obsolete"
            ticket = state.load(tdir)
        ticket = state.resume_target(ticket, action["state"], reason=note)
        contract["action_applied"] = True
        ticket.recovery["active"] = contract
        state.save(tdir, ticket)
    if not any(row.get("action") == heal.OPERATOR_RESUME and row.get("note") == note for row in heal.history(tdir)):
        heal.mark_operator_resume(tdir, note)
    if ticket.worktree and pathlib.Path(ticket.worktree).is_dir():
        transcript = pathlib.Path(ticket.worktree) / "planning" / key.lower() / "transcript.md"
        marker = f"<!-- recovery {contract['id']} event {event_id} -->"
        if not transcript.exists() or marker not in transcript.read_text():
            transcript.parent.mkdir(parents=True, exist_ok=True)
            with transcript.open("a") as handle:
                handle.write(f"\n{marker}\nAutomatic recovery: {action['kind']} -> {action['state']}.\n")
    ensure_supervisor(key, ticket.worktree, cfg.state_root)
    ticket = state.load(tdir)
    ticket.recovery = _normalize(ticket.recovery)
    active_after_action = ticket.recovery["active"]
    if active_after_action and active_after_action.get("id") == contract["id"]:
        _promote(ticket)
    state.save(tdir, ticket)
    return "resumed"


def _events_dir(cfg, processed: bool = False) -> pathlib.Path:
    path = pathlib.Path(cfg.state_root) / "defects" / "events"
    if processed:
        path /= "processed"
    path.mkdir(parents=True, exist_ok=True)
    return path


def emit_event(cfg, defect: dict, outcome: str, sha: str = "") -> pathlib.Path:
    if outcome not in EVENT_OUTCOMES:
        raise RecoveryError(f"invalid repair event outcome {outcome!r}")
    defect_id = _nonempty(defect.get("id"), "defect.id")
    affected = defect.get("affected_tickets", [])
    if not isinstance(affected, list):
        raise RecoveryError("affected_tickets must be a list")
    rows = []
    seen = set()
    for row in affected:
        if not isinstance(row, dict):
            raise RecoveryError("affected ticket references must be objects")
        pair = (_nonempty(row.get("ticket"), "affected.ticket"),
                _nonempty(row.get("recovery_id"), "affected.recovery_id"))
        if pair not in seen:
            rows.append({"ticket": pair[0], "recovery_id": pair[1],
                         "contract_fingerprint": row.get("contract_fingerprint", "")})
            seen.add(pair)
    event_id = f"repair-{defect_id}-{uuid.uuid4()}"
    value = {
        "schema_version": 1,
        "id": event_id,
        "defect_id": defect_id,
        "outcome": outcome,
        "sha": sha,
        "affected_tickets": rows,
        "created_utc": _now(),
    }
    target = _events_dir(cfg) / f"{event_id}.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(value, indent=2))
    os.replace(tmp, target)
    return target


def _set_event_wait(cfg, key: str, recovery_id: str, event_id: str, status: str) -> str:
    tdir, ticket = _ticket(cfg, key)
    ticket.recovery = _normalize(ticket.recovery)
    active = ticket.recovery["active"]
    if not isinstance(active, dict) or active.get("id") != recovery_id:
        return "obsolete"
    active = validate_contract(active)
    if active.get("last_event_id") == event_id and active.get("status") == status:
        return status
    active["status"] = status
    active["last_event_id"] = event_id
    active["updated_utc"] = _now()
    ticket.recovery["active"] = active
    state.save(tdir, ticket)
    return status


def _apply_event_row(cfg, key, event, row, active, retry_task, ensure_supervisor) -> str:
    recovery_id, event_id = row["recovery_id"], event["id"]
    if not isinstance(active, dict) or active.get("id") != recovery_id:
        return "obsolete"
    if row.get("contract_fingerprint") and row["contract_fingerprint"] != fingerprint(active):
        return "obsolete"
    if event["defect_id"] not in active.get("defect_ids", []):
        return "obsolete"
    if event["outcome"] in ("landed", "not-needed"):
        if active.get("last_event_id") != event_id or active.get("status") not in ("ready-to-resume", "resuming"):
            mark_ready(cfg, key, event_id)
        return resume_active(cfg, key, retry_task=retry_task, ensure_supervisor=ensure_supervisor)
    if event["outcome"] == "needs-approval":
        return _set_event_wait(cfg, key, recovery_id, event_id, "manual-safety-review")
    return _set_event_wait(cfg, key, recovery_id, event_id, "waiting-repair")


def consume_events(cfg, *, retry_task, ensure_supervisor, after_apply=None, can_resume=None, errors=None) -> list[str]:
    """Apply pending repair events at least once; per-contract receipts make replay idempotent."""
    results = []
    errors = [] if errors is None else errors
    for path in sorted(_events_dir(cfg).glob("*.json")):
        try:
            event = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise RecoveryError(f"cannot read repair event {path}: {exc}") from exc
        if event.get("schema_version") != 1 or event.get("outcome") not in EVENT_OUTCOMES:
            raise RecoveryError(f"unsupported repair event {path.name}")
        event_id = _nonempty(event.get("id"), "event.id")
        receipts = event.setdefault("receipts", {})
        pending = False
        for row in event.get("affected_tickets", []):
            key = _nonempty(row.get("ticket"), "affected.ticket")
            recovery_id = _nonempty(row.get("recovery_id"), "affected.recovery_id")
            receipt_key = f"{key}|{recovery_id}"
            if receipt_key in receipts:
                results.append(f"{key}:obsolete")
                continue
            tdir = cfg.ticket_dir(key)
            if (can_resume is not None and not can_resume(key)) or (tdir / "HUMAN").exists() or (tdir / "PARKED").exists():
                pending = True
                continue
            lease = locks.Lease(cfg.state_root / "locks" / f"runner-{key.lower()}", "recovery-event")
            if not lease.acquire(hold=True):
                pending = True
                continue
            try:
                ticket = state.load(tdir)
                active = ticket.recovery.get("active") if isinstance(ticket.recovery, dict) else None
                queued = ticket.recovery.get("queued", []) if isinstance(ticket.recovery, dict) else []
                if any(isinstance(candidate, dict) and candidate.get("id") == recovery_id for candidate in queued):
                    pending = True
                    continue
                try:
                    result = _apply_event_row(cfg, key, event, row, active, retry_task, ensure_supervisor)
                except Exception as exc:  # noqa: BLE001 -- one ticket's failure must not stall the fleet's events
                    pending = True
                    results.append(f"{key}:error")
                    errors.append(f"{key}: {type(exc).__name__}: {exc}")
                    continue
                receipts[receipt_key] = {"result": result, "utc": _now()}
                tmp = path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(event, indent=2))
                os.replace(tmp, path)
                results.append(f"{key}:{result}")
                if after_apply is not None:
                    after_apply(event, row, result)
            finally:
                lease.release()
        if not pending:
            processed = _events_dir(cfg, processed=True) / path.name
            os.replace(path, processed)
    return results
