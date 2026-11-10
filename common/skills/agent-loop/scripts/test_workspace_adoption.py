"""Explicit enrollment observes existing resources; it never launches or transfers ownership."""
import contextlib
import dataclasses
import io
import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import config
import locks
import runner
import state
import workspace_binding as wb
import workspaces
from test_config import MINI
from test_workspace_orca import LocalOrca


class ExistingOrca(LocalOrca):
    def __init__(self, root, cfg):
        super().__init__(root, cfg)
        self.running = True
        self.terminal_overrides = {}
        self.on_read = lambda: None

    def __call__(self, argv, **kwargs):
        if argv[1:3] == ["terminal", "read"]:
            self.calls.append(argv)
            self.assert_local(argv)
            self.on_read()
            return subprocess.CompletedProcess(argv, 0, json.dumps({"ok": True, "result": {"terminal": {
                "handle": "term-agent", "status": "running" if self.running else "exited", "tail": []}}}), "")
        result = super().__call__(argv, **kwargs)
        if argv[1:3] == ["terminal", "list"]:
            value = json.loads(result.stdout)
            for terminal in value["result"]["terminals"]:
                terminal.update(connected=True, writable=True, orphaned=False, ptyId="pty-1", incarnationId="incarnation-1")
                terminal.update(self.terminal_overrides)
            result.stdout = json.dumps(value)
        return result


class AdoptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.path = self.root / "hopper.toml"
        self.path.write_text(MINI.format(root=str(self.root / "state")) + f'\n[workspace]\nrepo="{self.root}/repo"\n')
        self.cfg = config.load(self.path)
        self.cfg.ensure_dirs()
        self.fake = ExistingOrca(self.root, self.cfg)
        self.fake.path.parent.mkdir()
        self.fake.git("-C", str(self.cfg.workspace_repo), "worktree", "add", "-b", "internal/zip-1", str(self.fake.path), "main")
        self.tdir = self.cfg.ticket_dir("ZIP-1")
        state.save(self.tdir, state.Ticket(key="ZIP-1", state="human-gate-1", worktree=str(self.fake.path), branch="internal/zip-1"))
        (self.tdir / "HUMAN").write_text("preserve")
        (self.tdir / "INTERACTIVE.json").write_text('{"session":"original"}')
        (self.cfg.state_root / "cohort.json").write_text('{"preserve":"exactly"}')
        self.real_run = subprocess.run
        self.fake.git = lambda *args: self.real_run(["git", *args], capture_output=True, text=True, check=True)
        boot = patch("locks.boot_id", return_value="fixture-boot")
        boot.start()
        self.addCleanup(boot.stop)
        transport = patch("workspaces.subprocess.run", side_effect=self.fake)
        transport.start()
        self.addCleanup(transport.stop)

    def request(self, **changes):
        return wb.Binding(backend="orca", key="ZIP-1", workspace_id=f"repo::{self.fake.path}",
                          agent={"kind": "terminal", "handle": "term-agent"}, **changes)

    def adopt(self):
        return workspaces.adopt(self.cfg, "ZIP-1", self.request())

    def snapshot(self):
        return {p.relative_to(self.cfg.state_root): p.read_bytes() for p in self.cfg.state_root.rglob("*") if p.is_file() and p.name != "workspace.json" and "locks" not in p.relative_to(self.cfg.state_root).parts}

    def test_enrollment_is_metadata_only_and_preserves_terminal_surface(self):
        before = self.snapshot()
        binding = self.adopt()
        self.assertEqual(binding.agent["kind"], "terminal")
        self.assertEqual(binding.agent["handle"], "term-agent")
        self.assertEqual(binding.agent["incarnation_id"], "incarnation-1")
        self.assertEqual(binding.phase, "ready")
        self.assertEqual(binding.worktree, str(self.fake.path))
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.fake.mutation_bindings, [])
        permitted = {("repo", "show"), ("worktree", "show"), ("terminal", "list"), ("terminal", "read")}
        for call in self.fake.calls:
            if call[0] != "git" and call[1] != "status":
                self.assertIn(tuple(call[1:3]), permitted)

    def test_identical_enrollment_is_idempotent(self):
        self.adopt()
        path = self.tdir / "workspace.json"
        before = path.read_bytes()
        self.adopt()
        self.assertEqual(before, path.read_bytes())

    def test_missing_ticket_never_creates_one(self):
        (self.tdir / "state.json").unlink()
        with self.assertRaises(workspaces.WorkspaceError):
            self.adopt()
        self.assertIsNone(wb.read(self.tdir))
        self.assertEqual(self.fake.calls, [])

    def test_wrong_path_repo_branch_or_host_is_rejected(self):
        for change in ({"hostId": "remote"}, {"repoId": "other"}, {"path": str(self.root)},
                       {"branch": "refs/heads/other"}, {"isMainWorktree": True}):
            with self.subTest(change=change):
                self.fake.row_overrides = change
                with self.assertRaises(workspaces.WorkspaceError):
                    self.adopt()
                self.assertIsNone(wb.read(self.tdir))

    def test_stale_or_duplicate_terminal_handle_is_rejected(self):
        for handles in ([], ["term-agent", "term-agent"]):
            with self.subTest(handles=handles):
                self.fake.terminal_handles = handles
                with self.assertRaises(workspaces.WorkspaceError):
                    self.adopt()
                self.assertIsNone(wb.read(self.tdir))

    def test_wrong_or_disconnected_terminal_is_rejected(self):
        for change in ({"executionHostId": "remote"}, {"worktreeId": "other"}, {"orphaned": True},
                       {"connected": False}, {"writable": False}, {"incarnationId": ""}, {"incarnationId": 123}):
            with self.subTest(change=change):
                self.fake.terminal_overrides = change
                with self.assertRaises(workspaces.WorkspaceError):
                    self.adopt()
                self.assertIsNone(wb.read(self.tdir))

    def test_exited_terminal_is_rejected(self):
        self.fake.running = False
        with self.assertRaises(workspaces.WorkspaceError):
            self.adopt()
        self.assertIsNone(wb.read(self.tdir))

    def test_changed_terminal_incarnation_is_not_reenrolled(self):
        self.adopt()
        before = (self.tdir / "workspace.json").read_bytes()
        self.fake.terminal_overrides["incarnationId"] = "replacement"
        with self.assertRaises(workspaces.WorkspaceError):
            self.adopt()
        self.assertEqual(before, (self.tdir / "workspace.json").read_bytes())

    def test_restart_refuses_a_replaced_adopted_terminal_incarnation(self):
        self.adopt()
        self.fake.terminal_overrides["incarnationId"] = "replacement"
        with self.assertRaises(workspaces.WorkspaceError):
            workspaces.launch(self.cfg, "ZIP-1", "internal/zip-1", "/loop-watch ZIP-1", "reference")
        self.assertEqual(self.fake.mutation_bindings, [])

    def test_active_writer_is_not_bypassed(self):
        for name in ("drip", "runner", "runner-zip-1"):
            with self.subTest(name=name):
                lease = locks.Lease(self.cfg.state_root / "locks" / name, "existing writer")
                self.assertTrue(lease.acquire(hold=True))
                try:
                    with self.assertRaises(workspaces.WorkspaceError):
                        self.adopt()
                finally:
                    lease.release()
        self.assertEqual(self.fake.calls, [])

    def test_unknown_lease_owner_is_not_overwritten(self):
        path = self.cfg.state_root / "locks" / "drip"
        path.parent.mkdir(exist_ok=True)
        for content in ("{", "[1]", '{"pid":123}', '{"pid":123,"boot_id":null}', '{"pid":0,"boot_id":"fixture-boot"}'):
            with self.subTest(content=content):
                path.write_text(content)
                with patch("locks.pid_alive", return_value=False) as probe, self.assertRaises(workspaces.WorkspaceError):
                    self.adopt()
                probe.assert_not_called()
                self.assertEqual(path.read_text(), content)
        self.assertEqual(self.fake.calls, [])

    def test_conflicting_binding_is_not_overwritten(self):
        original = dataclasses.replace(self.request(), agent={"kind": "terminal", "handle": "different"})
        wb.write(self.tdir, original)
        before = (self.tdir / "workspace.json").read_bytes()
        with self.assertRaises(workspaces.WorkspaceError):
            self.adopt()
        self.assertEqual(before, (self.tdir / "workspace.json").read_bytes())
        self.assertEqual(self.fake.calls, [])

    def test_ticket_change_during_observation_prevents_enrollment(self):
        def change():
            ticket = state.load(self.tdir)
            ticket.reason = "changed while observing"
            state.save(self.tdir, ticket)
        self.fake.on_read = change
        with self.assertRaises(workspaces.WorkspaceError):
            self.adopt()
        self.assertIsNone(wb.read(self.tdir))

    def test_old_state_writer_preserves_binding_through_recovery(self):
        self.adopt()
        before = (self.tdir / "workspace.json").read_bytes()
        ticket = state.load(self.tdir)
        ticket.recovery = {"active": {"status": "waiting-repair"}, "queued": []}
        state.save(self.tdir, ticket)
        self.assertEqual(before, (self.tdir / "workspace.json").read_bytes())

    def test_cli_returns_without_runner_reconciliation_or_supervision(self):
        before = self.snapshot()
        with contextlib.redirect_stdout(io.StringIO()), patch("runner.Runner", side_effect=AssertionError("worker execution")), patch("reconcile.reconcile", side_effect=AssertionError("recovery")):
            result = runner.main(["--config", str(self.path), "workspace-adopt", "--ticket", "ZIP-1", "--backend", "orca",
                                  "--workspace", f"repo::{self.fake.path}", "--agent-terminal", "term-agent"])
        self.assertEqual(result, 0)
        self.assertEqual(before, self.snapshot())
        self.assertIsNotNone(wb.read(self.tdir))
