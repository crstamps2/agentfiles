"""Usage ledger: tokens and cost per model, per stage, per ticket, per day.

Source of truth is the pi session log the runner keeps for every model call (worker attempts,
planner/critic rounds, review adjudication, PR-body drafting): each assistant turn records
provider/model and a usage block with input/output/cacheRead/cacheWrite tokens and pi's cost
estimate. The ledger folds those into `usage.jsonl` (one row per session file, idempotent by
path) and answers roll-ups. `metrics.jsonl` rows for worker attempts also get tokens/cost
attached at attempt close so the two ledgers agree.

Cost caveats: pi prices from its model registry. Ollama Cloud on the Free plan bills $0 but the
registry carries a nominal price; the ledger reports both `cost_usd` (pi) and `billed_usd`
(after FREE_PROVIDERS zeroing) so the number Cody pays is the one shown by default.
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import pathlib

FREE_PROVIDERS = {"ollama-cloud", "ollama-local"}
STAGE_OF = (("/attempts/", "worker"), ("/plans/", "plan"), ("/botreview/", "review"), ("/prbody/", "prbody"))


def _stage(path: str) -> str:
    for needle, name in STAGE_OF:
        if needle in path:
            return name
    return "other"


def _ticket(path: pathlib.Path, root: pathlib.Path) -> str:
    """First path component under attempts/ | plans/ | botreview/ | prbody/ is the ticket key."""
    try:
        parts = path.relative_to(root).parts
        return parts[0] if parts else "?"
    except ValueError:
        return "?"


def _role(path: pathlib.Path) -> str:
    s = str(path)
    if "/author-" in s: return "author"
    if "/critic-" in s: return "critic"
    if "/round-" in s: return "adjudicator"
    if "/prbody/" in s: return "prbody"
    if "/attempts/" in s:
        return "worker"
    return "?"


def summarize_session(f: pathlib.Path) -> dict | None:
    turns = 0; tok = collections.Counter(); cost = 0.0; model = None; provider = None; first = last = None
    for line in f.read_text(errors="replace").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        m = r.get("message") or {}
        if m.get("role") != "assistant" or not m.get("usage"):
            continue
        u = m["usage"]; turns += 1; model = m.get("model"); provider = m.get("provider")
        for k in ("input", "output", "cacheRead", "cacheWrite"):
            tok[k] += int(u.get(k) or 0)
        cost += float(((u.get("cost") or {}).get("total")) or 0.0)
        ts = r.get("timestamp"); first = first or ts; last = ts or last
    if not turns:
        return None
    billed = 0.0 if provider in FREE_PROVIDERS else cost
    return {"session": str(f), "provider": provider, "model": model, "turns": turns, "input": tok["input"], "output": tok["output"],
            "cache_read": tok["cacheRead"], "cache_write": tok["cacheWrite"], "cost_usd": round(cost, 4), "billed_usd": round(billed, 4),
            "first_ts": first, "last_ts": last}


def collect(state_root: pathlib.Path) -> list[dict]:
    """Fold every session log under the state root into usage.jsonl (idempotent by session path)."""
    out = state_root / "usage.jsonl"
    seen = {}
    if out.exists():
        for line in out.read_text().splitlines():
            if line.strip():
                r = json.loads(line); seen[r["session"]] = r
    rows = []
    for sub in ("attempts", "plans", "botreview", "prbody"):
        base = state_root / sub
        if not base.exists():
            continue
        for f in base.rglob("session/*.jsonl"):
            key = str(f)
            prev = seen.get(key)
            # re-summarise if the file grew since (a running session); otherwise keep the cached row
            if prev and prev.get("_size") == f.stat().st_size:
                rows.append(prev); continue
            s = summarize_session(f)
            if not s:
                continue
            s.update({"_size": f.stat().st_size, "stage": _stage(key), "role": _role(f), "ticket": _ticket(f, base),
                      "day": (s["first_ts"] or "")[:10]})
            rows.append(s); seen[key] = s
    out.write_text("".join(json.dumps(r) + "\n" for r in sorted(seen.values(), key=lambda r: r.get("first_ts") or "")))
    return sorted(seen.values(), key=lambda r: r.get("first_ts") or "")


def rollup(rows: list[dict], by: str) -> list[tuple]:
    agg = collections.defaultdict(lambda: collections.Counter())
    for r in rows:
        k = r.get(by) or "?"
        c = agg[k]; c["turns"] += r["turns"]; c["input"] += r["input"]; c["output"] += r["output"]; c["cache_read"] += r["cache_read"]
        c["cost"] += r["cost_usd"]; c["billed"] += r["billed_usd"]; c["sessions"] += 1
    return sorted(((k, dict(v)) for k, v in agg.items()), key=lambda kv: -kv[1]["billed"])


def report(state_root: pathlib.Path, since_day: str | None = None) -> str:
    rows = collect(state_root)
    if since_day:
        rows = [r for r in rows if (r.get("day") or "") >= since_day]
    lines = []
    tot = collections.Counter()
    for r in rows:
        tot["cost"] += r["cost_usd"]; tot["billed"] += r["billed_usd"]; tot["turns"] += r["turns"]
    lines.append(f"sessions={len(rows)} turns={tot['turns']} billed=${tot['billed']:.2f} (pi-estimate ${tot['cost']:.2f})")
    for by, title in (("model", "by model"), ("role", "by role"), ("ticket", "by ticket"), ("day", "by day")):
        lines.append(f"\n{title}:")
        for k, v in rollup(rows, by):
            lines.append(f"  {str(k):40} turns={v['turns']:5}  in={v['input']:>10,}  cache={v['cache_read']:>12,}  out={v['output']:>8,}  billed=${v['billed']:8.2f}")
    return "\n".join(lines)


def attempt_usage(attempt_dir: pathlib.Path) -> dict:
    """Tokens/cost for one attempt directory (all its session files)."""
    tot = collections.Counter(); cost = 0.0; billed = 0.0; model = None
    for f in (attempt_dir / "session").glob("*.jsonl") if (attempt_dir / "session").exists() else []:
        s = summarize_session(f)
        if not s: continue
        model = s["model"]
        for k in ("input", "output", "cache_read", "cache_write", "turns"): tot[k] += s[k]
        cost += s["cost_usd"]; billed += s["billed_usd"]
    return {"tokens_in": tot["input"], "tokens_out": tot["output"], "tokens_cache_read": tot["cache_read"], "turns": tot["turns"],
            "cost_usd": round(cost, 4), "billed_usd": round(billed, 4), "usage_model": model}


def today() -> str:
    return dt.date.today().isoformat()
