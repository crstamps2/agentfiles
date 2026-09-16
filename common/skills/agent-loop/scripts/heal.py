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
    (r"^resource: on battery", "retry", 200),           # waits for AC; effectively unbounded but visible
    (r"^resource: ", "retry", 200),
    (r"^heavy lane held|worker slot|slot busy", "retry", 200),
    (r"verification timeout|heavy lane busy", "retry", 6),
    (r"^gates failed: yarn build", "prepare-retry", 3),
    (r"^gates failed: rails test", "prepare-retry", 2),
    (r"^visual gate failed", "prepare-retry", 3),
    (r"^guard failed on the BASE tree|contract complaint|guard disagreement", "repair", 4),
    (r"^guard repair left the manifest invalid|^plan failed|budget exceeded", "repair", 2),
    (r"^publish failed|^finalize failed|^mark ready failed|^reply failed|^adjudication unusable|^adjudication is not valid", "republish", 3),
    (r"^rebase (onto origin/main )?conflict", "human", 0),
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


def count_for(tdir: pathlib.Path, action: str, reason_key: str) -> int:
    return sum(1 for h in history(tdir) if h.get("action") == action and h.get("reason_key") == reason_key)


def reason_key(reason: str) -> str:
    """Stable bucket for 'the same problem again': the policy pattern that matched."""
    for pat, _a, _b in POLICIES:
        if re.search(pat, reason or "", re.I):
            return pat
    return "unknown"


def record(tdir: pathlib.Path, action: str, reason: str, stage: str, note: str = "") -> None:
    with open(_log_path(tdir), "a") as f:
        f.write(json.dumps({"ts": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "action": action,
                            "reason_key": reason_key(reason), "reason": (reason or "")[:300], "stage": stage, "note": note}) + "\n")


def decide(tdir: pathlib.Path, reason: str) -> tuple[str, str]:
    """(action, explanation). 'human' when the policy says so OR the budget for this reason is spent."""
    action, budget = policy(reason)
    if action == "human":
        return "human", "policy: needs an owner"
    n = count_for(tdir, action, reason_key(reason))
    if n >= budget:
        return "human", f"{action} already tried {n}x for this reason; budget {budget} spent"
    return action, f"{action} ({n + 1}/{budget})"
