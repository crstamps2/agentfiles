"""Escalation ladder B': cheap ×2 (with gate feedback) → premium ×2 (with gate feedback) → blocked.

Premium got a second rung on 2026-09-14: Terra's only shot at ZIP-7873/006 did the cop work correctly and
missed one mechanical final step (regenerate the grandfather list); blocking a ticket at 6/8 for that costs
far more than one more premium attempt. The daily/total premium USD caps still bound spend."""
from __future__ import annotations
import dataclasses
import pathlib

import attempt as attempt_mod

CHEAP_ARMS = ("cloud", "local", "small")
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
    if arm == "small":
        # Small edit tasks (planner-tagged: 1-2 existing files, no browser): the free local model gets
        # the first shot -- it is 3/3 on edit-existing-file work when the task fits its 32K window --
        # then Sonnet, then premium. Everything else never touches local.
        return [Rung("local-worker", "cheap", 1), Rung("cloud-worker", "cheap", 2),
                Rung("premium-worker", "premium", 1), Rung("premium-worker", "premium", 2)]
    return [Rung(f"{arm}-worker", "cheap", 1), Rung(f"{arm}-worker", "cheap", 2),
            Rung("premium-worker", "premium", 1), Rung("premium-worker", "premium", 2)]


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
    new_text = old + f"## Attempt {attempt_n}\n{summarize_feedback(gate_summary)}\n\n"
    attempt_mod.safe_rewrite(p, new_text)
    return p


def summarize_feedback(gate_summary: str, max_items: int = 8) -> str:
    """The field guide's rule: two sentences about what the gate saw, not a wall. A runner
    reason is often `path: why; path: why; ...` -- group by the `why` clause, keep the first
    few paths per group, and state the counts. Anything else passes through trimmed."""
    text = gate_summary.strip()
    parts = [x.strip() for x in text.split("; ") if x.strip()]
    if len(parts) <= max_items or not all(": " in x for x in parts):
        return text[:2000]
    groups: dict[str, list] = {}
    for x in parts:
        path, why = x.split(": ", 1)
        groups.setdefault(why, []).append(path)
    lines = [f"{len(parts)} findings in {len(groups)} group(s):"]
    def _artifact(path: str) -> bool:      # sort likely build artifacts last so source paths surface
        return path.split("/", 1)[0] in ("tmp", "node_modules", "log", "coverage", "public") or path.startswith(".")
    for why, paths in groups.items():
        paths = sorted(paths, key=_artifact)
        shown = ", ".join(paths[:3]) + (f", … (+{len(paths)-3} more)" if len(paths) > 3 else "")
        lines.append(f"- {len(paths)}× {why}: {shown}")
    lines.append("Fix the source-file findings; build artifacts and caches are not yours to clean.")
    return "\n".join(lines)


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
