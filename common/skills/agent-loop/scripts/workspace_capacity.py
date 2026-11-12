"""Status-based Orca workspace budget; independent of autonomous execution slots."""
from __future__ import annotations

import dataclasses
import pathlib
import subprocess
import time

import state
import workspace_binding
import workspace_orca
from workspaces import WorkspaceError


class CapacityError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class Inventory:
    paths: frozenset[tuple[str, str]]
    observed_at: float
    on_hold_paths: frozenset[tuple[str, str]] = frozenset()

    @property
    def count(self):
        return len(self.paths)


def _normalize(value):
    return "-".join(value.strip().lower().replace("_", " ").replace("-", " ").split())


def read_inventory(cfg):
    backend = workspace_orca.Backend(cfg, run=subprocess.run, checkpoint=lambda _: None)
    return backend.command(["worktree", "list", "--repo", f"path:{cfg.workspace_repo}", "--limit", "1000"], timeout=15)


def observe(cfg):
    if cfg.workspace_limit is None:
        return None
    try:
        raw = read_inventory(cfg)
        rows = raw.get("worktrees")
        scope = raw.get("hostScope", {})
        if (not isinstance(rows, list) or raw.get("truncated") is not False
                or type(raw.get("totalCount")) is not int or raw["totalCount"] != len(rows)
                or not isinstance(scope, dict) or scope.get("hostIds") != ["local"]
                or scope.get("omittedHostIds") != []):
            raise CapacityError("workspace capacity unavailable: incomplete local Orca inventory")
        excluded = {_normalize(cfg.workspace_statuses.get(name, default)) for name, default in (
            ("in_review", "in-review"), ("approved", "approved"), ("done", "completed"), ("on_hold", "on-hold"))}
        excluded.update(("in-review", "approved", "done", "on-hold", "completed"))
        paths = set()
        on_hold_paths = set()
        for row in rows:
            if (not isinstance(row, dict) or type(row.get("isMainWorktree")) is not bool
                    or not isinstance(row.get("path"), str) or not pathlib.Path(row["path"]).is_absolute()
                    or row.get("hostId", "local") != "local"):
                raise CapacityError("workspace capacity unavailable: invalid Orca workspace")
            if row["isMainWorktree"]:
                continue
            status = row.get("workspaceStatus")
            path = ("local", str(pathlib.Path(row["path"]).resolve()))
            if isinstance(status, str) and _normalize(status) in {
                    "on-hold", _normalize(cfg.workspace_statuses.get("on_hold", "on-hold"))}:
                on_hold_paths.add(path)
            if isinstance(status, str) and _normalize(status) in excluded:
                continue
            paths.add(path)
        return Inventory(frozenset(paths), time.monotonic(), frozenset(on_hold_paths))
    except (WorkspaceError, OSError, TypeError, ValueError) as exc:
        if isinstance(exc, CapacityError):
            raise
        raise CapacityError(f"workspace capacity unavailable: {exc}") from exc


def represented(cfg, key, inventory):
    ticket = state.load(cfg.ticket_dir(key))
    binding = workspace_binding.read(cfg.ticket_dir(key))
    paths = [ticket.worktree, binding.worktree if binding else None]
    return any(("local", str(pathlib.Path(path).resolve())) in inventory.paths for path in paths if path)


def on_hold(cfg, key, inventory):
    if inventory is None:
        return False
    if time.monotonic() - inventory.observed_at > 30:
        raise CapacityError("workspace status unavailable: refresh Orca inventory")
    ticket = state.load(cfg.ticket_dir(key))
    binding = workspace_binding.read(cfg.ticket_dir(key))
    paths = [ticket.worktree, binding.worktree if binding else None]
    resolved = {str(pathlib.Path(path).resolve()) for path in paths if path}
    if ticket.key != key or len(resolved) > 1:
        raise CapacityError("workspace identity disagrees; On Hold handback deferred")
    return any(("local", path) in inventory.on_hold_paths for path in resolved)


def check(cfg, inventory, *, key, cohort=None, extra=0):
    """Return the incremental workspace charge, or refuse without mutating anything.

    A reserved slot charges for a workspace not visible in Orca yet. Active slots
    are never added: their workspace status alone determines whether they count.
    Call with the current cohort under its lease, after observing Orca outside it.
    """
    if cfg.workspace_limit is None:
        return 0
    if inventory is None or time.monotonic() - inventory.observed_at > 30:
        raise CapacityError("workspace capacity unavailable: refresh Orca inventory")
    if on_hold(cfg, key, inventory):
        raise CapacityError(f"{key} is On Hold; autonomous admission refused")
    try:
        pending = {row.ticket for row in cohort.slots if row.ticket and row.phase == "reserved"
                   and not represented(cfg, row.ticket, inventory)} if cohort else set()
        charge = int(key not in pending and not represented(cfg, key, inventory))
    except (OSError, ValueError, TypeError) as exc:
        raise CapacityError(f"workspace capacity unavailable: {exc}") from exc
    count = inventory.count + len(pending) + extra
    if count + charge > cfg.workspace_limit:
        raise CapacityError(f"workspace limit reached ({count}/{cfg.workspace_limit}); new admission deferred")
    return charge
