"""Escalation ladder B: cheap ×2 (with gate feedback) → premium ×1 → blocked."""
from __future__ import annotations
import dataclasses
import pathlib

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
    p = pathlib.Path(task_dir) / "feedback.md"
    with open(p, "a") as f:
        f.write(f"## Attempt {attempt_n}\n{gate_summary.strip()}\n\n")
    return p
