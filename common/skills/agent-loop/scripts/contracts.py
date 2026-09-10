"""Worker result.md parsing (lenient format, strict meaning) and tasks.toml validation."""
from __future__ import annotations
import dataclasses
import pathlib
import re
import tomllib

STATUSES = {"pass", "fail", "blocked"}
REASONS = {"none", "implementation", "test", "environment", "timeout", "owner", "protocol"}
_KEY_RE = re.compile(r"^\s*(?:[#*\-]+\s*)?(status|reason|base|files|evidence|unverified|next)\s*[:=]\s*(.*)$", re.I)
_ID_RE = re.compile(r"^\d{3}$")


class ProtocolError(ValueError):
    pass


class ManifestError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class Result:
    status: str
    reason: str = ""
    base: str = ""
    files: list = dataclasses.field(default_factory=list)
    evidence: str = ""
    unverified: str = ""
    next: str = ""


def parse_result(text: str) -> Result:
    found = {}
    for line in text.splitlines():
        m = _KEY_RE.match(line)
        if m:
            found.setdefault(m.group(1).lower(), m.group(2).strip())
    status = found.get("status", "").lower()
    if status not in STATUSES:
        raise ProtocolError(f"result.md missing or invalid STATUS (got {found.get('status')!r})")
    reason = found.get("reason", "").lower()
    files = [f.strip() for f in re.split(r"[,\s]+", found.get("files", "")) if f.strip()]
    return Result(status=status, reason=reason, base=found.get("base", ""), files=files,
                  evidence=found.get("evidence", ""), unverified=found.get("unverified", ""),
                  next=found.get("next", ""))


@dataclasses.dataclass(frozen=True)
class Task:
    id: str
    slug: str
    summary: str
    allowed_files: list
    verification_commands: list
    acceptance: list
    may_edit_tests: bool = False
    visual: bool = False
    timeout_s: int = 4500
    invariants: list = dataclasses.field(default_factory=list)
    out_of_scope: list = dataclasses.field(default_factory=list)
    stop_when: list = dataclasses.field(default_factory=list)
    figma_nodes: list = dataclasses.field(default_factory=list)


_REQ_STR = ("id", "slug", "summary")
_REQ_LIST = ("allowed_files", "verification_commands", "acceptance")


def validate_tasks(data: dict) -> list:
    errs = []
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if not tasks:
        return ["tasks: manifest must contain at least one [[tasks]] entry"]
    seen = set()
    prev = ""
    for i, t in enumerate(tasks):
        p = f"tasks[{i}]"
        for k in _REQ_STR:
            if not isinstance(t.get(k), str) or not t.get(k).strip():
                errs.append(f"{p}.{k}: required non-empty string")
        for k in _REQ_LIST:
            v = t.get(k)
            if not isinstance(v, list) or not v:
                errs.append(f"{p}.{k}: required non-empty list")
        tid = str(t.get("id", ""))
        if tid and not _ID_RE.match(tid):
            errs.append(f"{p}.id: must match ^\\d{{3}}$ (got {tid!r})")
        if tid in seen:
            errs.append(f"{p}.id: duplicate id {tid}")
        seen.add(tid)
        if tid and prev and tid <= prev:
            errs.append(f"{p}.id: ids must be ascending ({prev} then {tid})")
        prev = tid or prev
        for k in ("may_edit_tests", "visual"):
            if k in t and not isinstance(t[k], bool):
                errs.append(f"{p}.{k}: must be boolean")
        if "timeout_s" in t and (not isinstance(t["timeout_s"], int) or t["timeout_s"] <= 0):
            errs.append(f"{p}.timeout_s: must be positive int")
    return errs


def load_tasks(path) -> list:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    errs = validate_tasks(data)
    if errs:
        raise ManifestError(f"{path}:\n  " + "\n  ".join(errs))
    fields = {f.name for f in dataclasses.fields(Task)}
    return [Task(**{k: v for k, v in t.items() if k in fields}) for t in data["tasks"]]
