"""`runner.py watch --ticket X`: the deterministic, zero-token live view for a cmux agent pane.

Prints one line whenever anything about the ticket changes -- stage, task/attempt, worker agent,
attempt status/outcome, PR number, CI verdict, supervisor state, spend -- and a heartbeat line
every few minutes so the pane visibly breathes. No model is involved. The coordinator model
(/loop-watch) is only summoned when the ticket is paused/blocked and a decision is needed.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import time

import ledger
import state
import supervise


def _now() -> str:
    return dt.datetime.now().strftime("%H:%M:%S")


def _age(utc: str) -> str:
    try:
        then = dt.datetime.fromisoformat(utc.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return "?"
    minutes = int((dt.datetime.now(dt.timezone.utc) - then).total_seconds() // 60)
    return f"{minutes // 60}h{minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


def _recovery_text(t) -> str:
    active = t.recovery.get("active") if t and isinstance(t.recovery, dict) else None
    if not isinstance(active, dict):
        return ""
    resume = active.get("resume") or {}
    text = f"recovery {active.get('status', '?')} {resume.get('kind', '?')}->{resume.get('state', '?')}"
    if active.get("defect_ids"):
        text += " defect=" + ",".join(active["defect_ids"])
    text += f" age={_age(active.get('created_utc', ''))}"
    if active.get("last_event_id"):
        text += f" event={active['last_event_id']}"
    return text


def _decision_text(t) -> str:
    request = t.decision_request if t and isinstance(t.decision_request, dict) else None
    if not request:
        return ""
    notified = "notified" if request.get("notified_utc") else "not notified"
    return f"decision {request.get('id', '?')} age={_age(request.get('created_utc', ''))} {notified}: {request.get('question', '')}"[:220]


def _slot(cfg, key: str):
    if not (cfg.state_root / "cohort.json").exists():
        return None, None
    import cohort
    try:
        value = cohort.load(cfg)
    except cohort.CohortError as exc:
        return f"cohort unavailable: {exc}", None
    row = next((slot for slot in value.slots if slot.ticket == key), None)
    return (row, value) if row else (None, value)


def snapshot(cfg, key: str) -> dict:
    tdir = cfg.ticket_dir(key)
    t = state.load(tdir) if (tdir / "state.json").exists() else None
    snap = {"state": t.state if t else "queued", "reason": (t.reason or "")[:120] if t else ""}
    if t and t.needs_operator:
        snap["needs_operator"] = t.needs_operator[:160]
    row, _ = _slot(cfg, key)
    if isinstance(row, str):
        snap["cohort_error"] = row
    elif row is not None:
        snap["slot"], snap["admission_sequence"], snap["slot_phase"] = row.slot, row.admission_sequence, row.phase
    elif (tdir / "PARKED").exists():
        snap["waiting"] = "parked"
    if _recovery_text(t):
        snap["recovery"] = _recovery_text(t)
    if _decision_text(t):
        snap["decision"] = _decision_text(t)
    if t:
        snap["progress_utc"] = t.updated_utc
    cij = tdir / "ci.json"
    if cij.exists():
        ci = json.loads(cij.read_text()); snap["pr"] = ci.get("pr"); snap["ci_actions"] = ci.get("actions", [])[-1:] ; snap["bot_rounds"] = ci.get("bot_rounds")
        if snap["pr"]:
            snap["pr_state"] = pr_state(snap["pr"])       # "draft" | "ready" | "merged" | "closed" -- the PR's REAL state, not the stage name
    root = cfg.state_root / "attempts" / key
    newest = None
    if root.exists():
        for td in root.iterdir():
            if not td.is_dir() or td.name.startswith("hand"): continue
            for ad in td.iterdir():
                if ad.name.isdigit() and (ad / "attempt.json").exists():
                    m = (ad / "attempt.json").stat().st_mtime
                    if newest is None or m > newest[0]: newest = (m, td.name, ad)
    if newest:
        d = json.loads((newest[2] / "attempt.json").read_text())
        started = d.get("started_utc")
        el = ""
        if started and d.get("status") in ("LAUNCHING", "RUNNING", "STAGE_DONE"):
            st = dt.datetime.fromisoformat(started.replace("Z", "+00:00")); el = f" {int((dt.datetime.now(dt.timezone.utc) - st).total_seconds() // 60)}m"
        stage = d["stages"][-1]["kind"] if d.get("stages") else "-"
        snap["attempt"] = f"task {newest[1]} #{newest[2].name} {d.get('agent','?').replace('-worker','')} {d['status'].lower()}{('/' + d['outcome']) if d.get('outcome') else ''} [{stage}]{el}"
        if d.get("outcome") and d.get("outcome") != "accepted": snap["attempt_reason"] = (d.get("reason") or "")[:100]
    try:
        snap["sup"] = supervise.status(key).split(",")[0]
    except Exception:  # noqa: BLE001
        snap["sup"] = "?"
    return snap


def spend(cfg, key: str) -> float:
    return round(sum(r["billed_usd"] for r in ledger.collect(cfg.state_root) if r.get("ticket") == key), 2)


def fmt(key: str, s: dict, cost: float) -> str:
    parts = [f"{key} [{s['state']}]"]
    if s.get("slot"): parts.append(f"slot {s['slot']} (#{s['admission_sequence']})")
    elif s.get("waiting"): parts.append(s["waiting"])
    if s.get("attempt"): parts.append(s["attempt"])
    if s.get("pr"): parts.append(f"PR #{s['pr']}" + (f" ({s['pr_state']})" if s.get("pr_state") else ""))
    if s.get("ci_actions"): parts.append(f"ci:{s['ci_actions'][0]}")
    if s.get("bot_rounds"): parts.append(f"bot-round {s['bot_rounds']}")
    parts.append(f"sup:{s.get('sup','?').replace('state=','')}")
    parts.append(f"${cost:.2f}")
    line = "  ".join(parts)
    if s.get("needs_operator"): line += f"\n           \u21b3 NEEDS OPERATOR: {s['needs_operator']}"
    if s.get("cohort_error"): line += f"\n           \u21b3 {s['cohort_error']}"
    if s.get("recovery"): line += f"\n           \u21b3 {s['recovery']}"
    if s.get("decision"): line += f"\n           \u21b3 {s['decision']}"
    if s.get("reason") and s["state"] in ("paused", "blocked"): line += f"\n           ↳ {s['reason']}"
    elif s.get("attempt_reason"): line += f"\n           ↳ {s['attempt_reason']}"
    return line


def run(cfg, key: str, once: bool = False, interval: int = 20) -> int:
    last = None; last_beat = 0.0; last_cost = None
    while True:
        s = snapshot(cfg, key)
        sig_now = json.dumps(s, sort_keys=True)
        if last_cost is None or sig_now != last or time.time() - last_beat > 120:
            cost = spend(cfg, key)
        else:
            cost = last_cost
        last_cost = cost
        sig = json.dumps(s, sort_keys=True)
        if sig != last:
            print(f"{_now()}  {fmt(key, s, cost)}", flush=True); last = sig; last_beat = time.time()
        elif time.time() - last_beat > 120:
            print(f"{_now()}  · watching — still {s['state']}" + (f", {s['attempt'].split('[')[0].strip()}" if s.get('attempt') else "") + f"  ${cost:.2f}", flush=True); last_beat = time.time()
        if once or s["state"] in ("human-gate-1", "done"):
            if s["state"] in ("human-gate-1", "done"): print(f"{_now()}  READY FOR CODY" + (f": https://github.com/retailzipline/zipline-app/pull/{s['pr']}" if s.get('pr') else ""), flush=True)
            return 0
        time.sleep(interval)


def fleet(cfg, *, pr_lookup=None, sup_status=None) -> str:
    """Deterministic cohort overview: membership, waits, admission order, and review-ready PRs."""
    import cohort
    import drip
    pr_lookup = pr_lookup or pr_state
    sup_status = sup_status or supervise.status
    try:
        value = cohort.load(cfg)
    except cohort.CohortError as exc:
        return f"COHORT UNAVAILABLE: {exc}\nAdmission is fail-closed until cohort.json is valid."
    members = [row for row in value.slots if row.ticket]
    lines = [f"COHORT {len(members)}/{value.capacity} (hard ceiling {value.hard_capacity})"]
    max_in_flight = int(cfg.drip.get("max_in_flight", 0) or 0)
    if max_in_flight > drip.HARD_MAX_IN_FLIGHT:
        lines.append(f"VIOLATION drip.max_in_flight={max_in_flight} exceeds hard ceiling {drip.HARD_MAX_IN_FLIGHT}")
    for row in value.slots:
        if not row.ticket:
            lines.append(f"{row.slot}  -  empty"); continue
        tdir = cfg.ticket_dir(row.ticket)
        t = state.load(tdir) if (tdir / "state.json").exists() else None
        wait = "HUMAN takeover" if (tdir / "HUMAN").exists() else f"sup={sup_status(row.ticket).split(',')[0]}"
        detail = _decision_text(t) or _recovery_text(t) or wait
        lines.append(f"{row.slot}  {row.ticket}  #{row.admission_sequence}  {row.phase}  state={t.state if t else 'missing'}  "
                     f"{detail}  progress={t.updated_utc if t else '?'}  slot retained")
    occupied = {row.ticket for row in members}
    waiting = [key for key in drip.parked(cfg) if key not in occupied and not state.human_owned(cfg.ticket_dir(key))]
    lines += ["", "WAITING ADMISSION"]
    for key in waiting:
        meta = cfg.ticket_dir(key) / "PARKED.json"
        sequence = json.loads(meta.read_text()).get("admission_sequence", "?") if meta.exists() else "?"
        lines.append(f"{key} #{sequence}")
    lines.append(f"next: {waiting[0] if waiting else 'first eligible Jira board candidate'}")
    root = cfg.state_root / "tickets"
    lines += ["", "INTERACTIVE (outside cohort)"]
    for marker in sorted(root.glob("*/INTERACTIVE.json")):
        record = json.loads(marker.read_text())
        lines.append(f"{marker.parent.name}  {record['mode']}")
    lines += ["", "REVIEW READY"]
    for tdir in sorted(root.iterdir()) if root.exists() else []:
        if tdir.name in occupied or (tdir / "INTERACTIVE.json").exists() or not (tdir / "state.json").exists():
            continue
        t = state.load(tdir)
        if t.state != "human-gate-1":
            continue
        ci = json.loads((tdir / "ci.json").read_text()) if (tdir / "ci.json").exists() else {}
        pr = ci.get("pr")
        lines.append(f"{t.key}  PR #{pr} ({pr_lookup(pr)})" if pr else f"{t.key}  PR missing")
    return "\n".join(lines)


_PR_CACHE: dict = {}


def pr_state(pr: int) -> str:
    """Cached (60 s) GitHub truth for the PR: draft / ready / merged / closed. The ticket stage `ready`
    means the runner is WORKING ON getting it ready; Cody reads this field to know whether it is."""
    import subprocess, time as _t
    now = _t.time(); hit = _PR_CACHE.get(pr)
    if hit and now - hit[0] < 60:
        return hit[1]
    r = subprocess.run(["gh", "pr", "view", str(pr), "--repo", "retailzipline/zipline-app", "--json", "isDraft,state", "--jq", r'"\(.state) \(.isDraft)"'],
                       capture_output=True, text=True, timeout=30)
    out = r.stdout.strip().split()
    st = "?" if len(out) != 2 else ("merged" if out[0] == "MERGED" else "closed" if out[0] == "CLOSED" else "draft" if out[1] == "true" else "ready")
    _PR_CACHE[pr] = (now, st); return st

