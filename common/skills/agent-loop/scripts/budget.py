"""Spend admission for the autonomous loop.

`premium_daily_usd`, `premium_monthly_usd` and `total_unattended_daily_usd` were configured and
documented but never read by any production path (branch review 2026-09-23): the only ceiling that
ran was the per-ticket pre-implementation one. This module is the single place every paid dispatch
asks "may I spend?", answered from the same ledger the operator reads, so the number in hopper.toml
and the number enforced cannot drift.

Deliberately coarse: it reads settled usage, so concurrent starts can overshoot by roughly one call.
It is a spend CEILING, not an accountant; the alternative (reservations with a lock on every model
call) buys precision the loop does not need and adds a failure mode it cannot heal.
"""
import datetime as dt
import pathlib

import ledger

PREMIUM_ROLES = ("premium", "author", "critic", "adjudicator", "prbody", "writer")


def _today() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


def _month() -> str:
    return _today()[:7]


def spent(state_root: pathlib.Path, *, day: str | None = None, month: str | None = None,
          roles: tuple = ()) -> float:
    """Billed dollars in the ledger matching the given window and roles."""
    total = 0.0
    for r in ledger.collect(pathlib.Path(state_root)):
        d = (r.get("day") or (r.get("first_ts") or "")[:10])
        if day and d != day:
            continue
        if month and not d.startswith(month):
            continue
        if roles and not any(x in (r.get("role") or "") or x in (r.get("stage") or "") for x in roles):
            continue
        total += float(r.get("billed_usd") or 0.0)
    return total


def check(cfg, *, premium: bool) -> str:
    """Empty string when the dispatch may proceed, else the reason it may not."""
    b = getattr(cfg, "budgets", None)
    if b is None:
        return ""
    root = pathlib.Path(cfg.state_root)
    day_total = spent(root, day=_today())
    if b.total_unattended_daily_usd and day_total >= b.total_unattended_daily_usd:
        return (f"daily budget reached: ${day_total:.2f} spent today "
                f"(limit ${b.total_unattended_daily_usd:.2f})")
    if premium:
        pd = spent(root, day=_today(), roles=PREMIUM_ROLES)
        if b.premium_daily_usd and pd >= b.premium_daily_usd:
            return f"premium daily budget reached: ${pd:.2f} today (limit ${b.premium_daily_usd:.2f})"
        pm = spent(root, month=_month(), roles=PREMIUM_ROLES)
        if b.premium_monthly_usd and pm >= b.premium_monthly_usd:
            return f"premium monthly budget reached: ${pm:.2f} this month (limit ${b.premium_monthly_usd:.2f})"
    return ""
