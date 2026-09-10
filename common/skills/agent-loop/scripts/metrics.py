"""Append-only metrics.jsonl. One row per executed stage. Plan 2 adds tokens and cost."""
from __future__ import annotations
import datetime as dt
import fcntl
import json
import pathlib


def append(state_root, row: dict) -> None:
    p = pathlib.Path(state_root) / "metrics.jsonl"
    row = dict(row)
    row.setdefault("ts_utc", dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    with open(p, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps(row, sort_keys=True) + "\n")
        fcntl.flock(f, fcntl.LOCK_UN)


def read_all(state_root) -> list:
    p = pathlib.Path(state_root) / "metrics.jsonl"
    if not p.exists():
        return []
    rows = []
    lines = p.read_text().splitlines()
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            # A crash can tear only the final append; retain prior durable rows.
            if i == len(lines) - 1:
                break
            raise
    return rows
