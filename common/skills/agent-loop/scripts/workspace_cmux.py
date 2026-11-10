"""cmux workspace mechanics, including legacy recovery and installed-helper compatibility."""
from __future__ import annotations

import dataclasses
import inspect
import json
import pathlib
import re
import shutil
import subprocess
import sys

from workspaces import Adapter, DefiniteFailure, Snapshot, WorkspaceError, load_helper


def binary():
    fallback = pathlib.Path("/Applications/cmux.app/Contents/Resources/bin/cmux")
    return shutil.which("cmux") or (str(fallback) if fallback.is_file() else "cmux")


def helpers(cfg):
    helper = load_helper(cfg, "spinup_helper")
    previous = sys.modules.get("spinup_helper")
    try:
        sys.modules["spinup_helper"] = helper
        cc = load_helper(cfg, "cmux_chain")
    finally:
        if previous is None:
            sys.modules.pop("spinup_helper", None)
        else:
            sys.modules["spinup_helper"] = previous
    for name in ("ensure_worktree", "open_setup_tab", "wait_for_setup", "open_dev_server_tab", "wait_for_dev_server", "open_agent_tab", "cmux_new_surface"):
        if not callable(getattr(cc, name, None)):
            raise WorkspaceError("cmux-helper-incompatible", name)
    if "agent" not in inspect.signature(cc.open_agent_tab).parameters:
        raise WorkspaceError("cmux-helper-incompatible", "Explicit Pi harness unavailable")
    return cc


