"""Exercise review findings through real adapters and isolated transport fixtures."""
import contextlib
import dataclasses
import json
import subprocess
import unittest
from typing import Any
from unittest.mock import patch

import sidebar
import state
import test_workspace_cmux
import test_workspace_orca
import workspaces


class ReviewRegressionTests(unittest.TestCase):
    def fixture(self, backend) -> Any:
        cls = (test_workspace_cmux.CmuxLifecycleTests if backend == "cmux"
               else test_workspace_orca.OrcaLifecycleTests)
        fixture = cls("test_native_uuid_survives_ref_change_but_replacement_fails_closed"
                      if backend == "cmux" else "test_pane_parity_setup_dev_chat_app_reference")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        result = fixture.launch()
        return fixture, result.binding

    def test_cmux_notification_uses_real_adapter_and_configured_identity(self):
        fixture, binding = self.fixture("cmux")
        tdir = fixture.cfg.ticket_dir("ZIP-1")
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                if legacy:
                    (tdir / "workspace.json").unlink()
                state.save(tdir, state.Ticket(key="ZIP-1", worktree=binding.worktree,
                           branch=binding.branch, workspace="workspace:19" if legacy else binding.workspace_id))
                delivered = []

                def transport(argv, delivered=delivered, **kwargs):
                    if argv[:2] == ["cmux", "notify"]:
                        delivered.append(argv)
                        return subprocess.CompletedProcess(argv, 0, "", "")
                    return fixture.run_command(argv, **kwargs)

                with patch("workspaces.PRODUCTION_ROOT", fixture.cfg.state_root), \
                        patch("config.load", return_value=fixture.cfg):
                    try:
                        success = workspaces.notify(tdir, "ZIP-1", "Needs attention", "Review needed", run=transport)
                    except Exception as exc:
                        self.fail(f"Real cmux notification failed before delivery: {type(exc).__name__}: {exc}")
                self.assertTrue(success)
                self.assertEqual(len(delivered), 1)
                argv = delivered[0]
                self.assertEqual(argv[argv.index("--workspace") + 1], "uuid-original")
                self.assertEqual(argv[argv.index("--title") + 1], "Needs attention")
                self.assertEqual((tdir / "workspace.json").exists(), not legacy)

    def test_cmux_transient_read_can_retry_after_restart(self):
        self.assert_read_retry("cmux")

    def test_orca_transient_read_can_retry_after_restart(self):
        self.assert_read_retry("orca")

    def assert_read_retry(self, name):
        fixture, binding = self.fixture(name)
        fixture.cfg = dataclasses.replace(fixture.cfg, workspace_statuses={
            "in_progress": "in-progress", "ready_for_review": "status-5"})
        tdir = fixture.cfg.ticket_dir("ZIP-1")
        state.save(tdir, state.Ticket(key="ZIP-1", state="human-gate-1",
                   worktree=binding.worktree, branch=binding.branch, evidence_sha="head"))
        cache = fixture.root / "routes.json"
        routes = {}
        reads = 0
        failed = False
        moved = False
        original = fixture.run_command if name == "cmux" else fixture.fake

        def transport(argv, name=name, original=original, fixture=fixture, **kwargs):
            nonlocal reads, failed, moved
            command = argv[1:3]
            read = ["workspace", "list"] if name == "cmux" else ["worktree", "show"]
            if command == read:
                reads += 1
                if reads == 2 and not failed:
                    failed = True
                    raise subprocess.TimeoutExpired(argv, 1)
            if name == "cmux" and command == ["workspace-group", "list"]:
                reply = original(argv, **kwargs)
                data = json.loads(reply.stdout)
                data["groups"].append({"id": "group-ready", "ref": "workspace_group:2",
                    "name": "Ready for Review", "anchor_workspace_ref": "workspace:ready-anchor",
                    "member_workspace_refs": ["workspace:ready-anchor"] + (["workspace:19"] if moved else []),
                    "member_workspace_ids": ["uuid-ready-anchor"] + (["uuid-original"] if moved else [])})
                return subprocess.CompletedProcess(argv, 0, json.dumps(data), "")
            if name == "cmux" and command == ["workspace-group", "add"]:
                moved = True
                fixture.grouped = False
                return subprocess.CompletedProcess(argv, 0, "OK", "")
            return original(argv, **kwargs)

        adapter = workspaces.WorkspaceBackend(fixture.cfg, name, binding).adapter(run=original)
        observed = adapter.inspect(binding)
        adapter.run = transport

        def persist(cache=cache, routes=routes):
            workspaces.write_record(cache, routes)

        with patch("cohort._head", return_value="head"), \
                patch("cohort._review_evidence", return_value=(None, "verified-human-gate-1")):
            with contextlib.suppress(workspaces.WorkspaceError):
                sidebar._route_review_ready(fixture.cfg, "ZIP-1", adapter, observed, routes, persist)
            self.assertTrue(failed)
            restarted_routes = json.loads(cache.read_text()) if cache.exists() else {}
            routes.clear()
            routes.update(restarted_routes)
            self.assertNotIn("ZIP-1", routes, "Read-only failure must not leave a durable uncertain move")
            adapter = workspaces.WorkspaceBackend(fixture.cfg, name, binding).adapter(run=transport)
            sidebar._route_review_ready(fixture.cfg, "ZIP-1", adapter, observed, routes, persist)
        self.assertEqual(adapter.inspect(binding).lane, "ready-for-review")
        self.assertIsInstance(json.loads(cache.read_text())["ZIP-1"], list)

    def test_orca_presentation_preserves_operator_edit_after_its_read(self):
        fixture, binding = self.fixture("orca")
        backend, _ = fixture.backend()
        original = fixture.fake
        edited = False

        def transport(argv, **kwargs):
            nonlocal edited
            reply = original(argv, **kwargs)
            if argv[1:3] == ["worktree", "show"] and not edited:
                fixture.fake.comment = "Operator notes\nNew operator note"
                edited = True
            return reply

        backend.run = transport
        with contextlib.suppress(workspaces.WorkspaceError):
            backend.present(binding, workspaces.Presentation(True, "self-healing", "yellow", "wrench"))
        self.assertTrue(edited)
        self.assertEqual(fixture.fake.comment, "Operator notes\nNew operator note")

    def test_orca_reports_unsupported_comment_write_without_mutating_shared_field(self):
        fixture, binding = self.fixture("orca")
        backend, _ = fixture.backend()
        before = fixture.fake.comment
        start = len(fixture.fake.calls)
        with self.assertRaisesRegex(workspaces.WorkspaceError, "orca-comment-update-unsupported"):
            backend.present(binding, workspaces.Presentation(False, "needs attention", "orange", "question"))
        self.assertEqual(fixture.fake.comment, before)
        self.assertFalse(any("--comment" in argv for argv in fixture.fake.calls[start:]))
