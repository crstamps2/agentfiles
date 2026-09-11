"""Escalation ladder B: cheap ×2 (with gate feedback) → premium ×1 → blocked."""
from __future__ import annotations
import dataclasses
import pathlib

import attempt as attempt_mod

CHEAP_ARMS = ("cloud", "local")
ADVANCING = {"rejected", "timeout", "protocol", "unusable"}
OUTCOMES = ADVANCING | {"accepted", "environment"}


@dataclasses.dataclass(frozen=True)
class Rung:
    agent: str
    tier: str      # "cheap" | "premium"
    n: int


@dataclasses.dataclass(frozen=True)
class Attempt:
    rung: Rung
    outcome: str   # accepted | rejected | timeout | protocol | unusable | environment


def assign_arm(task_index: int, visual: bool, pin: str | None, alternate: list) -> str:
    if pin is not None:
        if pin not in CHEAP_ARMS:
            raise ValueError(f"pin_arm must be one of {CHEAP_ARMS}, got {pin!r}")
        return pin
    if not alternate:
        raise ValueError("arms.alternate must be non-empty")
    return alternate[task_index % len(alternate)]


def _sequence(arm: str) -> list:
    return [Rung(f"{arm}-worker", "cheap", 1), Rung(f"{arm}-worker", "cheap", 2), Rung("premium-worker", "premium", 1)]


def next_rung(attempts: list, arm: str) -> Rung | None:
    for a in attempts:
        if a.outcome not in OUTCOMES:
            raise ValueError(f"unknown attempt outcome {a.outcome!r}")
    # attempts are appended in call order; an accepted outcome is always last
    if attempts and attempts[-1].outcome == "accepted":
        return None
    consumed = sum(1 for a in attempts if a.outcome in ADVANCING)
    seq = _sequence(arm)
    return seq[consumed] if consumed < len(seq) else None


def append_feedback(task_dir, attempt_n: int, gate_summary: str) -> pathlib.Path:
    """Append via the no-follow discipline: read the old text with safe_read (missing file
    means "no feedback yet"; a symlink raises UnsafePath and propagates), then rewrite
    atomically with safe_rewrite (which creates the file via safe_write if absent)."""
    p = pathlib.Path(task_dir) / "feedback.md"
    try:
        old = attempt_mod.safe_read(p)
    except FileNotFoundError:
        old = ""
    new_text = old + f"## Attempt {attempt_n}\n{gate_summary.strip()}\n\n"
    attempt_mod.safe_rewrite(p, new_text)
    return p


def next_action(history: list, arm: str, outcome: str, env_failures: int) -> str:
    """The lifecycle decision derived from the ladder, computed once at CLASSIFIED and
    persisted (see the design's `next_action` field). `history` is prior attempts on this
    lineage; `outcome` is this attempt's outcome (its rung is `next_rung(history, arm)`)."""
    if outcome == "blocked":
        return "block"
    this_rung = next_rung(history, arm)
    if this_rung is None:
        combined = history
    else:
        combined = history + [Attempt(this_rung, outcome)]
    if next_rung(combined, arm) is None and outcome != "accepted":
        return "block"
    if outcome == "environment" and env_failures >= 2:
        return "pause-env"
    return "none"