class Backend(Adapter):
    def command(self, args):
        try:
            result = self.run([binary(), *args], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorkspaceError("cmux-unconfirmed", type(exc).__name__) from exc
        if result.returncode:
            raise WorkspaceError("cmux-failed")
        return result.stdout

    def refs(self):
        refs, duplicates = {}, set()
        for match in re.finditer(r"(workspace:\S+)\s+(.+)$", self.command(["workspace", "list"]), re.MULTILINE):
            name = re.sub(r"\s+\[[^\]]*\]$", "", match[2]).strip().lower()
            if name in refs:
                duplicates.add(name)
            refs[name] = match[1]
        return {k: v for k, v in refs.items() if k not in duplicates}

    def prepare(self, binding):
        self.cc = helpers(self.cfg)
        if pathlib.Path(self.cc.MAIN_REPO).resolve() != self.cfg.workspace_repo.resolve():
            raise WorkspaceError("cmux-repo-mismatch")

    def resolve_legacy(self, binding):
        ref = self.refs().get(binding.key.lower())
        if not ref:
            raise WorkspaceError("workspace-closed-or-ambiguous")
        binding.workspace_id = ref
        result = self.run(["git", "-C", binding.worktree, "branch", "--show-current"], capture_output=True, text=True, timeout=10)
        if result.returncode:
            raise WorkspaceError("cmux-branch-unconfirmed")
        if binding.branch and binding.branch != result.stdout.strip():
            raise WorkspaceError("cmux-branch-mismatch")
        binding.branch = result.stdout.strip()

    def json_command(self, args):
        try:
            value = json.loads(self.command(args))
        except (ValueError, TypeError) as exc:
            raise WorkspaceError("cmux-invalid-response") from exc
        if not isinstance(value, dict):
            raise WorkspaceError("cmux-invalid-response")
        return value

    def groups(self):
        value = self.json_command(["workspace-group", "list", "--json", "--id-format", "both"])
        groups = value.get("groups")
        if not isinstance(groups, list):
            raise WorkspaceError("cmux-groups-unconfirmed")
        self.last_groups = groups
        return groups

    def inspect(self, binding):
        ref = self.refs().get(binding.key.lower())
        if not ref:
            raise WorkspaceError("workspace-closed-or-ambiguous")
        identity, lane = ref, "other"
        try:
            groups = self.groups()
        except (WorkspaceError, ValueError):
            groups = []
        memberships = [g for g in groups if ref in g.get("member_workspace_refs", [])]
        if len(memberships) == 1:
            group = memberships[0]
            members = group["member_workspace_refs"]
            ids = group.get("member_workspace_ids", members)
            if len(ids) != len(members):
                raise WorkspaceError("cmux-identity-unconfirmed")
            identity = ids[members.index(ref)]
            if group.get("name") == "Ready for Review":
                lane = "ready-for-review"
            if (group.get("name") == "In Progress" and ref != group.get("anchor_workspace_ref")
                    and len([g for g in groups if g.get("name") == "In Progress"]) == 1):
                lane = "in-progress"
                if not hasattr(self, "source_ids"):
                    self.source_ids = {}
                self.source_ids.setdefault(identity, group.get("id", group.get("ref")))
        if binding.workspace_id and binding.workspace_id not in (ref, identity):
            raise WorkspaceError("cmux-workspace-changed")
        recorded = binding.operations.get("identity", {}).get("result", {}).get("identity")
        if recorded and identity != recorded:
            raise WorkspaceError("cmux-workspace-changed")
        return Snapshot(dataclasses.replace(binding, workspace_id=ref), identity, lane)

    def create(self, binding):
        path = self.operation(binding, "git-create", [binding.key, binding.branch],
                              lambda: {"worktree": self.cc.ensure_worktree(binding.key.lower(), binding.branch, True)})["worktree"]
        binding.worktree = path
        self.checkpoint(binding)
        value = self.operation(binding, "create", [path, binding.key],
                               lambda: {"workspace": self.cc.open_setup_tab(path, binding.key.lower())})
        if not isinstance(value["workspace"], str) or not value["workspace"]:
            raise WorkspaceError("cmux-workspace-unconfirmed")
        binding.workspace_id = value["workspace"]
        self.checkpoint(binding)

    def setup(self, binding):
        if binding.operations.get("setup", {}).get("status") == "done":
            return
        # The initial workspace owns the first setup process. A legacy recovery has no
        # create record; a proved failed setup may run again in that same checkout.
        initial = "create" in binding.operations and "setup" not in binding.operations
        if initial:
            self.cc.wait_for_setup(binding.worktree)
            try:
                marker = (pathlib.Path(binding.worktree) / ".cmux-setup-status").read_text().strip()
            except OSError as exc:
                raise WorkspaceError("setup-unconfirmed") from exc
            if not re.fullmatch(r"EXIT:[0-9]+", marker):
                raise WorkspaceError("setup-unconfirmed")
            code = int(marker.split(":")[1])
        else:
            attempt = sum(key.startswith("setup-run-") for key in binding.operations)
            def rerun():
                import os
                env = dict(os.environ, WORKTREE_NAME=binding.key.lower(), WORKTREE_ROOT_PATH=str(self.cfg.workspace_repo))
                result = self.run(["bin/worktree-setup"], cwd=binding.worktree, env=env, capture_output=True, text=True, timeout=1800)
                (self.cfg.ticket_dir(binding.key) / "spinup-setup.log").write_text((result.stdout + result.stderr)[-20000:])
                return {"exit_code": result.returncode}
            code = self.operation(binding, f"setup-run-{attempt}", [binding.worktree], rerun)["exit_code"]
            (pathlib.Path(binding.worktree) / ".cmux-setup-status").write_text(f"EXIT:{code}\n")
        binding.operations["setup"] = {"status": "done" if code == 0 else "failed", "exit_code": code}
        self.checkpoint(binding)
        if code:
            raise DefiniteFailure("setup-failed")

    def start_dev(self, binding):
        value = self.operation(binding, "dev", [binding.workspace_id, binding.worktree],
                               lambda: {"handle": self.cc.open_dev_server_tab(binding.workspace_id, binding.worktree)})
        binding.terminals["dev"] = value["handle"]
        self.checkpoint(binding)
        ready = self.cc.wait_for_dev_server(binding.worktree)
        binding.operations["serving"] = {"status": "done", "result": {"ready": ready}}
        self.checkpoint(binding)

    def start_agent(self, binding, prompt):
        value = self.operation(binding, "agent", [binding.workspace_id, prompt, "pi"],
                               lambda: {"handle": self.cc.open_agent_tab(binding.workspace_id, prompt, binding.worktree, agent="pi")})
        binding.agent = {"kind": "terminal", "handle": value["handle"]}
        self.checkpoint(binding)

    def ensure_views(self, binding, ref_url):
        urls = {"reference": ref_url}
        if binding.operations.get("serving", {}).get("result", {}).get("ready"):
            urls["app"] = f"https://admin.{binding.key.lower()}.test"
        for role, url in urls.items():
            value = self.operation(binding, f"view-{role}", [binding.workspace_id, url],
                                   lambda url=url: {"handle": self.cc.cmux_new_surface(binding.workspace_id, surface_type="browser", url=url)})
            binding.views[role] = value["handle"]
            self.checkpoint(binding)

    def present(self, binding, value):
        self.command(["workspace", "loading", "on" if value.spinner else "off", "--id", "agent-loop", "--workspace", binding.workspace_id])
        self.command(["set-status", "agent-loop", value.text, "--icon", value.icon, "--color", value.color,
                      "--priority", "90", "--workspace", binding.workspace_id])

    def move_to_review(self, binding, expected):
        current = self.inspect(binding)
        if current.identity != expected.identity or current.lane != "in-progress" or not self.before_move():
            return False
        groups = self.groups()
        destinations = [g for g in groups if g.get("name") == "Ready for Review"]
        if len(destinations) > 1:
            return False
        if not destinations:
            value = self.json_command(["workspace-group", "create", "--name", "Ready for Review",
                                       "--idempotency-key", "agent-loop-ready-for-review", "--json"])
            if not value.get("group", {}).get("ref"):
                raise WorkspaceError("cmux-group-unconfirmed")
        if not self.before_move():
            return False
        current = self.inspect(binding)
        groups = self.last_groups
        sources = [g for g in groups if g.get("name") == "In Progress"]
        destinations = [g for g in groups if g.get("name") == "Ready for Review"]
        if (current.identity != expected.identity or current.lane != "in-progress" or len(sources) != 1 or len(destinations) != 1
                or sources[0].get("id", sources[0].get("ref")) != self.source_ids.get(expected.identity)
                or not destinations[0].get("ref")):
            return False
        try:
            self.command(["workspace-group", "add", "--group", destinations[0]["ref"], "--workspace", current.binding.workspace_id])
        except WorkspaceError as exc:
            if exc.status == "cmux-failed":
                raise DefiniteFailure("cmux-group-add-rejected") from exc
            raise
        return True

    def notify(self, binding, title, body):
        current = self.inspect(binding)
        self.command(["notify", "--title", title, "--body", body, "--workspace", current.binding.workspace_id])
