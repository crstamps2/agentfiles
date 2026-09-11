"""Append-only metrics.jsonl. One row per executed stage, keyed by (attempt_id, stage_kind,
idx). Plan 2 adds tokens and cost."""
from __future__ import annotations
import datetime as dt
import fcntl
import json
import pathlib


class MetricsCorrupt(RuntimeError):
    """Raised when metrics.jsonl has an invalid record before the trailing (repairable)
    tail. append() writes nothing when this is raised."""
    pass


def _repair_offset(data: str) -> int:
    """Return the byte offset just after the last complete, `\\n`-terminated, valid-JSON
    line in `data`. Everything after that offset -- a torn tail, or even a syntactically
    valid but unterminated final object -- is dropped: per the design, an unterminated
    record was never durably committed. Raises MetricsCorrupt if any non-blank line
    *before* that offset fails to parse (interior corruption is never silently repaired)."""
    lines = data.splitlines(keepends=True)
    offsets = []
    cum = 0
    for line in lines:
        cum += len(line)
        offsets.append(cum)

    def parses(line: str) -> bool:
        content = line[:-1] if line.endswith("\n") else line
        if not content.strip():
            return True  # blank lines are inert, never flagged as corruption
        try:
            json.loads(content)
            return True
        except json.JSONDecodeError:
            return False

    def is_candidate(line: str) -> bool:
        content = line[:-1] if line.endswith("\n") else line
        return line.endswith("\n") and bool(content.strip()) and parses(line)

    last_good = -1
    for i, line in enumerate(lines):
        if is_candidate(line):
            last_good = i

    for i in range(max(last_good, 0)):
        if i == last_good:
            continue
        line = lines[i]
        content = line[:-1] if line.endswith("\n") else line
        if content.strip() and not parses(line):
            raise MetricsCorrupt(f"metrics.jsonl: invalid record before the repairable tail (line {i + 1})")

    return offsets[last_good] if last_good != -1 else 0


def append(state_root, row: dict) -> None:
    p = pathlib.Path(state_root) / "metrics.jsonl"
    row = dict(row)
    row.setdefault("ts_utc", dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    with open(p, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.seek(0)
            data = f.read()
            offset = _repair_offset(data)
            if offset != len(data):
                f.seek(0)
                f.truncate()
                f.write(data[:offset])
                f.flush()
            f.seek(0, 2)
            f.write(json.dumps(row, sort_keys=True) + "\n")
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def read_all(state_root) -> list:
    p = pathlib.Path(state_root) / "metrics.jsonl"
    if not p.exists():
        return []
    text = p.read_text()
    lines = text.splitlines(keepends=True)
    rows = []
    for i, raw in enumerate(lines):
        terminated = raw.endswith("\n")
        line = raw[:-1] if terminated else raw
        if not line.strip():
            continue
        if not terminated:
            # An unterminated final line was never durably committed, matching append()'s
            # repair semantics -- even if it happens to parse.
            if i == len(lines) - 1:
                break
            raise json.JSONDecodeError("unterminated line before end of file", raw, 0)
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            # A crash can tear only the final append; retain prior durable rows.
            if i == len(lines) - 1:
                break
            raise
    return rows


def has(state_root, attempt_id: str, stage_kind: str, idx) -> bool:
    """True iff a row for this (attempt_id, stage_kind, idx) key already exists -- used to
    make per-stage metrics projection idempotent."""
    for row in read_all(state_root):
        if (row.get("attempt_id") == attempt_id and row.get("stage_kind") == stage_kind
                and row.get("idx") == idx):
            return True
    return False
