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
        self.terminal_overrides = {}
        self.terminal_statuses = {}
        self.prompt_outcome = "journaled"
        self.prompt_overrides = {}
        self.receipt_overrides = {}
        self.mutation_bindings = []
        self.chat_present = True
        self.page_handles = ["page-1", "page-2"]
        self.wait_satisfied = True
        self.workspace_status = "in-progress"
        self.comment = "Operator notes"
        self.unread = False
        self.git("init", "-b", "main", str(cfg.workspace_repo))
        self.git("-C", str(cfg.workspace_repo), "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "--allow-empty", "-m", "fixture")

    @staticmethod
    def git(*args):
        return subprocess.run(["git", *args], capture_output=True, text=True, check=True)

    def row(self):
        return {"id": f"repo::{self.path}", "repoId": "repo", "hostId": "local", "path": str(self.path),
                "branch": "refs/heads/internal/zip-1", "isMainWorktree": False,
                "identity": {"key": self.identity}, "workspaceStatus": self.workspace_status, "comment": self.comment,
                "isUnread": self.unread, "linkedIssue": {"number": 99}, "linkedPR": {"number": 123}, **self.row_overrides}

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
            number = sum(c[1:3] == ["terminal", "create"] and c[c.index("--title") + 1] == title for c in self.calls if "--title" in c)
            handle = f"term-{title}" if number == 1 else f"term-{title}-{number}"
            if handle not in self.terminal_handles:
                self.terminal_handles.append(handle)
            result = {"terminal": {"handle": handle}}
        elif cmd == ("terminal", "wait"):
            result = {"wait": {"satisfied": self.wait_satisfied, "exitCode": self.exit_code}}
        elif cmd == ("terminal", "list"):
            result = {"terminals": [{"handle": h, "worktreeId": f"repo::{self.path}", "executionHostId": "local",
                                     "orphaned": False, "connected": True, "writable": True,
                                     "ptyId": f"pty-{h}", "incarnationId": f"incarnation-{h}", **self.terminal_overrides.get(h, {})}
                                    for h in self.terminal_handles], "truncated": False}
        elif cmd == ("terminal", "read"):
            handle = argv[argv.index("--terminal") + 1]
            result = {"terminal": {"handle": handle, "status": self.terminal_statuses.get(handle, "running")}}
        elif cmd == ("agent", "launch"):
            result = {"worktreeId": f"repo::{self.path}", "operationId": "launch-1",
                      "outcome": {"kind": self.kind, "handle": "chat-1" if self.kind == "structured" else "term-agent", "sessionId": "session-1", "tabId": "structured-agent-session-pi_test"},
                      "receipt": {"mode": self.kind, "preferred": "structured", "reason": "explicit_surface", "detail": "Requested surface" if self.kind == "structured" else "Chat unavailable; terminal fallback", **self.receipt_overrides},
                      "prompt": {"delivery": "submit", "outcome": self.prompt_outcome, "messageId": "message-1", **self.prompt_overrides}}
        elif cmd == ("tab", "create"):
            result = {"browserPageId": "page-" + str(sum(c[1:3] == ["tab", "create"] for c in self.calls))}
        elif cmd == ("tab", "list"):
            result = {"tabs": [{"browserPageId": p, "worktreeId": f"repo::{self.path}"} for p in self.page_handles]}
        elif cmd == ("worktree", "ps"):
            result = {"truncated": False, "worktrees": [{"worktreeId": f"repo::{self.path}", "hostId": "local", "path": str(self.path),
                      "identity": {"key": self.identity}, "agents": [{"paneKey": "structured-agent-session-pi_test:session-1", "agentType": "pi",
                      "structuredHostOwned": True, "state": "waiting"}] if self.chat_present else []}]}
        elif cmd == ("worktree", "set"):
            if "--workspace-status" in argv:
                self.workspace_status = argv[argv.index("--workspace-status") + 1]
            if "--comment" in argv:
                self.comment = argv[argv.index("--comment") + 1]
            if "--unread" in argv:
                self.unread = True
            result = {"worktree": self.row()}
        elif cmd != ("reference", "add"):
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

    def test_interrupted_setup_retry_waits_on_same_recorded_terminal(self):
        self.fake.exit_code = 1
        self.assertEqual(self.launch().status, "setup-failed")
        self.fake.exit_code = 0
        self.fake.fail = ("terminal", "wait")
        ticket = state.Ticket(key="ZIP-1", worktree=str(self.fake.path), branch="internal/zip-1")
        with self.assertRaises(workspaces.WorkspaceError):
            workspaces.retry_setup(self.cfg, ticket, "/loop-watch ZIP-1", "https://example.test/ZIP-1")
        binding = wb.read(self.cfg.ticket_dir("ZIP-1"))
        if binding is None:
            self.fail("Expected durable retry identity")
        handle = binding.terminals["setup"]
        self.fake.fail = None
        result = workspaces.retry_setup(self.cfg, ticket, "/loop-watch ZIP-1", "https://example.test/ZIP-1")
        self.assertEqual(result.status, "ok")
        setups = [c for c in self.fake.calls if c[1:3] == ["terminal", "create"] and c[c.index("--title") + 1] == "Setup"]
        self.assertEqual(len(setups), 2)
        waits = [c for c in self.fake.calls if c[1:3] == ["terminal", "wait"]]
        self.assertEqual(waits[-1][waits[-1].index("--terminal") + 1], handle)
        self.assertEqual(self.fake.count("agent", "launch"), 1)

    def test_only_a_conclusive_setup_failure_advances_the_attempt(self):
        self.fake.exit_code = 1
        self.assertEqual(self.launch().status, "setup-failed")
        ticket = state.Ticket(key="ZIP-1", worktree=str(self.fake.path), branch="internal/zip-1")
        self.assertEqual(workspaces.retry_setup(self.cfg, ticket, "/loop-watch ZIP-1", "https://example.test/ZIP-1").status, "setup-failed")
        self.fake.exit_code = 0
        self.assertEqual(workspaces.retry_setup(self.cfg, ticket, "/loop-watch ZIP-1", "https://example.test/ZIP-1").status, "ok")
        setups = [c for c in self.fake.calls if c[1:3] == ["terminal", "create"] and c[c.index("--title") + 1] == "Setup"]
        self.assertEqual(len(setups), 3)

    def test_fallback_records_actual_native_terminal_identity(self):
        self.fake.kind = "terminal"
        result = self.launch()
        self.assertEqual(result.binding.agent.get("pty_id"), "pty-term-agent")
        self.assertEqual(result.binding.agent.get("incarnation_id"), "incarnation-term-agent")
        self.assertEqual(self.fake.count("terminal", "read"), 1)

    def test_fallback_restart_rejects_replaced_or_disconnected_terminal(self):
        self.fake.kind = "terminal"
        self.launch()
        for changed in ({"ptyId": "replaced"}, {"incarnationId": "replaced"}, {"connected": False},
                        {"writable": False}, {"orphaned": True}, {"ptyId": None}):
            with self.subTest(changed=changed):
                self.fake.terminal_overrides = {"term-agent": changed}
                with self.assertRaises(workspaces.WorkspaceError):
                    self.launch()
                self.assertEqual(self.fake.count("agent", "launch"), 1)

    def test_fallback_restart_rejects_exited_observer(self):
        self.fake.kind = "terminal"
        self.launch()
        self.fake.terminal_statuses["term-agent"] = "exited"
        with self.assertRaises(workspaces.WorkspaceError):
            self.launch()
        self.assertEqual(self.fake.count("agent", "launch"), 1)

    def test_unverifiable_fallback_retains_receipt_without_relaunch(self):
        self.fake.kind = "terminal"
        self.fake.terminal_statuses["term-agent"] = "exited"
        with self.assertRaises(workspaces.WorkspaceError):
            self.launch()
        binding = wb.read(self.cfg.ticket_dir("ZIP-1"))
        if binding is None:
            self.fail("Expected actual launch receipt despite failed liveness")
        self.assertEqual(binding.phase, "uncertain")
        self.assertEqual(binding.agent["handle"], "term-agent")
        self.assertEqual(binding.operations["agent"]["status"], "pending")
        self.assertEqual(binding.operations["agent"]["launch_receipt"]["operation_id"], "launch-1")
        with self.assertRaises(workspaces.WorkspaceError):
            self.launch()
        self.assertEqual(self.fake.count("agent", "launch"), 1)

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

    def test_draft_prompt_is_not_counted_as_submitted(self):
        self.fake.prompt_overrides = {"delivery": "draft"}
        with self.assertRaises(workspaces.WorkspaceError):
            self.launch()
        self.assertEqual(self.fake.count("agent", "launch"), 1)

    def test_structured_prompt_requires_a_native_message_id(self):
        self.fake.prompt_overrides = {"messageId": ""}
        with self.assertRaises(workspaces.WorkspaceError):
            self.launch()
        self.assertEqual(self.fake.count("agent", "launch"), 1)

    def test_native_surface_receipt_must_match_actual_outcome(self):
        self.fake.receipt_overrides = {"mode": "terminal"}
        with self.assertRaises(workspaces.WorkspaceError):
            self.launch()
        self.assertEqual(self.fake.count("agent", "launch"), 1)

    def test_submitted_prompt_receipt_is_retained(self):
        result = self.launch()
        self.assertEqual(result.binding.operations["agent"]["result"].get("prompt", {}).get("message_id"), "message-1")

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

    def backend(self):
        binding = wb.read(self.cfg.ticket_dir("ZIP-1"))
        if binding is None:
            self.fail("Expected launched workspace")
        return workspaces.backend_for(self.cfg, state.Ticket(key="ZIP-1")).adapter(), binding

    def test_ready_for_review_uses_status_5_not_in_review(self):
        self.launch()
        self.cfg = dataclasses.replace(self.cfg, workspace_statuses={"in_progress": "in-progress", "ready_for_review": "status-5"})
        backend, binding = self.backend()
        self.assertTrue(backend.move_to_review(binding, backend.inspect(binding)))
        self.assertEqual(self.fake.workspace_status, "status-5")
        self.assertFalse(any("in-review" in c for c in self.fake.calls if c[1:3] == ["worktree", "set"]))

    def test_missing_mapping_preserves_board_status(self):
        self.launch()
        backend, binding = self.backend()
        self.assertFalse(backend.move_to_review(binding, backend.inspect(binding)))
        self.assertEqual(self.fake.workspace_status, "in-progress")
        backend.present(binding, workspaces.Presentation(True, "self-healing", "yellow", "wrench"))
        self.assertIn("self-healing", self.fake.comment)

    def test_comment_update_does_not_change_links_or_read_state(self):
        self.launch()
        backend, binding = self.backend()
        backend.present(binding, workspaces.Presentation(True, "self-healing", "yellow", "wrench"))
        backend.present(binding, workspaces.Presentation(False, "needs Cody", "orange", "question"))
        self.assertTrue(self.fake.comment.startswith("Operator notes\n"))
        self.assertEqual(self.fake.comment.count("agent-loop:"), 1)
        self.assertIn("needs Cody", self.fake.comment)
        for command in (c for c in self.fake.calls if c[1:3] == ["worktree", "set"]):
            self.assertFalse(set(command) & {"--issue", "--pr", "--unread", "--read", "--workspace-status"})
        self.assertFalse(self.fake.unread)

    def test_notification_uses_bound_workspace_only(self):
        self.launch()
        backend, binding = self.backend()
        backend.notify(binding, "Needs Cody", "Choose public API")
        command = self.fake.calls[-1]
        self.assertEqual(command[1:3], ["worktree", "set"])
        self.assertIn("--unread", command)
        self.assertEqual(command[command.index("--worktree") + 1], "id:" + binding.workspace_id)
        self.assertTrue(self.fake.unread)
        self.assertFalse(any(c[1] == "notify" for c in self.fake.calls))

    def test_human_move_before_native_write_is_preserved(self):
        self.launch()
        self.cfg = dataclasses.replace(self.cfg, workspace_statuses={"in_progress": "in-progress", "ready_for_review": "status-5"})
        backend, binding = self.backend()
        expected = backend.inspect(binding)
        self.fake.workspace_status = "in-review"
        self.assertFalse(backend.move_to_review(binding, expected))
        self.assertEqual(self.fake.workspace_status, "in-review")

    def test_duplicate_display_names_never_select_by_name(self):
        self.launch()
        backend, binding = self.backend()
        self.fake.row_overrides["name"] = "duplicate-title"
        start = len(self.fake.calls)
        backend.present(binding, workspaces.Presentation(False, "needs Cody", "orange", "question"))
        for command in self.fake.calls[start:]:
            if command[1:3] in (["worktree", "show"], ["worktree", "set"]):
                self.assertEqual(command[command.index("--worktree") + 1], "id:" + binding.workspace_id)
        self.assertFalse(any(c[1:3] == ["worktree", "list"] for c in self.fake.calls[start:]))

    def test_nonzero_status_command_never_changes_durable_ticket_state(self):
        self.launch()
        before = {p: p.read_bytes() for p in self.cfg.state_root.rglob("*") if p.is_file()}
        with patch("workspaces.subprocess.run", return_value=subprocess.CompletedProcess([], 1)):
            backend, binding = self.backend()
            with self.assertRaises(workspaces.WorkspaceError):
                backend.present(binding, workspaces.Presentation(True, "self-healing", "yellow", "wrench"))
        after = {p: p.read_bytes() for p in self.cfg.state_root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_status_remains_visible_when_agent_surface_disappears(self):
        self.launch()
        self.fake.chat_present = False
        backend, binding = self.backend()
        snapshot = backend.inspect(binding)
        self.assertIsNone(snapshot.agent_live)
        backend.present(binding, workspaces.Presentation(True, "self-healing", "yellow", "wrench"))
        self.assertIn("self-healing", self.fake.comment)
        with self.assertRaises(workspaces.WorkspaceError):
            self.launch()

    def test_missing_runtime_never_falls_back_or_starts_a_service(self):
        with patch("workspaces.subprocess.run", side_effect=FileNotFoundError("orca")) as command, self.assertRaises(workspaces.WorkspaceError):
            self.launch()
        self.assertEqual(command.call_count, 1)
        self.assertEqual(command.call_args.args[0][1], "status")
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.fake.mutation_bindings, [])

    def test_missing_chat_capabilities_never_launches_a_terminal_instead(self):
        self.fake.capabilities = []
        with self.assertRaises(workspaces.WorkspaceError):
            self.launch()
        self.assertEqual(self.fake.count("agent", "launch"), 0)
        self.assertEqual(self.fake.count("worktree", "create"), 0)
        self.assertEqual(self.fake.mutation_bindings, [])

    def test_corrupt_response_fails_before_creating_resources(self):
        self.fake.corrupt = True
        with self.assertRaises(workspaces.WorkspaceError):
            self.launch()
        self.assertEqual(self.fake.count("worktree", "create"), 0)
