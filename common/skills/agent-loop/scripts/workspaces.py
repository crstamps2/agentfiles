"""Shared workspace selection and lifecycle; app mechanics live in adapters."""
from __future__ import annotations

import dataclasses
import importlib
import importlib.util
import json
import pathlib
import subprocess
import typing

import state
from workspace_binding import BACKENDS, Binding, BindingError, read, write


class Presentation(typing.NamedTuple):
    spinner: bool
    text: str
    color: str
    icon: str


@dataclasses.dataclass
class LaunchResult:
    status: str
    binding: Binding
    warnings: tuple[str, ...] = ()


@dataclasses.dataclass
class Snapshot:
    binding: Binding
    identity: str
    lane: str
    agent_live: bool | None = None
    dev_live: bool | None = None


@dataclasses.dataclass
class WorkspaceBackend:
    cfg: typing.Any
    name: str
    binding: Binding | None

    def adapter(self, *, run=None):
        module = importlib.import_module(f"workspace_{self.name}")
        return module.Backend(self.cfg, run=run or subprocess.run,
                              checkpoint=lambda b: write(self.cfg.ticket_dir(b.key), b))


def backend_for(cfg, ticket: state.Ticket, *, new_launch: bool = False, override: str | None = None) -> WorkspaceBackend:
    if override is not None and override not in BACKENDS:
        raise BindingError("Unknown workspace backend override")
    binding = read(cfg.ticket_dir(ticket.key))
    if binding:
        if override is not None and override != binding.backend:
            raise BindingError("Override conflicts with recorded workspace backend")
        name = binding.backend
    elif new_launch and not (ticket.worktree or ticket.workspace):
        name = override or cfg.workspace_backend
    else:
        if override is not None:
            raise BindingError("Existing legacy workspace cannot be relocated by override")
        name = "cmux"
    if name not in BACKENDS:
        raise BindingError("Unknown workspace backend")
    return WorkspaceBackend(cfg, name, binding)


def load_helper(cfg, name):
    path = pathlib.Path(cfg.cmux_chain_dir) / f"{name}.py"
    try:
        spec = importlib.util.spec_from_file_location(f"agent_loop_{name}_{path.stat().st_mtime_ns}", path)
        if spec is None or spec.loader is None:
            raise WorkspaceError("workspace-helper-unavailable", name)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except (OSError, ImportError) as exc:
        raise WorkspaceError("workspace-helper-unavailable", name) from exc


class WorkspaceError(RuntimeError):
    def __init__(self, status: str, detail: str = ""):
        super().__init__(f"{status}: {detail}" if detail else status)
        self.status = status


class DefiniteFailure(WorkspaceError):
    """A proved failure, not an ambiguous mutation or observational timeout."""


class Adapter:
    def __init__(self, cfg, *, run, checkpoint):
        self.cfg, self.run, self.checkpoint = cfg, run, checkpoint

    def operation(self, binding: Binding, stage: str, payload: list, action) -> dict:
        previous = binding.operations.get(stage, {})
        if previous:
            if previous.get("payload") != payload:
                raise WorkspaceError("workspace-payload-conflict", stage)
            if previous.get("status") == "done":
                return previous["result"]
            if previous.get("status") == "pending":
                raise WorkspaceError("workspace-uncertain", stage)
        record = {"status": "pending", "payload": payload}
        binding.operations[stage] = record
        self.checkpoint(binding)
        try:
            result = action()
            record.update(status="done", result=result)
            self.checkpoint(binding)
            return result
        except Exception as exc:
            binding.phase = "uncertain"
            captured = getattr(exc, "output", None) or getattr(exc.__cause__, "output", None)
            if captured:
                try:
                    data = json.loads(captured)
                    if isinstance(data, dict) and isinstance(data.get("operationId"), str):
                        record["operation_id"] = data["operationId"]
                except (ValueError, TypeError):
                    record["receipt_unreadable"] = True
            self.checkpoint(binding)
            if isinstance(exc, WorkspaceError):
                raise
            raise WorkspaceError("workspace-uncertain", f"{stage}: {type(exc).__name__}") from exc


def _completed(binding: Binding) -> LaunchResult:
    ready = binding.operations.get("serving", {}).get("result", {}).get("ready")
    warnings = ()
    if binding.backend == "orca" and binding.agent.get("kind") == "terminal":
        warnings = ("Orca supplied one terminal fallback for requested Pi chat; retained that agent.",)
    return LaunchResult("ok" if ready else "serving-timeout", binding, warnings)


def _execute(cfg, selection: WorkspaceBackend, binding: Binding, prompt: str, ref_url: str) -> LaunchResult:
    if binding.phase == "uncertain" or any(v.get("status") == "pending" for v in binding.operations.values()):
        raise WorkspaceError("workspace-uncertain", "Inspect recorded resources; no mutation was retried")
    adapter = selection.adapter()
    write(cfg.ticket_dir(binding.key), binding)
    try:
        adapter.prepare(binding)
        if binding.workspace_id:
            adapter.inspect(binding)
        else:
            adapter.create(binding)
        if binding.phase == "ready":
            return _completed(binding)
        adapter.setup(binding)
        adapter.start_dev(binding)
        adapter.start_agent(binding, prompt)
        adapter.ensure_views(binding, ref_url)
        binding.phase = "ready"
        write(cfg.ticket_dir(binding.key), binding)
        return _completed(binding)
    except DefiniteFailure as exc:
        binding.phase = "setup-failed"
        write(cfg.ticket_dir(binding.key), binding)
        return LaunchResult(exc.status, binding)


def launch(cfg, key: str, branch: str, prompt: str, ref_url: str, *, backend: str | None = None) -> LaunchResult:
    ticket = state.load(cfg.ticket_dir(key))
    selection = backend_for(cfg, ticket, new_launch=True, override=backend)
    if selection.binding is None and (ticket.worktree or ticket.workspace):
        raise WorkspaceError("legacy-workspace-requires-recovery", key)
    binding = selection.binding or Binding(backend=selection.name, key=key, branch=branch)
    if binding.branch != branch:
        raise BindingError("Launch branch conflicts with recorded branch")
    return _execute(cfg, selection, binding, prompt, ref_url)


def retry_setup(cfg, ticket: state.Ticket, prompt: str, ref_url: str) -> LaunchResult:
    selection = backend_for(cfg, ticket)
    binding = selection.binding or Binding(backend="cmux", key=ticket.key, worktree=ticket.worktree,
                                           branch=ticket.branch, workspace_id=ticket.workspace)
    if not binding.worktree or not pathlib.Path(binding.worktree).is_dir():
        raise WorkspaceError("workspace-missing", ticket.key)
    if ticket.worktree and pathlib.Path(ticket.worktree).resolve() != pathlib.Path(binding.worktree).resolve():
        raise BindingError("Recovery worktree conflicts with recorded binding")
    adapter = selection.adapter()
    if not binding.workspace_id:
        adapter.resolve_legacy(binding)
        write(cfg.ticket_dir(ticket.key), binding)
    return _execute(cfg, selection, binding, prompt, ref_url)
