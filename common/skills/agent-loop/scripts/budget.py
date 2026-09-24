"""Spend visibility for the autonomous loop.

Not a gate. Cody's decision (2026-09-23): the loop should not refuse work on a dollar threshold --
it should make cost legible, so a bad night is obvious the next morning rather than inferred from a
number in a log. The one ceiling that remains lives in lifecycle.py and is about OUTPUT, not money:
a ticket that has spent a lot and produced no accepted task stops for an owner ruling.

Everything here reads the same `usage.jsonl`-derived rows the operator reads, so the report and the
reality cannot drift.
"""
import collections
import datetime as dt
import pathlib

import ledger

PREMIUM_ROLES = ("premium", "author", "critic", "adjudicator", "prbody", "writer")


def _today() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


def _month() -> str:
    return _today()[:7]


def _day_of(row: dict) -> str:
    return row.get("day") or (row.get("first_ts") or "")[:10]


def rows(state_root, since: str = "") -> list[dict]:
    out = [r for r in ledger.collect(pathlib.Path(state_root)) if not since or _day_of(r) >= since]
    return sorted(out, key=_day_of)


def spent(state_root, *, day: str = "", month: str = "", roles: tuple = ()) -> float:
    total = 0.0
    for r in rows(state_root):
        d = _day_of(r)
        if day and d != day:
            continue
        if month and not d.startswith(month):
            continue
        if roles and not any(x in (r.get("role") or "") or x in (r.get("stage") or "") for x in roles):
            continue
        total += float(r.get("billed_usd") or 0.0)
    return total


def group(rs: list[dict], key: str) -> list[tuple]:
    """[(name, dollars, calls)] descending by dollars. `key` is a row field: ticket/model/stage/day."""
    acc = collections.defaultdict(lambda: [0.0, 0])
    for r in rs:
        name = (_day_of(r) if key == "day" else r.get(key)) or "(none)"
        acc[name][0] += float(r.get("billed_usd") or 0.0)
        acc[name][1] += 1
    return sorted(((n, v[0], v[1]) for n, v in acc.items()), key=lambda x: -x[1])


def bar(value: float, peak: float, width: int = 24) -> str:
    """Eighth-block bar so small values stay visible instead of rounding to nothing."""
    if peak <= 0:
        return ""
    units = max(0.0, value) / peak * width
    full = int(units)
    rem = units - full
    tail = "▏▎▍▌▋▊▉█"[min(7, int(rem * 8))] if rem > 0.02 else ""
    return ("█" * full + tail)[:width] or "▏"


def render(state_root, since: str = "", by: tuple = ("ticket", "model", "stage")) -> str:
    rs = rows(state_root, since)
    if not rs:
        return f"no recorded spend{' since ' + since if since else ''}"
    total = sum(float(r.get("billed_usd") or 0.0) for r in rs)
    days = sorted({_day_of(r) for r in rs if _day_of(r)})
    out = [f"agent-loop spend   ${total:,.2f} over {len(rs)} calls"
           f"   {days[0]} .. {days[-1]}" if days else f"agent-loop spend   ${total:,.2f}"]
    for key in by:
        rowset = group(rs, key)
        if not rowset:
            continue
        peak = max(v for _, v, _ in rowset) or 1.0
        width = min(34, max(len(str(n)) for n, _, _ in rowset))
        out.append("")
        out.append(f"by {key}")
        for name, dollars, calls in rowset[:14]:
            share = dollars / total * 100 if total else 0
            out.append(f"  {str(name)[:width]:<{width}}  ${dollars:8,.2f}  {share:5.1f}%  "
                       f"{calls:4d} calls  {bar(dollars, peak)}")
        if len(rowset) > 14:
            rest = sum(d for _, d, _ in rowset[14:])
            out.append(f"  {'(' + str(len(rowset) - 14) + ' more)':<{width}}  ${rest:8,.2f}")
    return "\n".join(out)


def per_ticket_detail(state_root, ticket: str) -> str:
    """One ticket: where its money went, by stage and model."""
    rs = [r for r in rows(state_root) if (r.get("ticket") or "") == ticket]
    if not rs:
        return f"{ticket}: no recorded spend"
    total = sum(float(r.get("billed_usd") or 0.0) for r in rs)
    out = [f"{ticket}   ${total:,.2f} over {len(rs)} calls"]
    for key in ("stage", "model"):
        rowset = group(rs, key)
        peak = max(v for _, v, _ in rowset) or 1.0
        out.append("")
        out.append(f"by {key}")
        width = min(34, max(len(str(n)) for n, _, _ in rowset))
        for name, dollars, calls in rowset:
            out.append(f"  {str(name)[:width]:<{width}}  ${dollars:8,.2f}  {dollars/total*100:5.1f}%  "
                       f"{calls:4d} calls  {bar(dollars, peak)}")
    return "\n".join(out)
