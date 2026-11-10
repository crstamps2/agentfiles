"""Business status/routing policy must not depend on an app's transport."""
import dataclasses
import datetime as dt
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import config
import locks
import sidebar
import state
import workspace_binding as wb
import workspaces
from test_config import MINI


class App:
    def __init__(self, binding):
        self.binding = binding
        self.identity = binding.workspace_id
        self.lane = "in-progress"
        self.presentations = []
        self.moves = 0
        self.fail = False
        self.before_move = lambda: True

    def inspect(self, binding):
        return workspaces.Snapshot(binding, self.identity, self.lane)

    def present(self, binding, value):
        if self.fail:
            raise workspaces.WorkspaceError("app-unavailable")
        self.presentations.append(value)

    def move_to_review(self, binding, expected):
        if self.lane != "in-progress" or self.identity != expected.identity or not self.before_move():
            return False
        self.lane = "ready-for-review"
        self.moves += 1
        return True


class MixedSidebarTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        path = self.root / "hopper.toml"
        path.write_text(MINI.format(root=str(self.root / "state")))
        self.cfg = config.load(path)
        self.cfg.ensure_dirs()
        self.apps = {}
        for key, backend in (("ZIP-1", "cmux"), ("ZIP-2", "orca")):
            binding = wb.Binding(backend=backend, key=key, workspace_id=f"{backend}-{key}", phase="ready")
            wb.write(self.cfg.ticket_dir(key), binding)
            self.apps[key] = App(binding)
            self.ticket(key, "implement")
        patches = [patch("sidebar.PRODUCTION_ROOT", self.cfg.state_root),
                   patch("workspaces.observe", side_effect=self.observe),
                   patch("cohort._head", return_value="head"), patch("watch.pr_state", return_value="ready")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def ticket(self, key, stage, **kwargs):
        t = state.Ticket(key=key, state=stage, worktree=str(self.root), evidence_sha="head", **kwargs)
        state.save(self.cfg.ticket_dir(key), t)
        (self.cfg.ticket_dir(key) / "ci.json").write_text(json.dumps({"pr": 123}))
        return t

    def observe(self, cfg, ticket, **kwargs):
        app = self.apps[ticket.key]
        return app, app.inspect(app.binding)

    def sync(self):
        sidebar.sync(self.cfg, run=lambda *args, **kwargs: self.fail("Unexpected transport outside adapter"))

    def test_both_backends_receive_identical_status_semantics(self):
        cases = [("implement", {}, "implementing"),
                 ("blocked", {"recovery": {"active": {"status": "waiting-provider"}}}, "quota wait"),
                 ("blocked", {"recovery": {"active": {"status": "waiting-repair"}}}, "self-healing"),
                 ("decision-required", {}, "needs Cody"), ("human-gate-1", {}, "ready · PR #123")]
        for stage, kwargs, expected in cases:
            with self.subTest(stage=stage):
                for key in self.apps:
                    self.ticket(key, stage, **kwargs)
                self.sync()
                left, right = (a.presentations[-1] for a in self.apps.values())
                self.assertEqual(left, right)
                self.assertEqual(left.text, expected)
        for key in self.apps:
            (self.cfg.ticket_dir(key) / "INTERACTIVE.json").write_text("{}")
        self.sync()
        self.assertEqual(self.apps["ZIP-2"].presentations[-1].text, "interactive")
        for key in self.apps:
            (self.cfg.ticket_dir(key) / "INTERACTIVE.json").unlink()
            self.ticket(key, "implement")
            path = self.cfg.ticket_dir(key) / "state.json"
            raw = json.loads(path.read_text())
            raw["updated_utc"] = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=6)).isoformat()
            path.write_text(json.dumps(raw))
        self.sync()
        self.assertEqual(self.apps["ZIP-1"].presentations[-1].text, "stalled 6h")
        self.assertEqual(self.apps["ZIP-1"].presentations[-1], self.apps["ZIP-2"].presentations[-1])

    def test_mixed_fleet_routes_each_binding_once(self):
        for key in self.apps:
            self.ticket(key, "human-gate-1")
        self.sync()
        for app in self.apps.values():
            self.assertEqual(app.moves, 1)
            app.lane = "in-progress"  # Human return after a completed automatic move.
        self.sync()
        self.assertEqual([a.moves for a in self.apps.values()], [1, 1])

    def test_backend_failure_does_not_block_other_backend(self):
        self.apps["ZIP-1"].fail = True
        self.sync()
        self.assertEqual(len(self.apps["ZIP-2"].presentations), 1)
        self.assertEqual(len(self.apps["ZIP-1"].presentations), 0)

    def test_failed_presentation_is_not_cached(self):
        self.apps["ZIP-2"].fail = True
        self.sync()
        self.apps["ZIP-2"].fail = False
        self.sync()
        self.assertEqual([len(a.presentations) for a in self.apps.values()], [1, 1])

    def test_replaced_resource_during_remote_verification_is_not_routed(self):
        for key in self.apps:
            self.ticket(key, "human-gate-1")
        def replace(pr):
            self.apps["ZIP-1"].identity = "replacement"
            self.apps["ZIP-2"].lane = "other"
            return "ready"
        with patch("watch.pr_state", side_effect=replace):
            self.sync()
        self.assertEqual([a.moves for a in self.apps.values()], [0, 0])

    def test_stale_or_not_ready_evidence_never_routes_either_backend(self):
        for key in self.apps:
            self.ticket(key, "human-gate-1")
        for status in ("draft", "closed", "merged", "unknown"):
            with self.subTest(status=status), patch("watch.pr_state", return_value=status):
                self.sync()
                self.assertEqual([a.moves for a in self.apps.values()], [0, 0])
        for key in self.apps:
            ticket = state.load(self.cfg.ticket_dir(key))
            ticket.evidence_sha = "stale"
            state.save(self.cfg.ticket_dir(key), ticket)
        self.sync()
        self.assertEqual([a.moves for a in self.apps.values()], [0, 0])

    def test_owned_parked_or_recovering_tickets_never_route_either_backend(self):
        for marker in ("HUMAN", "INTERACTIVE.json", "PARKED"):
            with self.subTest(marker=marker):
                for key in self.apps:
                    self.ticket(key, "human-gate-1")
                    (self.cfg.ticket_dir(key) / marker).write_text("{}")
                self.sync()
                self.assertEqual([a.moves for a in self.apps.values()], [0, 0])
                for key in self.apps:
                    (self.cfg.ticket_dir(key) / marker).unlink()
        for key in self.apps:
            self.ticket(key, "human-gate-1", recovery={"active": {"status": "waiting-repair"}})
        self.sync()
        self.assertEqual([a.moves for a in self.apps.values()], [0, 0])

    def test_earlier_manual_board_move_is_preserved_for_both_backends(self):
        for key, app in self.apps.items():
            self.ticket(key, "human-gate-1")
            app.lane = "other"
        with patch("watch.pr_state") as lookup:
            self.sync()
        lookup.assert_not_called()
        self.assertEqual([a.moves for a in self.apps.values()], [0, 0])

    def test_unfinished_or_uncertain_binding_never_routes(self):
        for phase in ("launching", "setup-failed", "uncertain"):
            with self.subTest(phase=phase):
                for key, app in self.apps.items():
                    self.ticket(key, "human-gate-1")
                    wb.write(self.cfg.ticket_dir(key), dataclasses.replace(app.binding, phase=phase))
                self.sync()
                self.assertEqual([a.moves for a in self.apps.values()], [0, 0])

    def test_binding_change_during_remote_verification_prevents_movement(self):
        for key in self.apps:
            self.ticket(key, "human-gate-1")
        def replace(pr):
            for key in self.apps:
                path = self.cfg.ticket_dir(key) / "workspace.json"
                raw = json.loads(path.read_text())
                raw["phase"] = "uncertain"
                path.write_text(json.dumps(raw))
            return "ready"
        with patch("watch.pr_state", side_effect=replace):
            self.sync()
        self.assertEqual([a.moves for a in self.apps.values()], [0, 0])

    def test_uncertain_route_is_not_replayed_after_a_human_return(self):
        for key in self.apps:
            self.ticket(key, "human-gate-1")
        def lose_reply(app, binding, expected):
            app.lane = "ready-for-review"
            app.moves += 1
            raise TimeoutError("reply lost after movement")
        with patch.object(App, "move_to_review", lose_reply):
            self.sync()
        for app in self.apps.values():
            app.lane = "in-progress"
        self.sync()
        self.assertEqual([a.moves for a in self.apps.values()], [1, 1])

    def test_another_status_writer_prevents_a_competing_sync(self):
        lease = locks.Lease(self.cfg.state_root / "locks" / "sidebar", "test writer")
        self.assertTrue(lease.acquire(hold=True))
        try:
            self.sync()
        finally:
            lease.release()
        self.assertEqual([len(a.presentations) for a in self.apps.values()], [0, 0])

    def test_route_intent_must_be_durable_before_movement(self):
        for key in self.apps:
            self.ticket(key, "human-gate-1")
        (self.cfg.state_root / "sidebar.json").mkdir()
        self.sync()
        self.assertEqual([a.moves for a in self.apps.values()], [0, 0])

    def test_nonproduction_root_is_nonmutating_even_when_forced(self):
        with patch("sidebar.PRODUCTION_ROOT", self.root / "elsewhere"):
            sidebar.sync(self.cfg, force=True)
        self.assertEqual([len(a.presentations) for a in self.apps.values()], [0, 0])
