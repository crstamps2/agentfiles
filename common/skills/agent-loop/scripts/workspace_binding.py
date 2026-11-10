"""Validated workspace identity, separate from legacy writers of state.json."""
from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import re
import tempfile

BACKENDS = ("orca", "cmux")
MAX_BYTES = 1_000_000


class BindingError(ValueError):
    pass


@dataclasses.dataclass
class Binding:
    backend: str
    key: str
    version: int = 1
    environment: str = "local"
    worktree: str = ""
    branch: str = ""
    workspace_id: str = ""
    repo_id: str = ""
    terminals: dict = dataclasses.field(default_factory=dict)
    agent: dict = dataclasses.field(default_factory=dict)
    views: dict = dataclasses.field(default_factory=dict)
    phase: str = "launching"
    operations: dict = dataclasses.field(default_factory=dict)


def validate(binding: Binding, ticket_dir: pathlib.Path) -> None:
    if type(binding.version) is not int or binding.version != 1 or binding.backend not in BACKENDS:
        raise BindingError("Unsupported workspace binding version/backend")
    if not isinstance(binding.key, str) or not re.fullmatch(r"[A-Z][A-Z0-9]*-[1-9][0-9]*", binding.key):
        raise BindingError("Invalid ticket key")
    if pathlib.Path(ticket_dir).name != binding.key or binding.environment != "local":
        raise BindingError("Workspace binding ticket/environment mismatch")
    for name in ("worktree", "branch", "workspace_id", "repo_id"):
        if not isinstance(getattr(binding, name), str):
            raise BindingError(f"Invalid {name}")
    if binding.worktree and not pathlib.Path(binding.worktree).is_absolute():
        raise BindingError("Worktree path must be absolute")
    if binding.phase not in ("launching", "setup-failed", "ready", "uncertain"):
        raise BindingError("Unknown workspace phase")
    for name in ("terminals", "views"):
        values = getattr(binding, name)
        if not isinstance(values, dict) or any(not isinstance(k, str) or not isinstance(v, str) or not v for k, v in values.items()):
            raise BindingError(f"Invalid {name} identities")
    if not isinstance(binding.agent, dict):
        raise BindingError("Invalid agent identity")
    if binding.agent:
        if binding.agent.get("kind") not in ("chat", "terminal") or not isinstance(binding.agent.get("handle"), str) or not binding.agent["handle"]:
            raise BindingError("Missing agent surface/handle")
        if binding.agent["kind"] == "chat" and (not isinstance(binding.agent.get("session_id"), str) or not binding.agent["session_id"]):
            raise BindingError("Chat requires its actual session ID")
    if not isinstance(binding.operations, dict) or any(
        not isinstance(k, str) or not isinstance(v, dict) or v.get("status") not in ("pending", "done", "failed")
        for k, v in binding.operations.items()
    ):
        raise BindingError("Invalid operation journal")


def read(ticket_dir: pathlib.Path) -> Binding | None:
    path = pathlib.Path(ticket_dir) / "workspace.json"
    try:
        if path.is_symlink():
            raise BindingError("Workspace record cannot be a symlink")
        if path.stat().st_size > MAX_BYTES:
            raise BindingError("Workspace record too large")
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise BindingError(f"Cannot read workspace binding: {exc}") from exc
    try:
        binding = Binding(**raw)
        validate(binding, ticket_dir)
    except (TypeError, ValueError) as exc:
        raise BindingError(f"Invalid workspace binding: {exc}") from exc
    return binding


def write(ticket_dir: pathlib.Path, binding: Binding) -> None:
    ticket_dir = pathlib.Path(ticket_dir)
    validate(binding, ticket_dir)
    previous = read(ticket_dir)
    if previous:
        for name in ("backend", "key", "environment", "worktree", "branch", "workspace_id", "repo_id"):
            old = getattr(previous, name)
            if old and old != getattr(binding, name):
                raise BindingError(f"Refusing to replace recorded {name}")
    encoded = json.dumps(dataclasses.asdict(binding), indent=2)
    if len(encoded.encode()) > MAX_BYTES:
        raise BindingError("Workspace record too large")
    ticket_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=ticket_dir, prefix=".workspace-", delete=False) as out:
            tmp = pathlib.Path(out.name)
            os.chmod(tmp, 0o600)
            out.write(encoded)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, ticket_dir / "workspace.json")
        fd = os.open(ticket_dir, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
