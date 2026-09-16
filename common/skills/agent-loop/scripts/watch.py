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


def snapshot(cfg, key: str) -> dict:
    tdir = cfg.ticket_dir(key)
    t = state.load(tdir) if (tdir / "state.json").exists() else None
    snap = {"state": t.state if t else "queued", "reason": (t.reason or "")[:120] if t else ""}
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
    if s.get("attempt"): parts.append(s["attempt"])
    if s.get("pr"): parts.append(f"PR #{s['pr']}" + (f" ({s['pr_state']})" if s.get("pr_state") else ""))
    if s.get("ci_actions"): parts.append(f"ci:{s['ci_actions'][0]}")
    if s.get("bot_rounds"): parts.append(f"bot-round {s['bot_rounds']}")
    parts.append(f"sup:{s.get('sup','?').replace('state=','')}")
    parts.append(f"${cost:.2f}")
    line = "  ".join(parts)
    if s.get("reason") and s["state"] in ("paused", "blocked"): line += f"\n           ↳ {s['reason']}"
    elif s.get("attempt_reason"): line += f"\n           ↳ {s['attempt_reason']}"
    return line


def run(cfg, key: str, once: bool = False, interval: int = 20) -> int:
    last = None; last_beat = 0.0; last_cost = None
    while True:
        s = snapshot(cfg, key)
        cost = spend(cfg, key) if (last_cost is None or time.time() - last_beat > 120) else last_cost
        last_cost = cost
        sig = json.dumps(s, sort_keys=True)
        if sig != last:
            print(f"{_now()}  {fmt(key, s, cost)}", flush=True); last = sig; last_beat = time.time()
        elif time.time() - last_beat > 300:
            print(f"{_now()}  · still {s['state']}" + (f", {s['attempt'].split('[')[0].strip()}" if s.get('attempt') else "") + f"  ${cost:.2f}", flush=True); last_beat = time.time()
        if once or s["state"] in ("human-gate-1", "done"):
            if s["state"] in ("human-gate-1", "done"): print(f"{_now()}  READY FOR CODY" + (f": https://github.com/retailzipline/zipline-app/pull/{s['pr']}" if s.get('pr') else ""), flush=True)
            return 0
        time.sleep(interval)


_PR_CACHE: dict = {}


def pr_state(pr: int) -> str:
    """Cached (60 s) GitHub truth for the PR: draft / ready / merged / closed. The ticket stage `ready`
    means the runner is WORKING ON getting it ready; Cody reads this field to know whether it is."""
    import subprocess, time as _t
    now = _t.time(); hit = _PR_CACHE.get(pr)
    if hit and now - hit[0] < 60:
        return hit[1]
    r = subprocess.run(["gh", "pr", "view", str(pr), "--repo", "retailzipline/zipline-app", "--json", "isDraft,state", "--jq", '"\(.state) \(.isDraft)"'],
                       capture_output=True, text=True, timeout=30)
    out = r.stdout.strip().split()
    st = "?" if len(out) != 2 else ("merged" if out[0] == "MERGED" else "closed" if out[0] == "CLOSED" else "draft" if out[1] == "true" else "ready")
    _PR_CACHE[pr] = (now, st); return st

