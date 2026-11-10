"""Shared workspace selection and lifecycle; app mechanics live in adapters."""
from __future__ import annotations

import contextlib
import dataclasses
import importlib
import importlib.util
import json
import os
import pathlib
import subprocess
import tempfile
import typing

import state
from workspace_binding import BACKENDS, Binding, BindingError, read, validate, write

PRODUCTION_ROOT = pathlib.Path("~/.local/state/agent-loop").expanduser()


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

    def adapter(self, *, run=None, checkpoint=None):
        module = importlib.import_module(f"workspace_{self.name}")
        return module.Backend(self.cfg, run=run or subprocess.run,
                              checkpoint=checkpoint or (lambda b: write(self.cfg.ticket_dir(b.key), b)))


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
        self.before_move = lambda: True

    def verify_resources(self, binding: Binding) -> None:
        """Strict resume preflight, separate from cosmetic workspace observation."""

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
            adapter.verify_resources(binding)
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


def write_record(path: pathlib.Path, value) -> None:
    """Persist a private shared-policy intent/receipt before any corresponding app write."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}-", delete=False) as stream:
            temporary = pathlib.Path(stream.name)
            json.dump(value, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def production_ticket(ticket_dir) -> bool:
    path = pathlib.Path(ticket_dir)
    return len(path.parents) >= 2 and path.parent.parent.resolve() == PRODUCTION_ROOT.resolve()


def notify(ticket_dir, key: str, title: str, body: str, *, run=None) -> bool:
    if not production_ticket(ticket_dir):
        return False
    binding = read(pathlib.Path(ticket_dir))
    if binding is None:
        ticket = state.load(pathlib.Path(ticket_dir))
        if ticket.key != key:
            return False
        binding = Binding(backend="cmux", key=key, worktree=ticket.worktree,
                          branch=ticket.branch, workspace_id=ticket.workspace)
    if binding.key != key:
        return False
    adapter = WorkspaceBackend(None, binding.backend, binding).adapter(run=run)
    adapter.notify(binding, title, body)
    return True


def adopt(cfg, key: str, binding: Binding, *, run=None) -> Binding:
    """Explicit metadata enrollment. Hold existing writer/spawner leases; never run recovery."""
    import locks

    tdir = cfg.ticket_dir(key)
    validate(binding, tdir)
    if binding.key != key or binding.backend != "orca" or binding.agent.get("kind") != "terminal":
        raise WorkspaceError("adoption-requires-explicit-orca-terminal")
    if not (tdir / "state.json").is_file():
        raise WorkspaceError("adoption-missing-ticket")
    with contextlib.ExitStack() as held:
        for name in ("drip", "runner", f"runner-{key.lower()}"):
            path = cfg.state_root / "locks" / name
            owner = locks.owner(path)
            if path.exists():
                if not isinstance(owner, dict) or not isinstance(owner.get("boot_id"), str) or not owner["boot_id"]:
                    raise WorkspaceError("adoption-writer-unconfirmed")
                try:
                    pid = int(owner["pid"])
                    if pid <= 0:
                        raise ValueError("Invalid writer PID")
                except (KeyError, ValueError, TypeError) as exc:
                    raise WorkspaceError("adoption-writer-unconfirmed") from exc
                boot = locks.boot_id()
                if not boot:
                    raise WorkspaceError("adoption-writer-unconfirmed")
                if owner["boot_id"] == boot and locks.pid_alive(pid):
                    raise WorkspaceError("adoption-writer-active", name)
            lease = locks.Lease(path, "workspace enrollment")
            if not lease.acquire(hold=True):
                raise WorkspaceError("adoption-writer-active", name)
            held.callback(lease.release)
        return _adopt_observed(cfg, key, binding, run=run)


def _adopt_observed(cfg, key, requested, *, run):
    tdir = cfg.ticket_dir(key)
    path = tdir / "state.json"
    if path.is_symlink():
        raise WorkspaceError("adoption-ticket-unconfirmed")
    before = path.read_bytes()
    ticket = state.load(tdir)
    if ticket.key != key or not ticket.worktree or not pathlib.Path(ticket.worktree).is_dir():
        raise WorkspaceError("adoption-worktree-missing")
    existing = read(tdir)
    if existing and (existing.phase != "ready" or existing.backend != requested.backend
                     or existing.workspace_id != requested.workspace_id
                     or any(existing.agent.get(k) != requested.agent.get(k) for k in ("kind", "handle"))):
        raise WorkspaceError("adoption-conflicting-binding")
    candidate = dataclasses.replace(requested, worktree=requested.worktree or ticket.worktree,
                                    branch=requested.branch or ticket.branch, agent=dict(requested.agent))
    if pathlib.Path(candidate.worktree) != pathlib.Path(ticket.worktree):
        raise WorkspaceError("adoption-ticket-path-mismatch")
    adapter = WorkspaceBackend(cfg, "orca", candidate).adapter(run=run, checkpoint=lambda _: None)
    actual_branch = adapter.git("branch", "--show-current", cwd=candidate.worktree)
    if (candidate.branch and candidate.branch != actual_branch) or (ticket.branch and ticket.branch != actual_branch):
        raise WorkspaceError("adoption-ticket-branch-mismatch")
    candidate.branch = actual_branch
    adapter.prepare(candidate)
    target = existing or candidate
    if existing and any(getattr(existing, k) != getattr(candidate, k) for k in ("worktree", "branch", "repo_id")):
        raise WorkspaceError("adoption-conflicting-binding")
    observed = adapter.inspect(target)
    adapter.verify_resources(target)
    identity = adapter.observe_terminal(target)
    if existing and any(existing.agent.get(k) and existing.agent[k] != value for k, value in identity.items()):
        raise WorkspaceError("adoption-terminal-changed")
    if (path.read_bytes() != before or read(tdir) != existing or adapter.inspect(target).identity != observed.identity
            or adapter.observe_terminal(target) != identity):
        raise WorkspaceError("adoption-observation-changed")
    if existing:
        return existing
    candidate.agent.update(identity)
    candidate.phase = "ready"
    candidate.operations = {"adopt": {"status": "done", "result": {"identity": observed.identity}}}
    write(tdir, candidate)
    return candidate


def observe(cfg, ticket: state.Ticket, *, run=None):
    """Read normalized app identity without creating resources or adopting legacy tickets."""
    selection = backend_for(cfg, ticket)
    binding = selection.binding or Binding(backend=selection.name, key=ticket.key, worktree=ticket.worktree,
                                           branch=ticket.branch, workspace_id=ticket.workspace)
    adapter = selection.adapter(run=run)
    return adapter, adapter.inspect(binding)


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
