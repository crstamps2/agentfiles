"""Legacy setup recovery uses real supported primitives, not fictitious finish_chain."""
import inspect
import json
import pathlib
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

import config
import state
import workspace_binding as wb
import workspace_cmux
import workspaces
from test_config import MINI


class Chain:
    def __init__(self, root):
        self.MAIN_REPO = root / "repo"
        self.WORKTREE_BASE = root / "wts"
        self.calls = []
        self.exit_code = 0
        self.workspace_ref = "workspace:19"

    def ensure_worktree(self, name, branch, new_branch):
        self.calls.append("create-worktree")
        path = self.WORKTREE_BASE / name
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    def open_setup_tab(self, worktree, name):
        self.calls.append("create-workspace")
        (pathlib.Path(worktree) / ".cmux-setup-status").write_text(f"EXIT:{self.exit_code}\n")
        return self.workspace_ref

    def wait_for_setup(self, worktree):
        return (pathlib.Path(worktree) / ".cmux-setup-status").read_text().strip() == "EXIT:0"

    def cmux_focus_workspace(self, workspace):
        self.calls.append(("focus", workspace))

    def open_dev_server_tab(self, workspace, worktree):
        self.calls.append("dev")
        return "surface:2"

    def wait_for_dev_server(self, worktree):
        return True

    def open_agent_tab(self, workspace, prompt, worktree, agent="pi"):
        self.calls.append(("agent", agent, prompt))
        return "surface:3"

    def cmux_new_surface(self, workspace, surface_type="terminal", url=None):
        self.calls.append(("view", url))
        return "surface:" + str(len(self.calls) + 5)


class CmuxLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        p = self.root / "hopper.toml"
        p.write_text(MINI.format(root=str(self.root / "state")) + f'\n[workspace]\nbackend="cmux"\nrepo="{self.root}/repo"\n')
        self.cfg = config.load(p)
        self.cc = Chain(self.root)
        self.native_id = "uuid-original"
        self.native_cwd = str(self.cc.WORKTREE_BASE / "zip-1")
        self.native_remote = {"enabled": False}
        self.native_overrides = {}
        self.git_overrides = {}
        self.group_exists = True
        self.grouped = False
        self.native_fail = None
        self.native_calls = []
        self.patcher = patch("workspace_cmux.helpers", return_value=self.cc)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        selected_binary = patch("workspace_cmux.binary", return_value="cmux")
        selected_binary.start()
        self.addCleanup(selected_binary.stop)
        self.runner = patch("workspaces.subprocess.run", side_effect=self.run_command)
        self.runner.start()
        self.addCleanup(self.runner.stop)

    def run_command(self, argv, **kwargs):
        self.native_calls.append(argv)
        if self.native_fail and tuple(argv[1:3]) == self.native_fail:
            raise subprocess.TimeoutExpired(argv, 1)
        if argv[:3] == ["cmux", "workspace", "list"]:
            row = {"id": self.native_id, "ref": "workspace:19", "title": "zip-1", "current_directory": self.native_cwd,
                   "remote": self.native_remote, **self.native_overrides}
            out = json.dumps({"workspaces": [row]}) if "--json" in argv else "workspace:19 zip-1\n"
            return subprocess.CompletedProcess(argv, 0, out, "")
        if argv[:3] == ["cmux", "workspace-group", "list"]:
            group = {"id": "group-ip", "ref": "workspace_group:1", "name": "In Progress", "anchor_workspace_ref": "workspace:anchor",
                     "member_workspace_refs": ["workspace:anchor"] + (["workspace:19"] if self.grouped else []),
                     "member_workspace_ids": ["uuid-anchor"] + ([self.native_id] if self.grouped else [])}
            return subprocess.CompletedProcess(argv, 0, json.dumps({"groups": [group] if self.group_exists else []}), "")
        if argv[:3] == ["cmux", "workspace-group", "create"]:
            self.group_exists = True
            return subprocess.CompletedProcess(argv, 0, json.dumps({"group": {"id": "group-ip", "ref": "workspace_group:1"}}), "")
        if argv[:2] == ["cmux", "new-workspace"]:
            path = argv[argv.index("--cwd") + 1]
            name = argv[argv.index("--name") + 1]
            ref = self.cc.open_setup_tab(path, name)
            self.grouped = "--group" in argv and argv[argv.index("--group") + 1] in ("group-ip", "workspace_group:1")
            return subprocess.CompletedProcess(argv, 0, f"OK {ref}\n", "")
        if argv == ["bin/worktree-setup"]:
            return subprocess.CompletedProcess(argv, self.cc.exit_code, "setup output", "")
        if argv[0] == "git":
            field = "branch" if "--show-current" in argv else "root" if "--show-toplevel" in argv else "common"
            value = self.git_overrides.get(field, {"branch": "internal/zip-1", "root": self.native_cwd,
                                                  "common": str(self.cfg.workspace_repo / ".git")}[field])
            return subprocess.CompletedProcess(argv, 0, value + "\n", "")
        raise AssertionError(f"Unexpected live operation: {argv}")

    def launch(self):
        return workspaces.launch(self.cfg, "ZIP-1", "internal/zip-1", "/loop-watch ZIP-1", "https://example.atlassian.net/browse/ZIP-1")

    def test_native_uuid_survives_ref_change_but_replacement_fails_closed(self):
        result = self.launch()
        self.assertEqual(result.binding.workspace_id, "uuid-original")
        self.native_overrides = {"ref": "workspace:41"}
        self.assertEqual(self.launch().status, "ok")
        self.native_id = "uuid-replacement"
        before = list(self.cc.calls)
        with self.assertRaises(workspaces.WorkspaceError):
            self.launch()
        self.assertEqual(self.cc.calls, before)

    def test_new_workspace_is_created_atomically_in_progress(self):
        self.group_exists = False
        result = self.launch()
        backend = workspaces.backend_for(self.cfg, state.Ticket(key="ZIP-1")).adapter()
        self.assertEqual(backend.inspect(result.binding).lane, "in-progress")
        created = [c for c in self.native_calls if c[:2] == ["cmux", "new-workspace"]]
        self.assertEqual(len(created), 1)
        self.assertIn("--group", created[0])
        self.assertIn("--focus", created[0])
        self.assertFalse(any(c[:3] == ["cmux", "workspace-group", "add"] for c in self.native_calls))
        groups = [c for c in self.native_calls if c[:3] == ["cmux", "workspace-group", "create"]]
        self.assertIn("--idempotency-key", groups[0])
        self.assertNotIn("--from", groups[0])

    def test_foreign_create_receipt_never_starts_dev_or_agent(self):
        self.cc.workspace_ref = "workspace:foreign"
        with self.assertRaises(workspaces.WorkspaceError):
            self.launch()
        self.assertNotIn("dev", self.cc.calls)
        self.assertFalse(any(isinstance(c, tuple) and c[0] == "agent" for c in self.cc.calls))

    def test_resume_rejects_changed_path_host_branch_and_repository(self):
        self.launch()
        for field, changed in (("cwd", str(self.root / "foreign")), ("remote", {"enabled": True}),
                               ("branch", "other"), ("root", str(self.root / "foreign")), ("common", str(self.root / "foreign.git"))):
            with self.subTest(field=field):
                self.native_cwd = str(self.cc.WORKTREE_BASE / "zip-1")
                self.native_remote = {"enabled": False}
                self.git_overrides = {}
                if field == "cwd":
                    self.native_cwd = changed
                elif field == "remote":
                    self.native_remote = changed
                else:
                    self.git_overrides[field] = changed
                before = list(self.cc.calls)
                with self.assertRaises(workspaces.WorkspaceError):
                    self.launch()
                self.assertEqual(self.cc.calls, before)

    def test_replacement_after_setup_wait_cannot_start_dev_or_agent(self):
        wait = self.cc.wait_for_setup
        def replace(worktree):
            result = wait(worktree)
            self.native_id = "replacement-during-setup"
            return result
        self.cc.wait_for_setup = replace
        with self.assertRaises(workspaces.WorkspaceError):
            self.launch()
        self.assertNotIn("dev", self.cc.calls)
        self.assertFalse(any(isinstance(c, tuple) and c[0] == "agent" for c in self.cc.calls))

    def test_legacy_name_match_requires_exact_worktree_proof(self):
        wt = self.cc.WORKTREE_BASE / "zip-1"
        wt.mkdir(parents=True)
        self.native_cwd = str(self.root / "foreign")
        ticket = state.Ticket(key="ZIP-1", branch="internal/zip-1", worktree=str(wt))
        with self.assertRaises(workspaces.WorkspaceError):
            workspaces.retry_setup(self.cfg, ticket, "/loop-watch ZIP-1", "https://example.test/ZIP-1")
        self.assertEqual(self.cc.calls, [])
        self.assertIsNone(wb.read(self.cfg.ticket_dir("ZIP-1")))

    def test_uncertain_group_creation_never_creates_or_replays_a_workspace(self):
        self.group_exists = False
        self.native_fail = ("workspace-group", "create")
        with self.assertRaises(workspaces.WorkspaceError):
            self.launch()
        with self.assertRaises(workspaces.WorkspaceError):
            self.launch()
        self.assertNotIn("create-workspace", self.cc.calls)
        groups = [c for c in self.native_calls if c[:3] == ["cmux", "workspace-group", "create"]]
        self.assertEqual(len(groups), 1)

    def test_terminal_starts_refocus_only_before_new_native_mutations(self):
        result = self.launch()
        dev = self.cc.calls.index("dev")
        agent = self.cc.calls.index(("agent", "pi", "/loop-watch ZIP-1"))
        self.assertEqual(self.cc.calls[dev - 1], ("focus", result.binding.workspace_id))
        self.assertEqual(self.cc.calls[agent - 1], ("focus", result.binding.workspace_id))
        before = list(self.cc.calls)
        self.launch()
        self.assertEqual(self.cc.calls, before)

    def test_cmux_override_uses_same_observer_prompt(self):
        result = self.launch()
        self.assertEqual(result.status, "ok")
        self.assertIn(("agent", "pi", "/loop-watch ZIP-1"), self.cc.calls)
        self.assertEqual(result.binding.agent, {"kind": "terminal", "handle": "surface:3"})

    def test_legacy_recorded_ref_is_resolved_before_first_binding_write(self):
        wt = self.cc.WORKTREE_BASE / "zip-1"
        wt.mkdir(parents=True)
        ticket = state.Ticket(key="ZIP-1", branch="internal/zip-1", worktree=str(wt), workspace="workspace:19")
        try:
            result = workspaces.retry_setup(self.cfg, ticket, "/loop-watch ZIP-1", "https://example.test/ZIP-1")
        except workspaces.WorkspaceError as exc:
            self.fail(f"Legacy ref must resolve before becoming immutable: {exc}")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.binding.workspace_id, "uuid-original")
        self.assertEqual(result.binding.repo_id, str(self.cfg.workspace_repo / ".git"))
        self.assertNotIn("create-workspace", self.cc.calls)

    def test_legacy_setup_retry_has_no_finish_chain_or_new_workspace(self):
        wt = self.cc.WORKTREE_BASE / "zip-1"
        wt.mkdir(parents=True)
        ticket = state.Ticket(key="ZIP-1", branch="internal/zip-1", worktree=str(wt))
        result = workspaces.retry_setup(self.cfg, ticket, "/loop-watch ZIP-1", "https://example.atlassian.net/browse/ZIP-1")
        self.assertEqual(result.status, "ok")
        self.assertNotIn("create-worktree", self.cc.calls)
        self.assertNotIn("create-workspace", self.cc.calls)
        self.assertEqual((wt / ".cmux-setup-status").read_text(), "EXIT:0\n")
        binding = wb.read(self.cfg.ticket_dir("ZIP-1"))
        if binding is None:
            self.fail("Expected legacy binding enrollment")
        self.assertEqual(binding.backend, "cmux")

    def test_existing_legacy_launch_does_not_create_another_workspace(self):
        state.save(self.cfg.ticket_dir("ZIP-1"), state.Ticket(key="ZIP-1", worktree=str(self.cc.WORKTREE_BASE / "zip-1")))
        with self.assertRaises(workspaces.WorkspaceError):
            self.launch()
        self.assertEqual(self.cc.calls, [])

    def test_failed_setup_never_starts_agent_or_dev(self):
        self.cc.exit_code = 1
        result = self.launch()
        self.assertEqual(result.status, "setup-failed")
        self.assertEqual(self.cc.calls, ["create-worktree", "create-workspace"])

    def test_installed_cmux_primitives_match_adapter(self):
        self.patcher.stop()
        path = pathlib.Path.home() / ".claude/skills/spinup/scripts/cmux_chain.py"
        if not path.is_file():
            self.skipTest("Installed cmux helper unavailable; contract test needs managed installation")
        cfg = types.SimpleNamespace(cmux_chain_dir=path.parent)
        cc = workspace_cmux.helpers(cfg)
        inspect.signature(cc.open_agent_tab).bind("workspace:1", "/loop-watch ZIP-1", "/workspaces/zip-1", agent="pi")
        inspect.signature(cc.ensure_worktree).bind("zip-1", "internal/zip-1", True)
        inspect.signature(cc.cmux_focus_workspace).bind("workspace:1")
        inspect.signature(cc.cmux_new_surface).bind("workspace:1", surface_type="browser", url="https://example.test")
