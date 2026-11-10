"""Selection must not relocate old jobs or guess after a corrupt receipt."""
import dataclasses
import pathlib
import tempfile
import unittest

import config
import state
import workspace_binding as wb
import workspaces
from test_config import MINI


class WorkspaceSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.path = root / "hopper.toml"
        self.path.write_text(MINI.format(root=str(root / "state")))
        self.cfg = config.load(self.path)
        self.ticket = state.Ticket(key="ZIP-1", worktree="/workspaces/existing")

    def select(self, **kwargs):
        return workspaces.backend_for(self.cfg, self.ticket, **kwargs).name

    def bind(self):
        wb.write(self.cfg.ticket_dir("ZIP-1"), wb.Binding(backend="orca", key="ZIP-1", worktree="/workspaces/existing"))

    def test_new_launch_defaults_to_orca(self):
        self.ticket = state.Ticket(key="ZIP-1")
        self.assertEqual(self.select(new_launch=True), "orca")

    def test_existing_legacy_launch_cannot_implicitly_switch_backend(self):
        ticket = state.Ticket(key="ZIP-1", worktree="/worktrees/zip-1", workspace="workspace:1")
        self.assertEqual(workspaces.backend_for(self.cfg, ticket, new_launch=True).name, "cmux")
        with self.assertRaises(wb.BindingError):
            workspaces.backend_for(self.cfg, ticket, new_launch=True, override="orca")

    def test_explicit_cmux_override(self):
        self.ticket = state.Ticket(key="ZIP-1")
        self.assertEqual(self.select(new_launch=True, override="cmux"), "cmux")
        self.path.write_text(self.path.read_text() + '\n[workspace]\nbackend="cmux"\n')
        self.cfg = config.load(self.path)
        self.assertEqual(self.select(new_launch=True), "cmux")

    def test_legacy_ticket_stays_cmux(self):
        self.assertEqual(self.select(), "cmux")

    def test_bound_orca_survives_default_change(self):
        self.bind()
        self.cfg = dataclasses.replace(self.cfg, workspace_backend="cmux")
        self.assertEqual(self.select(), "orca")
        self.assertEqual(self.select(new_launch=True), "orca")

    def test_conflicting_override_is_refused(self):
        self.bind()
        with self.assertRaises(wb.BindingError):
            self.select(new_launch=True, override="cmux")
        with self.assertRaises(wb.BindingError):
            self.select(override="invalid")

    def test_corrupt_binding_never_becomes_a_new_default(self):
        p = self.cfg.ticket_dir("ZIP-1")
        p.mkdir(parents=True)
        (p / "workspace.json").write_text("{")
        with self.assertRaises(wb.BindingError):
            self.select(new_launch=True)

    def test_invalid_backend_is_config_error(self):
        self.path.write_text(self.path.read_text() + '\n[workspace]\nbackend="typo"\n')
        with self.assertRaises(config.ConfigError):
            config.load(self.path)

    def test_returned_orca_path_counts_as_in_flight(self):
        import drip
        path = self.path.parent / "native" / "zip-1"
        path.mkdir(parents=True)
        state.save(self.cfg.ticket_dir("ZIP-1"), state.Ticket(key="ZIP-1", state="implement", worktree=str(path)))
        self.assertIn("ZIP-1", drip.in_flight(self.cfg))

    def test_workspace_paths_and_status_mapping_are_validated(self):
        self.path.write_text(self.path.read_text() + '\n[workspace]\nrepo="~/workspace/example-app"\n[workspace.statuses]\nready_for_review="status-5"\n')
        cfg = config.load(self.path)
        self.assertTrue(cfg.workspace_repo.is_absolute())
        self.assertEqual(cfg.workspace_statuses["ready_for_review"], "status-5")
        self.path.write_text(self.path.read_text().replace('"status-5"', '""'))
        with self.assertRaises(config.ConfigError):
            config.load(self.path)
