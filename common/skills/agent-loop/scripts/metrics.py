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
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
