"""Run the shared lifecycle against realistic Orca JSON and real isolated Git trees."""
import dataclasses
import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import config
import state
import workspace_binding as wb
import workspace_orca
import workspaces
from test_config import MINI


class LocalOrca:
    def __init__(self, root, cfg):
        self.cfg = cfg
        self.root = root
        self.path = root / "native" / "zip-1"
        self.calls = []
        self.fail: tuple[str, str] | None = None
        self.real_run = subprocess.run
        self.exit_code = 0
        self.kind = "structured"
        self.corrupt = False
        self.capabilities = list(workspace_orca.REQUIRED_CAPABILITIES)
        self.identity = "wt2:local:instance-1"
        self.row_overrides = {}
        self.terminal_handles = ["term-Setup", "term-Dev", "term-agent"]
        self.prompt_outcome = "journaled"
        self.mutation_bindings = []
        self.chat_present = True
        self.page_handles = ["page-1", "page-2"]
        self.wait_satisfied = True
        self.git("init", "-b", "main", str(cfg.workspace_repo))
        self.git("-C", str(cfg.workspace_repo), "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "--allow-empty", "-m", "fixture")

    @staticmethod
    def git(*args):
        return subprocess.run(["git", *args], capture_output=True, text=True, check=True)

    def row(self):
        return {"id": f"repo::{self.path}", "repoId": "repo", "hostId": "local", "path": str(self.path),
                "branch": "refs/heads/internal/zip-1", "isMainWorktree": False,
                "identity": {"key": self.identity}, "workspaceStatus": "in-progress", **self.row_overrides}

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[0] == "git":
            if "fetch" in argv or "ls-remote" in argv:
                return subprocess.CompletedProcess(argv, 0, "", "")
            return self.real_run(argv, **kwargs)
        if argv[0] == "overmind":
            return subprocess.CompletedProcess(argv, 0, "web 123 running\n", "")
        if argv[0] == "curl":
            return subprocess.CompletedProcess(argv, 0, "200", "")
        if argv[0] != "/opt/homebrew/bin/orca":
            raise AssertionError(f"Unexpected command: {argv}")
        self.assert_local(argv)
        cmd = tuple(argv[1:3])
        if cmd in (("worktree", "create"), ("terminal", "create"), ("agent", "launch"), ("tab", "create"), ("reference", "add")):
            binding = wb.read(self.cfg.ticket_dir("ZIP-1"))
            if binding is None or not any(op.get("status") == "pending" for op in binding.operations.values()):
                raise AssertionError("Durable intent must precede every mutation")
            self.mutation_bindings.append(binding)
        if self.fail == cmd:
            raise subprocess.TimeoutExpired(argv, 1, output='{"operationId":"accepted-launch"}')
        result = {}
        if argv[1] == "status":
            result = {"runtime": {"reachable": True, "capabilities": self.capabilities}, "graph": {"state": "ready"}}
        elif cmd == ("repo", "show"):
            result = {"repo": {"id": "repo", "path": str(self.cfg.workspace_repo), "kind": "git"}}
        elif cmd == ("worktree", "create"):
            if wb.read(self.cfg.ticket_dir("ZIP-1")) is None:
                raise AssertionError("Binding must precede native mutation")
            self.path.parent.mkdir(exist_ok=True)
            self.git("-C", str(self.cfg.workspace_repo), "worktree", "add", "-b", "internal/zip-1", str(self.path), "main")
            result = {"worktree": self.row()}
        elif cmd == ("worktree", "show"):
            result = {"worktree": self.row()}
        elif cmd == ("terminal", "create"):
            title = argv[argv.index("--title") + 1]
            if title == "Setup":
                (self.path / ".env").write_text("PORT=34567\n")
                (self.path / ".overmind.sock").touch()
            result = {"terminal": {"handle": f"term-{title}"}}
        elif cmd == ("terminal", "wait"):
            result = {"wait": {"satisfied": self.wait_satisfied, "exitCode": self.exit_code}}
        elif cmd == ("terminal", "list"):
            result = {"terminals": [{"handle": h, "worktreeId": f"repo::{self.path}", "executionHostId": "local"} for h in self.terminal_handles], "truncated": False}
        elif cmd == ("agent", "launch"):
            result = {"worktreeId": f"repo::{self.path}", "operationId": "launch-1",
                      "outcome": {"kind": self.kind, "handle": "chat-1" if self.kind == "structured" else "term-agent", "sessionId": "session-1", "tabId": "structured-agent-session-pi_test"},
                      "receipt": {"detail": "Requested surface" if self.kind == "structured" else "Chat unavailable; terminal fallback"},
                      "prompt": {"outcome": self.prompt_outcome}}
        elif cmd == ("tab", "create"):
            result = {"browserPageId": "page-" + str(sum(c[1:3] == ["tab", "create"] for c in self.calls))}
        elif cmd == ("tab", "list"):
            result = {"tabs": [{"browserPageId": p, "worktreeId": f"repo::{self.path}"} for p in self.page_handles]}
        elif cmd == ("worktree", "ps"):
            result = {"truncated": False, "worktrees": [{"worktreeId": f"repo::{self.path}", "hostId": "local", "path": str(self.path),
                      "identity": {"key": self.identity}, "agents": [{"paneKey": "structured-agent-session-pi_test:session-1", "agentType": "pi",
                      "structuredHostOwned": True, "state": "waiting"}] if self.chat_present else []}]}
        elif cmd not in (("reference", "add"), ("worktree", "set")):
            raise AssertionError(f"Unexpected Orca operation: {argv}")
        text = "invalid JSON" if self.corrupt else json.dumps({"ok": True, "result": result})
        return subprocess.CompletedProcess(argv, 0, text, "")

    @staticmethod
    def assert_local(argv):
        if argv[-3:] != ["--environment", "local", "--json"]:
            raise AssertionError("Orca must explicitly target local runtime")

    def count(self, *cmd):
        return sum(c[1:1+len(cmd)] == list(cmd) for c in self.calls)


class OrcaLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        p = self.root / "hopper.toml"
        p.write_text(MINI.format(root=str(self.root / "state")) + f'\n[workspace]\nrepo="{self.root}/repo"\n')
        self.cfg = config.load(p)
        self.fake = LocalOrca(self.root, self.cfg)
        self.patches = [patch("workspaces.subprocess.run", side_effect=self.fake), patch("workspace_orca.port_in_use", return_value=False)]
        # The transport double delegates real local git through a saved runner, not this patched module.
        self.real_run = subprocess.run
        self.fake.git = lambda *args: self.real_run(["git", *args], capture_output=True, text=True, check=True)
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def launch(self):
        return workspaces.launch(self.cfg, "ZIP-1", "internal/zip-1", "/loop-watch ZIP-1", "https://example.atlassian.net/browse/ZIP-1")

    def test_pane_parity_setup_dev_chat_app_reference(self):
        result = self.launch()
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.binding.worktree, str(self.fake.path))
        self.assertEqual(result.binding.agent, {"kind": "chat", "handle": "chat-1", "session_id": "session-1", "tab_id": "structured-agent-session-pi_test"})
        self.assertEqual(set(result.binding.terminals), {"setup", "dev"})
        self.assertEqual(set(result.binding.views), {"app", "reference"})
        agent = next(c for c in self.fake.calls if c[1:3] == ["agent", "launch"])
        self.assertEqual(agent[agent.index("--surface") + 1], "chat")
        self.assertEqual(agent[agent.index("--prompt") + 1], "/loop-watch ZIP-1")
        self.assertNotIn("--agent", next(c for c in self.fake.calls if c[1:3] == ["worktree", "create"]))

    def test_chat_session_identity_survives_restart(self):
        self.launch()
        result = self.launch()
        self.assertEqual(result.binding.agent["session_id"], "session-1")
        self.assertEqual(self.fake.count("agent", "launch"), 1)
        self.assertEqual(self.fake.count("terminal", "create"), 2)
        self.assertEqual(self.fake.count("tab", "create"), 2)

    def test_setup_failure_retries_existing_workspace(self):
        self.fake.exit_code = 1
        result = self.launch()
        self.assertEqual(result.status, "setup-failed")
        self.assertEqual(self.fake.count("agent", "launch"), 0)
        self.fake.exit_code = 0
        t = state.Ticket(key="ZIP-1", worktree=str(self.fake.path), branch="internal/zip-1")
        result = workspaces.retry_setup(self.cfg, t, "/loop-watch ZIP-1", "https://example.atlassian.net/browse/ZIP-1")
        self.assertEqual(result.status, "ok")
        self.assertEqual(self.fake.count("worktree", "create"), 1)

    def test_agent_reply_lost_never_relaunches_or_resends(self):
        self.fake.fail = ("agent", "launch")
        with self.assertRaises(workspaces.WorkspaceError):
            self.launch()
        self.fake.fail = None
        with self.assertRaises(workspaces.WorkspaceError):
            self.launch()
        self.assertEqual(self.fake.count("agent", "launch"), 1)
        binding = wb.read(self.cfg.ticket_dir("ZIP-1"))
        if binding is None:
            self.fail("Expected uncertain binding")
        self.assertEqual(binding.phase, "uncertain")
        self.assertEqual(binding.operations["agent"].get("operation_id"), "accepted-launch")

    def test_create_reply_lost_retains_pending_intent(self):
        self.fake.fail = ("worktree", "create")
        with self.assertRaises(workspaces.WorkspaceError):
            self.launch()
        with self.assertRaises(workspaces.WorkspaceError):
            self.launch()
        self.assertEqual(self.fake.count("worktree", "create"), 1)

    def test_host_terminal_fallback_is_recorded_reported_and_never_duplicated(self):
        self.fake.kind = "terminal"
        result = self.launch()
        self.assertEqual(result.binding.agent["kind"], "terminal")
        self.assertIn("fallback", result.binding.operations["agent"]["result"]["detail"])
        self.assertIn("terminal fallback", "; ".join(result.warnings))
        self.launch()
        self.assertEqual(self.fake.count("agent", "launch"), 1)

    def test_chat_refusal_does_not_start_terminal(self):
        self.fake.capabilities = []
        with self.assertRaises(workspaces.WorkspaceError):
            self.launch()
        self.assertEqual(self.fake.count("worktree", "create"), 0)
        self.assertEqual(self.fake.count("agent", "launch"), 0)

    def test_restart_uses_recorded_backend_after_default_changes(self):
        self.launch()
        self.cfg = dataclasses.replace(self.cfg, workspace_backend="cmux")
        self.assertEqual(self.launch().binding.backend, "orca")

    def test_binding_precedes_first_mutation(self):
        self.launch()
        self.assertEqual(len(self.fake.mutation_bindings), 7)

    def test_orca_launch_uses_native_path_and_exact_branch(self):
        result = self.launch()
        self.assertEqual(result.binding.worktree, str(self.fake.path))
        self.assertEqual(result.binding.branch, "internal/zip-1")
        command = next(c for c in self.fake.calls if c[1:3] == ["worktree", "create"])
        self.assertEqual(command[command.index("--setup") + 1], "skip")
        self.assertEqual(command[command.index("--branch") + 1], "internal/zip-1")
        self.assertIn("--no-parent", command)

    def test_orca_launch_does_not_inject_foreground_policy(self):
        self.launch()
        command = next(c for c in self.fake.calls if c[1:3] == ["agent", "launch"])
        self.assertEqual(command[command.index("--prompt") + 1], "/loop-watch ZIP-1")

    def test_chat_handle_is_never_sent_to_terminal_api(self):
        self.launch()
        self.launch()
        self.assertFalse(any("chat-1" in c for c in self.fake.calls if c[1] == "terminal"))

    def test_no_cross_backend_fallback(self):
        self.fake.fail = ("worktree", "create")
        with self.assertRaises(workspaces.WorkspaceError):
            self.launch()
        self.assertFalse(any(c[0] == "cmux" for c in self.fake.calls))

    def test_crash_after_setup_does_not_duplicate_dev_or_agent(self):
        result = self.launch()
        result.binding.phase = "launching"
        result.binding.terminals.pop("dev")
        result.binding.agent = {}
        result.binding.views = {}
        wb.write(self.cfg.ticket_dir("ZIP-1"), result.binding)
        with patch("workspace_orca.port_in_use", return_value=True):
            result = self.launch()
        self.assertEqual(result.status, "ok")
        self.assertEqual(self.fake.count("agent", "launch"), 1)
        self.assertEqual(self.fake.count("terminal", "create"), 2)
        self.assertEqual(self.fake.count("tab", "create"), 2)

    def test_wrong_native_identity_is_rejected_before_another_mutation(self):
        self.launch()
        self.fake.identity = "wt2:local:replacement"
        count = len(self.fake.mutation_bindings)
        with self.assertRaises(workspaces.WorkspaceError):
            self.launch()
        self.assertEqual(len(self.fake.mutation_bindings), count)

    def test_wrong_host_branch_repo_and_path_are_rejected(self):
        self.launch()
        count = len(self.fake.mutation_bindings)
        for field, value in (("hostId", "remote"), ("repoId", "other"), ("path", str(self.cfg.workspace_repo)), ("branch", "refs/heads/other")):
            with self.subTest(field=field):
                self.fake.row_overrides = {field: value}
                with self.assertRaises(workspaces.WorkspaceError):
                    self.launch()
                self.assertEqual(len(self.fake.mutation_bindings), count)

    def test_stale_terminal_handle_is_not_replaced(self):
        self.launch()
        self.fake.terminal_handles.remove("term-Dev")
        with self.assertRaises(workspaces.WorkspaceError):
            self.launch()
        self.assertEqual(self.fake.count("terminal", "create"), 2)

    def test_stale_chat_or_browser_view_cannot_start_another_surface(self):
        self.launch()
        count = len(self.fake.mutation_bindings)
        self.fake.chat_present = False
        with self.assertRaises(workspaces.WorkspaceError):
            self.launch()
        self.fake.chat_present = True
        self.fake.page_handles = ["page-1"]
        with self.assertRaises(workspaces.WorkspaceError):
            self.launch()
        self.assertEqual(len(self.fake.mutation_bindings), count)

    def test_ambiguous_setup_wait_does_not_start_or_rerun(self):
        self.fake.wait_satisfied = False
        with self.assertRaises(workspaces.WorkspaceError):
            self.launch()
        self.fake.wait_satisfied = True
        with self.assertRaises(workspaces.WorkspaceError):
            self.launch()
        self.assertEqual(self.fake.count("terminal", "create"), 1)
        self.assertEqual(self.fake.count("agent", "launch"), 0)

    def test_missing_prompt_proof_never_relaunches(self):
        self.fake.prompt_outcome = "unconfirmed"
        with self.assertRaises(workspaces.WorkspaceError):
            self.launch()
        self.fake.prompt_outcome = "journaled"
        with self.assertRaises(workspaces.WorkspaceError):
            self.launch()
        self.assertEqual(self.fake.count("agent", "launch"), 1)

    def test_app_view_waits_for_readiness_but_reference_remains(self):
        with patch("workspace_orca.Backend.server_ready", return_value=False):
            result = self.launch()
        self.assertEqual(result.status, "serving-timeout")
        self.assertEqual(set(result.binding.views), {"reference"})
        self.assertEqual(self.fake.count("agent", "launch"), 1)

    def test_port_conflict_does_not_kill_or_start_services(self):
        with patch("workspace_orca.port_in_use", return_value=True):
            result = self.launch()
        self.assertEqual(result.status, "port-conflict")
        self.assertEqual(self.fake.count("terminal", "create"), 1)
        self.assertEqual(self.fake.count("agent", "launch"), 0)
        self.assertFalse(any("kill" in " ".join(c) for c in self.fake.calls))

    def test_corrupt_response_fails_before_creating_resources(self):
        self.fake.corrupt = True
        with self.assertRaises(workspaces.WorkspaceError):
            self.launch()
        self.assertEqual(self.fake.count("worktree", "create"), 0)
