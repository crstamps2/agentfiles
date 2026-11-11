"""Exercise review findings through real adapters and isolated transport fixtures."""
import contextlib
import dataclasses
import io
import json
import pathlib
import subprocess
import types
import unittest
from typing import Any
from unittest.mock import patch

import drip
import runner
import sidebar
import state
import test_workspace_cmux
import test_workspace_orca
import workspace_binding as wb
import workspaces


class ReviewRegressionTests(unittest.TestCase):
    def fixture(self, backend, *, launch=True) -> Any:
        cls = (test_workspace_cmux.CmuxLifecycleTests if backend == "cmux"
               else test_workspace_orca.OrcaLifecycleTests)
        fixture = cls("test_native_uuid_survives_ref_change_but_replacement_fails_closed"
                      if backend == "cmux" else "test_pane_parity_setup_dev_chat_app_reference")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        result = fixture.launch() if launch else None
        return fixture, result.binding if result else None

    def test_orca_git_preflight_failure_can_retry_without_poisoned_intent(self):
        fixture, _ = self.fixture("orca", launch=False)
        failed = False

        def transport(argv, **kwargs):
            nonlocal failed
            if argv[0] == "git" and "ls-remote" in argv and not failed:
                failed = True
                return subprocess.CompletedProcess(argv, 128, "", "remote temporarily unavailable")
            return fixture.fake(argv, **kwargs)

        with patch("workspaces.subprocess.run", side_effect=transport):
            with contextlib.suppress(workspaces.WorkspaceError):
                fixture.launch()
            persisted = wb.read(fixture.cfg.ticket_dir("ZIP-1"))
            assert persisted is not None
            self.assertNotEqual(persisted.phase, "uncertain", "A failed read did not attempt native creation")
            self.assertNotIn("create", persisted.operations)
            self.assertEqual(fixture.fake.count("worktree", "create"), 0)
            result = fixture.launch()
        self.assertEqual(result.status, "ok")
        self.assertEqual(fixture.fake.count("worktree", "create"), 1)
        self.assertEqual(fixture.fake.count("agent", "launch"), 1)

    def test_orca_collision_is_definite_and_does_not_create_unknown_resources(self):
        fixture, _ = self.fixture("orca", launch=False)
        fixture.fake.git("-C", str(fixture.cfg.workspace_repo), "branch", "internal/zip-1")
        with contextlib.suppress(workspaces.WorkspaceError):
            fixture.launch()
        persisted = wb.read(fixture.cfg.ticket_dir("ZIP-1"))
        assert persisted is not None
        self.assertEqual(persisted.phase, "setup-failed")
        self.assertNotIn("create", persisted.operations)
        self.assertEqual(fixture.fake.count("worktree", "create"), 0)
        fixture.fake.git("-C", str(fixture.cfg.workspace_repo), "branch", "-D", "internal/zip-1")
        self.assertEqual(fixture.launch().status, "ok")
        self.assertEqual(fixture.fake.count("worktree", "create"), 1)

    def test_cmux_group_read_failure_can_retry_without_recreating_worktree(self):
        fixture, _ = self.fixture("cmux", launch=False)
        fixture.native_fail = ("workspace-group", "list")
        with contextlib.suppress(workspaces.WorkspaceError):
            fixture.launch()
        persisted = wb.read(fixture.cfg.ticket_dir("ZIP-1"))
        assert persisted is not None
        self.assertNotEqual(persisted.phase, "uncertain", "Group inventory is read-only")
        self.assertNotIn("initial-group", persisted.operations)
        fixture.native_fail = None
        self.assertEqual(fixture.launch().status, "ok")
        self.assertEqual(fixture.cc.calls.count("create-worktree"), 1)
        self.assertEqual(fixture.cc.calls.count("create-workspace"), 1)

    def test_cmux_dev_identity_read_failure_does_not_poison_mutation(self):
        self.assert_cmux_required_read_retry("dev", "setup")

    def test_cmux_agent_identity_read_failure_does_not_poison_mutation(self):
        self.assert_cmux_required_read_retry("agent", "dev")

    def assert_cmux_required_read_retry(self, stage, previous_stage):
        fixture, _ = self.fixture("cmux", launch=False)
        failed = False

        def transport(argv, **kwargs):
            nonlocal failed
            binding = wb.read(fixture.cfg.ticket_dir("ZIP-1"))
            if (argv[1:3] == ["workspace", "list"] and binding is not None and not failed
                    and binding.operations.get(previous_stage, {}).get("status") == "done"
                    and binding.operations.get(stage, {}).get("status") != "done"):
                failed = True
                raise subprocess.TimeoutExpired(argv, 5)
            return fixture.run_command(argv, **kwargs)

        with patch("workspaces.subprocess.run", side_effect=transport):
            with contextlib.suppress(workspaces.WorkspaceError):
                fixture.launch()
            self.assertTrue(failed)
            binding = wb.read(fixture.cfg.ticket_dir("ZIP-1"))
            assert binding is not None
            self.assertNotEqual(binding.phase, "uncertain")
            self.assertNotIn(stage, binding.operations)
            self.assertEqual(fixture.launch().status, "ok")
        self.assertEqual(fixture.cc.calls.count("dev"), 1)
        self.assertEqual(len([call for call in fixture.cc.calls if isinstance(call, tuple) and call[0] == "agent"]), 1)

    def test_orca_refused_reference_does_not_block_completed_agent(self):
        fixture, _ = self.fixture("orca", launch=False)

        def transport(argv, **kwargs):
            reply = fixture.fake(argv, **kwargs)
            if argv[1:3] == ["reference", "add"]:
                return subprocess.CompletedProcess(argv, 1, json.dumps({"ok": False, "error": {"code": "refused"}}), "")
            return reply

        with patch("workspaces.subprocess.run", side_effect=transport):
            self.assert_optional_failure_continues(fixture)
        self.assertEqual(fixture.fake.count("reference", "add"), 1)
        self.assertEqual(fixture.fake.count("agent", "launch"), 1)
        persisted = wb.read(fixture.cfg.ticket_dir("ZIP-1"))
        assert persisted is not None
        self.assertEqual(persisted.operations["reference"]["status"], "pending")

    def test_orca_lost_browser_reply_does_not_replay_or_block_agent(self):
        fixture, _ = self.fixture("orca", launch=False)
        fixture.fake.fail = ("tab", "create")
        self.assert_optional_failure_continues(fixture)
        self.assertEqual(fixture.fake.count("agent", "launch"), 1)
        self.assertEqual(fixture.fake.count("tab", "create"), 2)
        persisted = wb.read(fixture.cfg.ticket_dir("ZIP-1"))
        assert persisted is not None
        self.assertEqual(persisted.operations["view-reference"]["status"], "pending")
        self.assertEqual(persisted.operations["view-reference"]["operation_id"], "accepted-launch")

    def test_cmux_lost_browser_reply_does_not_replay_or_block_agent(self):
        fixture, _ = self.fixture("cmux", launch=False)
        attempted_urls = []

        def lost_reply(workspace, surface_type="terminal", url=None):
            self.assertEqual(workspace, "uuid-original")
            self.assertEqual(surface_type, "browser")
            attempted_urls.append(url)
            raise subprocess.TimeoutExpired("cmux browser", 5)

        fixture.cc.cmux_new_surface = lost_reply
        self.assert_optional_failure_continues(fixture)
        self.assertEqual(len(attempted_urls), 2)
        self.assertEqual(len([call for call in fixture.cc.calls if isinstance(call, tuple) and call[0] == "agent"]), 1)

    def assert_optional_failure_continues(self, fixture):
        warnings = []
        try:
            worktree = drip._workspace_action(fixture.cfg, "ZIP-1", fixture.launch, log=warnings.append)
        except drip.SpinupFailed as exc:
            self.fail(f"Optional view failure stranded an already submitted agent: {exc}")
        persisted = wb.read(fixture.cfg.ticket_dir("ZIP-1"))
        assert persisted is not None
        self.assertEqual(persisted.phase, "ready")
        self.assertTrue(any("view" in warning.lower() for warning in warnings))
        self.assertEqual(worktree, pathlib.Path(persisted.worktree))
        restarted = fixture.launch()
        self.assertEqual(restarted.status, "ok")
        self.assertTrue(restarted.warnings)
        started = []
        supervisor = types.SimpleNamespace(start=lambda key, wt, root: started.append((key, wt, root)))
        drip._finish_spinup(fixture.cfg, "ZIP-1", worktree, supervisor)
        self.assertEqual(state.load(fixture.cfg.ticket_dir("ZIP-1")).worktree, str(worktree))
        self.assertEqual(started, [("ZIP-1", str(worktree), fixture.cfg.state_root)])

    def test_optional_views_use_short_timeout_after_agent_submission(self):
        fixture, _ = self.fixture("orca", launch=False)
        deadlines = []

        def transport(argv, **kwargs):
            if argv[1:3] == ["reference", "add"]:
                deadlines.append(kwargs["timeout"])
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            return fixture.fake(argv, **kwargs)

        with patch("workspaces.subprocess.run", side_effect=transport):
            self.assert_optional_failure_continues(fixture)
        self.assertEqual(len(deadlines), 1)
        self.assertLessEqual(deadlines[0], 10)

    def test_required_agent_ack_keeps_launch_timeout_budget(self):
        fixture, _ = self.fixture("orca", launch=False)

        def slow_agent(argv, **kwargs):
            if argv[1:3] == ["agent", "launch"] and kwargs["timeout"] < 20:
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            return fixture.fake(argv, **kwargs)

        with patch("workspaces.subprocess.run", side_effect=slow_agent):
            self.assertEqual(fixture.launch().status, "ok")
        self.assertEqual(fixture.fake.count("agent", "launch"), 1)

    def test_orca_cosmetic_observation_does_not_wait_minutes_on_ipc(self):
        self.assert_cosmetic_timeout("observe")

    def test_orca_cosmetic_observation_does_not_wait_minutes_on_git(self):
        self.assert_cosmetic_timeout("observe", hang_git=True)

    def test_orca_notification_does_not_wait_minutes_on_ipc(self):
        self.assert_cosmetic_timeout("notify")

    def assert_cosmetic_timeout(self, operation, *, hang_git=False):
        fixture, binding = self.fixture("orca")
        virtual_wait = 0

        def unresponsive(argv, **kwargs):
            nonlocal virtual_wait
            if (argv[0] == "git") if hang_git else (argv[1:3] == ["worktree", "show"]):
                virtual_wait += kwargs["timeout"]
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            return fixture.fake(argv, **kwargs)

        with patch("workspaces.subprocess.run", side_effect=unresponsive), \
                patch("workspaces.PRODUCTION_ROOT", fixture.cfg.state_root), \
                patch("config.load", return_value=fixture.cfg), self.assertRaises(workspaces.WorkspaceError):
            if operation == "notify":
                workspaces.notify(fixture.cfg.ticket_dir("ZIP-1"), "ZIP-1", "Review needed", "Needs attention")
            else:
                workspaces.observe(fixture.cfg, state.Ticket(key="ZIP-1", worktree=binding.worktree, branch=binding.branch))
        self.assertGreater(virtual_wait, 0)
        self.assertLessEqual(virtual_wait, 10, "Cosmetic failure must return promptly, not wait for launch-scale timeouts")

    def test_overview_reports_existing_native_worktree_outside_legacy_root(self):
        fixture, binding = self.fixture("orca")
        state.save(fixture.cfg.ticket_dir("ZIP-1"), state.Ticket(key="ZIP-1", worktree=binding.worktree, branch=binding.branch))
        output = io.StringIO()
        with patch.dict("os.environ", {"HOME": str(fixture.root / "isolated-home")}), \
                patch("supervise.active", return_value=[]), patch("ledger.collect", return_value=[]), \
                contextlib.redirect_stdout(output):
            result = runner.main(["--config", str(fixture.root / "hopper.toml"), "overview"])
        self.assertEqual(result, 0)
        row = next(line for line in output.getvalue().splitlines() if line.startswith("ZIP-1 "))
        self.assertIn("wt=yes", row)

    def test_explicit_supervisor_start_uses_recorded_native_worktree(self):
        fixture, binding = self.fixture("orca")
        state.save(fixture.cfg.ticket_dir("ZIP-1"), state.Ticket(key="ZIP-1", worktree=binding.worktree, branch=binding.branch))
        started = []

        def start(key, worktree, root):
            started.append((key, worktree, root))
            return "fixture supervisor"

        with patch.dict("os.environ", {"HOME": str(fixture.root / "isolated-home")}), \
                patch("supervise.start", side_effect=start), contextlib.redirect_stdout(io.StringIO()):
            result = runner.main(["--config", str(fixture.root / "hopper.toml"), "supervise", "start", "--ticket", "ZIP-1"])
        self.assertEqual(result, 0)
        self.assertEqual(started, [("ZIP-1", binding.worktree, fixture.cfg.state_root)])

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
