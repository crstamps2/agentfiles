"""Validated product/API decision requests that retain cohort ownership while waiting."""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import pathlib
import re

import recovery
import state


class DecisionError(ValueError):
    pass


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DecisionError(f"{field} must be non-empty text")
    return value.strip()


def validate_request(payload) -> dict:
    if not isinstance(payload, dict):
        raise DecisionError("decision request must be an object")
    try:
        encoded = json.dumps(payload)
    except (TypeError, ValueError) as exc:
        raise DecisionError("decision request must be JSON data") from exc
    if len(encoded.encode()) > 32768:
        raise DecisionError("decision request exceeds 32 KiB")
    value = copy.deepcopy(payload)
    if value.get("schema_version") != 1:
        raise DecisionError(f"unsupported decision schema {value.get('schema_version')!r}")
    value["summary"] = _text(value.get("summary", value.get("ambiguity")), "summary")
    value["why_unresolved"] = _text(value.get("why_unresolved"), "why_unresolved")
    if "recommendation_reason" not in value and "ambiguity" in value:
        choice, separator, reasoning = _text(value.get("recommendation"), "recommendation").partition(",")
        if not separator:
            raise DecisionError("recommendation must include reasoning")
        value["recommendation"], value["recommendation_reason"] = choice.strip(), reasoning.strip()
    value["recommendation_reason"] = _text(value.get("recommendation_reason"), "recommendation_reason")
    sources = value.get("sources", value.get("sources_checked"))
    if not isinstance(sources, list) or not sources:
        raise DecisionError("sources must contain at least one source")
    value["sources"] = [_text(source, "source") for source in sources]
    options = value.get("options")
    if not isinstance(options, list) or len(options) < 2:
        raise DecisionError("options must contain at least two alternatives")
    normalized, signatures, ids = [], set(), set()
    for option in options:
        if not isinstance(option, dict):
            raise DecisionError("each option must be an object")
        row = {"id": _text(option.get("id"), "option.id"),
               "label": _text(option.get("label", option.get("choice")), "option.label"),
               "consequences": _text(option.get("consequences", option.get("consequence")), "option.consequences")}
        if row["id"] in ids:
            raise DecisionError("option ids must be unique")
        signature = (row["label"].casefold(), row["consequences"].casefold())
        if signature in signatures:
            raise DecisionError("options must be materially different")
        ids.add(row["id"]); signatures.add(signature); normalized.append(row)
    value["options"] = normalized
    value["recommendation"] = _text(value.get("recommendation"), "recommendation")
    if value["recommendation"] not in ids:
        raise DecisionError("recommendation must name one option")
    value["question"] = _text(value.get("question"), "question")
    checkpoint = value.get("checkpoint")
    resume = value.get("resume", checkpoint.get("resume") if isinstance(checkpoint, dict) else None)
    try:
        checked = recovery.new_contract("DECISION", checkpoint, resume,
                                        ("decision-validation",), contract_id="decision-validation")
    except recovery.RecoveryError as exc:
        raise DecisionError(str(exc)) from exc
    if checked["resume"]["kind"] not in ("resume-stage", "replan"):
        raise DecisionError("decision resume must use resume-stage or replan")
    value["checkpoint"] = checked["checkpoint"]
    value["resume"] = checked["resume"]
    canonical = json.dumps({k: v for k, v in value.items() if k not in ("id", "created_utc", "notified_utc")}, sort_keys=True)
    value["id"] = str(value.get("id") or f"decision-{hashlib.sha256(canonical.encode()).hexdigest()[:16]}")
    if not re.fullmatch(r"decision-[A-Za-z0-9._-]+", value["id"]):
        raise DecisionError("decision id must be a safe decision-* identifier")
    value["created_utc"] = str(value.get("created_utc") or _now())
    value["notified_utc"] = str(value.get("notified_utc") or "")
    return value


def _notice(request: dict) -> tuple[str, str]:
    choices = "; ".join(f"{row['id']}: {row['label']}" for row in request["options"])
    return "Product decision required", (
        f"{request['summary']} {request['question']} Options: {choices}. "
        f"Recommended: {request['recommendation']} - {request['recommendation_reason']} "
        f"[{request['id']}]"
    )


def propose(cfg, key: str, payload, notify=None) -> state.Ticket:
    request = validate_request(payload)
    tdir = cfg.ticket_dir(key); ticket = state.load(tdir)
    current = ticket.decision_request if isinstance(ticket.decision_request, dict) else {}
    if current:
        if current.get("id") != request["id"]:
            raise DecisionError(f"{key} already has open decision {current.get('id')}")
        return ticket
    if isinstance(ticket.recovery, dict) and ticket.recovery.get("active"):
        raise DecisionError(f"{key} has active recovery; cannot open a product decision")
    if ticket.state != "decision-required":
        try:
            ticket = state.transition(ticket, "decision-required", reason=request["question"])
        except state.IllegalTransition:
            ticket = state.resume_target(ticket, "implement", reason="stage validated product decision")
            ticket = state.transition(ticket, "decision-required", reason=request["question"])
    ticket.decision_request = request
    state.save(tdir, ticket)
    if notify is not None and not request["notified_utc"]:
        notify(*_notice(request))
        request["notified_utc"] = _now()
        state.save(tdir, ticket)
    return ticket


def _append_context(ticket: state.Ticket, request: dict, guidance: str) -> None:
    if not ticket.worktree:
        return
    root = pathlib.Path(ticket.worktree) / "planning" / ticket.key.lower()
    root.mkdir(parents=True, exist_ok=True)
    marker = f"<!-- {request['id']} -->"
    blocks = {
        "plan.md": f"\n## Owner rulings\n\n{marker}\n- {request['question']}\n- Guidance: {guidance}\n",
        "transcript.md": f"\n## Product decision {_now()}\n\n{marker}\n{request['summary']}\n\nGuidance: {guidance}\n",
    }
    for name, block in blocks.items():
        path = root / name
        if not path.exists() or marker not in path.read_text():
            with path.open("a") as handle:
                handle.write(block)


def answer(cfg, key: str, guidance: str, *, ensure_supervisor) -> state.Ticket:
    guidance = _text(guidance, "guidance")
    tdir = cfg.ticket_dir(key); ticket = state.load(tdir)
    if ticket.state != "decision-required" or not ticket.decision_request:
        raise DecisionError(f"{key} has no open decision request")
    request = validate_request(ticket.decision_request)
    prior = next((row for row in ticket.decisions if isinstance(row, dict) and row.get("id") == request["id"]), None)
    if prior:
        if prior.get("guidance") != guidance:
            raise DecisionError("this request already has durable guidance; finish resuming that answer first")
    else:
        ticket.decisions.append({"id": request["id"], "question": request["question"],
                                 "request": copy.deepcopy(request), "guidance": guidance,
                                 "resume": copy.deepcopy(request["resume"]), "answered_utc": _now()})
        state.save(tdir, ticket)  # durable answer exists before the standing request is cleared
    _append_context(ticket, request, guidance)
    ticket = state.resume_target(ticket, request["resume"]["state"], reason="product decision answered")
    ticket.decision_request = {}
    state.save(tdir, ticket)
    ensure_supervisor(key, ticket.worktree, cfg.state_root)
    return ticket
