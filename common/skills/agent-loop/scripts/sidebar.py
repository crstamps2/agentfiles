"""Mirror each ticket's state into its cmux workspace sidebar: a loading spinner while work is moving,
and one coloured status pill.

Cosmetic and best-effort. Every cmux call has a short timeout and every failure is swallowed, so a
missing cmux, a dead socket or a renamed workspace can never affect the loop.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import typing

import state

KEY = "agent-loop"
PRODUCTION_ROOT = pathlib.Path("~/.local/state/agent-loop").expanduser()
CMUX_FALLBACK = "/Applications/cmux.app/Contents/Resources/bin/cmux"
TIMEOUT_S = 5
GREEN, YELLOW, ORANGE, BLUE, GRAY = "#34C759", "#FFCC00", "#FF9500", "#4C8DFF", "#8E8E93"
STAGE_TEXT = {"queued": "starting", "spinup": "starting", "plan": "planning", "plan-review": "planning",
              "implement": "implementing", "gates": "gates", "draft-pr": "CI", "ready": "CI", "bot-loop": "bot review"}
PAST_REVIEW = frozenset(("human-gate-1", "colleague-loop", "human-gate-2", "done"))
OWNER_QUESTION = re.compile(r"need(?:s)? Cody|owner question|question for the owner|tier-[34]", re.I)


class Label(typing.NamedTuple):
    spinner: bool
    text: str
    color: str
    icon: str


def label(cfg, t: state.Ticket, queued: int | None = None) -> Label:
    tdir = cfg.ticket_dir(t.key)
    if t.state in PAST_REVIEW:
        try:
            pr = json.loads((tdir / "ci.json").read_text()).get("pr")
        except (OSError, ValueError):
            pr = None
        return Label(False, f"ready · PR #{pr}" if pr else "ready for review", BLUE, "checkmark.circle")
    if queued is not None:
        return Label(False, f"queued #{queued}", GRAY, "clock")
    if (tdir / "HUMAN").exists() or t.state == "decision-required" or OWNER_QUESTION.search(t.reason or ""):
        return Label(False, "needs Cody", ORANGE, "exclamationmark.bubble")
    active = t.recovery.get("active") if isinstance(t.recovery, dict) else None
    if isinstance(active, dict):
        if active.get("status") == "waiting-provider":
            return Label(False, "quota wait", YELLOW, "hourglass")
        if active.get("status") == "manual-safety-review":
            return Label(False, "needs approval", ORANGE, "exclamationmark.bubble")
        return Label(True, "self-healing", YELLOW, "wrench")
    if t.state in ("paused", "blocked"):
        return Label(True, "self-healing", YELLOW, "wrench")
    return Label(True, STAGE_TEXT.get(t.state, t.state), GREEN, "hammer")


def _cmux() -> str | None:
    return shutil.which("cmux") or (CMUX_FALLBACK if os.path.exists(CMUX_FALLBACK) else None)


def _workspaces(run, binary) -> dict[str, str]:
    out = run([binary, "workspace", "list"], capture_output=True, text=True, timeout=TIMEOUT_S).stdout
    return {m.group(2).strip(): m.group(1) for m in re.finditer(r"(workspace:\S+)\s+(.+)$", out or "", re.M)}


def _desired(cfg) -> dict[str, Label]:
    import cohort
    import drip
    root = cfg.state_root / "tickets"
    tickets = {d.name: state.load(d) for d in sorted(root.iterdir()) if (d / "state.json").is_file()} if root.exists() else {}
    members = set(cohort.members(cfg)) if drip.cohort_enabled(cfg) else set(tickets)
    order = {key: n for n, key in enumerate(drip.parked(cfg), 1)}
    want = {}
    for key, t in tickets.items():
        if t.state in PAST_REVIEW or key in members:
            want[key] = label(cfg, t)
        elif key in order:
            want[key] = label(cfg, t, queued=order[key])
    return want


def sync(cfg, *, run=subprocess.run, force: bool = False) -> None:
    """Best-effort: never raises. Only touches the real sidebar for the production state root."""
    try:
        if not force and pathlib.Path(cfg.state_root).resolve() != PRODUCTION_ROOT.resolve():
            return                                    # tests and dry state roots must not repaint Cody's sidebar
        binary = _cmux() or "cmux"
        refs = _workspaces(run, binary)
        cache_path = cfg.state_root / "sidebar.json"
        try:
            cache = json.loads(cache_path.read_text())
        except (OSError, ValueError):
            cache = {}
        for key, want in _desired(cfg).items():
            ref = refs.get(key.lower())
            if not ref:
                continue
            signature = [ref, *want]
            if cache.get(key) == signature:
                continue
            try:
                run([binary, "workspace", "loading", "on" if want.spinner else "off", "--id", KEY, "--workspace", ref],
                    capture_output=True, text=True, timeout=TIMEOUT_S)
                run([binary, "set-status", KEY, want.text, "--icon", want.icon, "--color", want.color,
                     "--priority", "90", "--workspace", ref], capture_output=True, text=True, timeout=TIMEOUT_S)
            except Exception:  # noqa: BLE001 -- cosmetic; retry on the next pass
                continue
            cache[key] = signature
        tmp = cache_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cache, indent=2))
        os.replace(tmp, cache_path)
    except Exception:  # noqa: BLE001 -- the sidebar must never affect the loop
        return
