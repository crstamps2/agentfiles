"""Backend defaults cannot change admission, ownership, or recovery policy."""
import dataclasses
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import cohort
import config
import state
import workspace_binding as wb
import workspaces
from test_config import MINI


class ReadyApp(workspaces.Adapter):
    def prepare(self, binding):
        return None

    def inspect(self, binding):
        return workspaces.Snapshot(binding, binding.workspace_id, "in-progress")


class MissingApp(ReadyApp):
    def prepare(self, binding):
        raise workspaces.WorkspaceError("backend-unavailable")


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        path = self.root / "hopper.toml"
        path.write_text(MINI.format(root=str(self.root / "state")))
        self.cfg = dataclasses.replace(config.load(path), cohort_capacity=2)
        self.cfg.ensure_dirs()

    def test_production_default_and_mapping_keep_two_slot_capacity(self):
        cfg = config.load(pathlib.Path(__file__).parent.parent / "hopper.toml")
        self.assertEqual(cfg.workspace_backend, "orca")
        self.assertEqual(cfg.workspace_statuses.get("in_progress"), "in-progress")
        self.assertEqual(cfg.workspace_statuses.get("ready_for_review"), "status-5")
        self.assertEqual(cfg.cohort_capacity, 2)
        self.assertEqual(cfg.drip["max_in_flight"], 2)

    def test_new_default_does_not_relocate_existing_jobs(self):
        ticket = state.Ticket(key="ZIP-1", worktree=str(self.root))
        self.assertEqual(workspaces.backend_for(self.cfg, ticket, new_launch=True).name, "cmux")
        binding = wb.Binding(backend="orca", key=ticket.key, worktree=str(self.root))
        wb.write(self.cfg.ticket_dir(ticket.key), binding)
        opposite = dataclasses.replace(self.cfg, workspace_backend="cmux")
        self.assertEqual(workspaces.backend_for(opposite, ticket, new_launch=True).name, "orca")
        self.assertEqual(workspaces.backend_for(opposite, state.Ticket(key="ZIP-2"), new_launch=True).name, "cmux")

    def test_two_full_slots_and_foreground_ownership_are_unchanged(self):
        cohort.initialize(self.cfg)
        for key in ("ZIP-1", "ZIP-2"):
            cohort.reserve(self.cfg, key, "fixture")
        before = (self.cfg.state_root / "cohort.json").read_bytes()
        for key, backend in (("ZIP-3", "orca"), ("ZIP-4", "cmux")):
            tdir = self.cfg.ticket_dir(key)
            state.save(tdir, state.Ticket(key=key, state="implement", worktree=str(self.root)))
            owner = tdir / "INTERACTIVE.json"
            owner.write_text('{"session":"original","mode":"interactive"}')
            original_owner = owner.read_bytes()
            wb.write(tdir, wb.Binding(backend=backend, key=key, phase="ready", worktree=str(self.root),
                                     workspace_id=f"{backend}-{key}", views={"app": "original-page"}))
            def adapter(selected, backend=backend, **kwargs):
                self.assertEqual(selected.name, backend)
                return ReadyApp(self.cfg, run=lambda *a, **k: self.fail("Unexpected host command"), checkpoint=lambda _: self.fail("Unexpected binding write"))
            with patch("workspaces.WorkspaceBackend.adapter", autospec=True, side_effect=adapter):
                result = workspaces.launch(self.cfg, key, "", f"/loop-watch {key}", "reference")
            self.assertEqual(result.binding.backend, backend)
            self.assertEqual(owner.read_bytes(), original_owner)
            self.assertEqual((self.cfg.state_root / "cohort.json").read_bytes(), before)
        self.assertEqual(cohort.available(self.cfg), 0)
        self.assertEqual(cohort.members(self.cfg), ["ZIP-1", "ZIP-2"])

    def test_backend_error_keeps_reservation_and_recovery_target(self):
        cohort.initialize(self.cfg)
        for key, backend in (("ZIP-1", "orca"), ("ZIP-2", "cmux")):
            cohort.reserve(self.cfg, key, "fixture")
            tdir = self.cfg.ticket_dir(key)
            state.save(tdir, state.Ticket(key=key, state="spinup", recovery={"active": {"resume": {"kind": "setup", "state": "spinup"}}}))
            before = (tdir / "state.json").read_bytes()
            slots = (self.cfg.state_root / "cohort.json").read_bytes()
            def adapter(selected, backend=backend, tdir=tdir, **kwargs):
                self.assertEqual(selected.name, backend)
                return MissingApp(self.cfg, run=lambda *a, **k: self.fail("Unexpected host command"), checkpoint=lambda b: wb.write(tdir, b))
            with patch("workspaces.WorkspaceBackend.adapter", autospec=True, side_effect=adapter), self.assertRaises(workspaces.WorkspaceError):
                workspaces.launch(self.cfg, key, "branch", f"/loop-watch {key}", "reference", backend=backend)
            self.assertEqual((tdir / "state.json").read_bytes(), before)
            self.assertEqual((self.cfg.state_root / "cohort.json").read_bytes(), slots)
            self.assertIn(key, cohort.members(self.cfg))
