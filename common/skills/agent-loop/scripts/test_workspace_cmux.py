"""Legacy setup recovery uses real supported primitives, not fictitious finish_chain."""
import inspect
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

    def ensure_worktree(self, name, branch, new_branch):
        self.calls.append("create-worktree")
        path = self.WORKTREE_BASE / name
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    def open_setup_tab(self, worktree, name):
        self.calls.append("create-workspace")
        (pathlib.Path(worktree) / ".cmux-setup-status").write_text(f"EXIT:{self.exit_code}\n")
        return "workspace:19"

    def wait_for_setup(self, worktree):
        return (pathlib.Path(worktree) / ".cmux-setup-status").read_text().strip() == "EXIT:0"

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
        if argv[:3] == ["cmux", "workspace", "list"]:
            return subprocess.CompletedProcess(argv, 0, "workspace:19 zip-1\n", "")
        if argv[:3] == ["cmux", "workspace-group", "list"]:
            return subprocess.CompletedProcess(argv, 0, '{"groups": []}', "")
        if argv == ["bin/worktree-setup"]:
            return subprocess.CompletedProcess(argv, self.cc.exit_code, "setup output", "")
        if argv[0] == "git":
            return subprocess.CompletedProcess(argv, 0, "internal/zip-1\n" if "--show-current" in argv else str(self.cfg.workspace_repo / ".git"), "")
        raise AssertionError(f"Unexpected live operation: {argv}")

    def launch(self):
        return workspaces.launch(self.cfg, "ZIP-1", "internal/zip-1", "/loop-watch ZIP-1", "https://example.atlassian.net/browse/ZIP-1")

    def test_cmux_override_uses_same_observer_prompt(self):
        result = self.launch()
        self.assertEqual(result.status, "ok")
        self.assertIn(("agent", "pi", "/loop-watch ZIP-1"), self.cc.calls)
        self.assertEqual(result.binding.agent, {"kind": "terminal", "handle": "surface:3"})

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
        inspect.signature(cc.cmux_new_surface).bind("workspace:1", surface_type="browser", url="https://example.test")
