"""Self-healing: every `paused` reason has a recovery policy the runner applies itself before it
ever waits for a human.

The loop's failure pattern all week: the runner DETECTS a problem correctly, then pauses -- and
the fix was mechanical every time (rerun after a transient, re-prepare the worktree, hand a bad
manifest to the task-writer, re-run publish after a code fix, retry a flaky gate). A human's only
contribution was typing `resume`. This module makes `resume` a policy, with a per-reason retry
budget so a genuinely stuck ticket still surfaces -- with the count and history attached.

`policy(reason)` -> (action, budget). Actions:
  retry         re-enter the current stage (transients: resources, slot/lane contention, timeouts)
  prepare-retry `bin/wt prepare --for rails` then re-enter (stale deps/migrations after a rebase)
  repair        hand the recorded evidence to the task-writer, then re-enter (bad guards, contract complaints)
  fix-task      queue a runner-assigned fix task for named lint offenses, then re-enter implement
  republish     re-run the publish step for the accepted task (publish/push defects fixed in code)
  human         genuinely needs an owner (design questions, hand-written merge conflicts, HUMAN file)

Every automatic recovery is appended to `tickets/<K>/heal.jsonl` so the pane and the morning
report show what healed itself and how often.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import re

POLICIES = [
    # (regex on the pause reason, action, max automatic recoveries for this reason per ticket)
    (r"^provider unavailable|usage limit|rate.?limit", "retry", 100),   # clears on its own; never a plan/worker fault
    (r"^resource: on battery", "retry", 200),           # waits for AC; effectively unbounded but visible
    (r"^resource: ", "retry", 200),
    (r"^heavy lane held|worker slot|slot busy", "retry", 200),
    (r"verification timeout|heavy lane busy", "retry", 6),
    (r"critic produced no review", "retry", 5),          # the critic never ran; nothing was judged
    (r"^gates failed: pre-commit lint", "fix-task", 3),   # offenses in OUR work: a worker fixes them
    (r"^gates failed: yarn build", "prepare-retry", 3),
    (r"^gates failed: rails test", "prepare-retry", 2),
    (r"^visual gate failed", "prepare-retry", 3),
    (r"^guard failed on the BASE tree|contract complaint|guard disagreement", "repair", 4),
    (r"^guard repair left the manifest invalid", "repair", 2),
    (r"^plan failed|budget exceeded|task-writer left the manifest invalid", "replan", 3),   # -> plan stage; it repairs from the revise review when one exists
    (r"^publish failed|^finalize failed|^mark ready failed|^reply failed|^adjudication unusable|^adjudication is not valid", "republish", 3),
    (r"^rebase (onto origin/main )?conflict", "human", 0),
    (r"^planning ceiling", "human", 0),
    (r"need Cody|NEEDS CODY|^HUMAN file|owner question|tier-3", "human", 0),
]


def policy(reason: str) -> tuple[str, int]:
    for pat, action, budget in POLICIES:
        if re.search(pat, reason or "", re.I):
            return action, budget
    return "retry", 2                                     # unknown pause: one careful retry, then a human


def _log_path(tdir: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(tdir) / "heal.jsonl"


def history(tdir: pathlib.Path) -> list[dict]:
    p = _log_path(tdir)
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


OPERATOR_RESUME = "operator-resume"


def mark_operator_resume(tdir: pathlib.Path, note: str = "") -> None:
    """An operator put the ticket back in motion (usually after fixing the runner): budgets restart."""
    record(tdir, OPERATOR_RESUME, "", "", note)


def active_history(tdir: pathlib.Path) -> list[dict]:
    """Heals since the last operator resume. Budgets count these; history() keeps the full trail."""
    hist = history(tdir)
    last = max((i for i, h in enumerate(hist) if h.get("action") == OPERATOR_RESUME), default=-1)
    return hist[last + 1:]


def count_for(tdir: pathlib.Path, action: str, reason_key: str) -> int:
    return sum(1 for h in active_history(tdir) if h.get("action") == action and h.get("reason_key") == reason_key)


def reason_key(reason: str) -> str:
    """Stable bucket for 'the same problem again': the policy pattern that matched."""
    for pat, _a, _b in POLICIES:
        if re.search(pat, reason or "", re.I):
            return pat
    return "unknown"


def record(tdir: pathlib.Path, action: str, reason: str, stage: str, note: str = "") -> None:
    with open(_log_path(tdir), "a") as f:
        f.write(json.dumps({"ts": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "action": action,
                            "reason_key": reason_key(reason), "signature": signature(reason),
                            "reason": (reason or "")[:300], "stage": stage, "note": note}) + "\n")


BACKOFF_S = {"provider": 1800}      # a provider usage limit clears on a provider clock, not ours


def cooling_off(tdir: pathlib.Path, reason: str) -> int:
    """Seconds still to wait before retrying THIS reason, 0 if ready. Keeps a launchd 3-minute restart
    loop from burning a retry budget against a provider limit that resets hourly."""
    import time as _t
    if not re.search(r"provider unavailable|usage limit|rate.?limit", reason or "", re.I):
        return 0
    hist = [h for h in active_history(tdir) if h.get("reason_key") == reason_key(reason)]
    if not hist:
        return 0
    last = hist[-1].get("ts", "")
    try:
        t0 = dt.datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp()
    except ValueError:
        return 0
    return max(0, int(BACKOFF_S["provider"] - (_t.time() - t0)))


MAX_HEALS_PER_TICKET = 25          # any reason, any action: a ticket that has healed this often is broken
MAX_SAME_SIGNATURE = 4             # the same failure evidence recurring is a defect, not a transient


def signature(reason: str) -> str:
    """Failure identity with volatile parts removed, so renaming a reason cannot buy a fresh budget."""
    import hashlib
    norm = re.sub(r"\d+", "N", reason or "")
    norm = re.sub(r"/[\w./-]+/", "/P/", norm)
    norm = re.sub(r"\s+", " ", norm).strip().lower()[:300]
    return hashlib.sha256(norm.encode()).hexdigest()[:12]


TRANSIENT = re.compile(r"^resource:|^heavy lane|heavy lane busy|provider unavailable|usage limit|rate.?limit", re.I)


def decide(tdir: pathlib.Path, reason: str) -> tuple[str, str]:
    """(action, explanation). 'human' when the policy says so OR the budget for this reason is spent."""
    action, budget = policy(reason)
    if action == "human":
        return "human", "policy: needs an owner"
    # Per-reason budgets are not a ticket ceiling: the same root defect can present under several
    # reasons (and heal actions rewrite reason text), so a ticket could heal indefinitely. Two global
    # stops, both owner decisions (branch review 2026-09-23).
    hist = [h for h in active_history(tdir) if not TRANSIENT.search(h.get("reason") or "")]
    if len(hist) >= MAX_HEALS_PER_TICKET:
        return "human", f"ticket has self-healed {len(hist)} times; that is a defect, not a transient"
    sig = signature(reason)
    same = [h for h in hist if h.get("signature") == sig]
    # A machine that is busy or a provider at its limit recurs by nature; its own retry budget bounds it.
    if len(same) >= MAX_SAME_SIGNATURE and not TRANSIENT.search(reason or ""):
        return "human", f"the same failure has recurred {len(same)} times ({sig}); healing is not fixing it"
    wait = cooling_off(tdir, reason)
    if wait:
        return "wait", f"provider cooling off; {wait // 60} min left"
    n = count_for(tdir, action, reason_key(reason))
    if n >= budget:
        return "human", f"{action} already tried {n}x for this reason; budget {budget} spent"
    return action, f"{action} ({n + 1}/{budget})"
