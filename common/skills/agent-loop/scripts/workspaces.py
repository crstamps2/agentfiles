"""Shared workspace selection and lifecycle; app mechanics live in adapters."""
from __future__ import annotations

import dataclasses
import typing

import state
from workspace_binding import BACKENDS, Binding, BindingError, read


class Presentation(typing.NamedTuple):
    spinner: bool
    text: str
    color: str
    icon: str


@dataclasses.dataclass
class LaunchResult:
    status: str
    binding: Binding


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


def backend_for(cfg, ticket: state.Ticket, *, new_launch: bool = False, override: str | None = None) -> WorkspaceBackend:
    if override is not None and override not in BACKENDS:
        raise BindingError("Unknown workspace backend override")
    binding = read(cfg.ticket_dir(ticket.key))
    if binding:
        if override is not None and override != binding.backend:
            raise BindingError("Override conflicts with recorded workspace backend")
        name = binding.backend
    elif new_launch:
        name = override or cfg.workspace_backend
    else:
        if override is not None:
            raise BindingError("Existing legacy workspace cannot be relocated by override")
        name = "cmux"
    if name not in BACKENDS:
        raise BindingError("Unknown workspace backend")
    return WorkspaceBackend(cfg, name, binding)
