"""Notification signatures stay shared; delivery targets only the recorded workspace."""
import os
import pathlib
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import config
import lifecycle
import state
import workspace_binding as wb
import workspaces
from test_config import MINI


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        path = self.root / "hopper.toml"
        path.write_text(MINI.format(root=str(self.root / "state")))
        self.cfg = config.load(path)
        self.cfg.ensure_dirs()
        self.tdir = self.cfg.ticket_dir("ZIP-1")
        state.save(self.tdir, state.Ticket(key="ZIP-1", workspace="workspace:1"))
        production = patch("workspaces.PRODUCTION_ROOT", self.cfg.state_root)
        production.start()
        self.addCleanup(production.stop)
        loaded_config = patch("config.load", return_value=self.cfg)
        loaded_config.start()
        self.addCleanup(loaded_config.stop)

    def test_notification_selects_binding_not_new_default(self):
        wb.write(self.tdir, wb.Binding(backend="orca", key="ZIP-1", workspace_id="repo::/worktree", worktree="/worktree", repo_id="repo"))
        with patch("workspaces.WorkspaceBackend.adapter", autospec=True) as adapter:
            self.assertTrue(workspaces.notify(self.tdir, "ZIP-1", "Title", "Body"))
        self.assertEqual(adapter.call_args.args[0].name, "orca")
        notice = adapter.return_value.notify.call_args.args[0]
        self.assertEqual(notice.workspace_id, "repo::/worktree")

    def test_legacy_notification_remains_cmux(self):
        with patch("workspaces.WorkspaceBackend.adapter", autospec=True) as adapter:
            self.assertTrue(workspaces.notify(self.tdir, "ZIP-1", "Title", "Body"))
        self.assertEqual(adapter.call_args.args[0].name, "cmux")

    def test_corrupt_notification_binding_never_falls_back(self):
        (self.tdir / "workspace.json").write_text("{")
        with patch("workspaces.WorkspaceBackend.adapter") as adapter, self.assertRaises(wb.BindingError):
            workspaces.notify(self.tdir, "ZIP-1", "Title", "Body")
        adapter.assert_not_called()

    def test_no_successful_delivery_is_not_cached(self):
        with patch("workspaces.notify", return_value=False) as app, patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1)):
            lifecycle._notify_once(self.tdir, "ZIP-1", "Title", "Body")
            lifecycle._notify_once(self.tdir, "ZIP-1", "Title", "Body")
        self.assertEqual(app.call_count, 2)
        self.assertEqual(list(self.tdir.glob(".notified-*")), [])

    def test_uncertain_notification_is_not_blindly_replayed(self):
        with patch("workspaces.notify", side_effect=TimeoutError("reply lost")) as app, patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1)):
            lifecycle._notify_once(self.tdir, "ZIP-1", "Title", "Body")
            lifecycle._notify_once(self.tdir, "ZIP-1", "Title", "Body")
        self.assertEqual(app.call_count, 1)
        self.assertEqual(len(list(self.tdir.glob(".notified-*"))), 1)

    def test_delivery_deduplicates_and_renotifies(self):
        with patch("workspaces.notify", return_value=True) as app, patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as mac:
            lifecycle._notify_once(self.tdir, "ZIP-1", "Title", "Body")
            lifecycle._notify_once(self.tdir, "ZIP-1", "Title", "Body")
            self.assertEqual(app.call_count, 1)
            marker = next(self.tdir.glob(".notified-*"))
            old = time.time() - lifecycle.RENOTIFY_S - 60
            os.utime(marker, (old, old))
            lifecycle._notify_once(self.tdir, "ZIP-1", "Title", "Body")
        self.assertEqual(app.call_count, 2)
        self.assertEqual(mac.call_count, 2)
        self.assertEqual(mac.call_args.args[0][0], "osascript")

    def test_nonproduction_notifications_have_no_app_or_os_side_effects(self):
        with patch("workspaces.PRODUCTION_ROOT", self.root / "other"), patch("subprocess.run") as run, patch("workspaces.WorkspaceBackend.adapter") as adapter:
            self.assertFalse(workspaces.notify(self.tdir, "ZIP-1", "Title", "Body"))
            lifecycle._notify_once(self.tdir, "ZIP-1", "Title", "Body")
        run.assert_not_called()
        adapter.assert_not_called()
