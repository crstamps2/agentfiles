import pathlib
import unittest
from unittest.mock import patch

import cohort
import config
import locks
import state
import test_workspace_capacity as fixtures


class Supervisor:
    def __init__(self):
        self.stopped = []

    def stop(self, key):
        self.stopped.append(key)

    def status(self, key):
        return "not loaded" if key in self.stopped else "state=active"


class OnHoldTests(unittest.TestCase):
    cfg: config.Config
    rows: list[dict]

    setUp = fixtures.WorkspaceCapacityTests.setUp
    inventory = fixtures.WorkspaceCapacityTests.inventory
    fill = fixtures.WorkspaceCapacityTests.fill

    def member(self, key, index):
        path = pathlib.Path(self.rows[index]["path"])
        path.mkdir(exist_ok=True)
        state.save(self.cfg.ticket_dir(key), state.Ticket(
            key=key, state="blocked", worktree=str(path), reason="owner question", needs_operator="owner question"))
        cohort.reserve(self.cfg, key, "test")
        cohort.activate(self.cfg, key)

    def prepare(self):
        self.fill(2)
        self.member("ZIP-9119", 0)
        self.member("ZIP-4246", 1)
        self.rows[0]["workspaceStatus"] = "status-6"
        self.supervisor = Supervisor()

    def test_release_preserves_state_and_other_member_without_parking(self):
        self.prepare()
        before = (self.cfg.ticket_dir("ZIP-9119") / "state.json").read_bytes()
        self.assertTrue(cohort.release_on_hold(self.cfg, "ZIP-9119", supervisor=self.supervisor))
        self.assertEqual(cohort.members(self.cfg), ["ZIP-4246"])
        self.assertEqual(self.supervisor.stopped, ["ZIP-9119"])
        self.assertEqual((self.cfg.ticket_dir("ZIP-9119") / "state.json").read_bytes(), before)
        self.assertFalse((self.cfg.ticket_dir("ZIP-9119") / "PARKED").exists())
        self.assertEqual(cohort.load(self.cfg).history[-1]["reason"], "operator-on-hold")
        with self.assertRaisesRegex(cohort.WorkspaceCapacityError, "On Hold"):
            cohort.reserve(self.cfg, "ZIP-9119", "drip")

    def test_live_ticket_writer_defers_without_stopping_anyone(self):
        self.prepare()
        lease = locks.Lease(self.cfg.state_root / "locks" / "runner-zip-9119", "test")
        self.assertTrue(lease.acquire(hold=True))
        try:
            self.assertFalse(cohort.release_on_hold(self.cfg, "ZIP-9119", supervisor=self.supervisor))
        finally:
            lease.release()
        self.assertEqual(set(cohort.members(self.cfg)), {"ZIP-9119", "ZIP-4246"})
        self.assertEqual(self.supervisor.stopped, [])

    def test_no_status_change_or_failed_supervisor_stop_does_not_release(self):
        self.prepare()
        self.rows[0]["workspaceStatus"] = "in-progress"
        self.assertFalse(cohort.release_on_hold(self.cfg, "ZIP-9119", supervisor=self.supervisor))
        self.assertEqual(self.supervisor.stopped, [])
        self.rows[0]["workspaceStatus"] = "status-6"
        with patch.object(self.supervisor, "status", return_value="state=active"), self.assertRaisesRegex(
                cohort.CohortError, "still loaded"):
            cohort.release_on_hold(self.cfg, "ZIP-9119", supervisor=self.supervisor)
        self.assertIn("ZIP-9119", cohort.members(self.cfg))

    def test_normal_reconciler_applies_hold_before_any_recovery_or_refill(self):
        self.prepare()
        with patch.object(cohort, "_reconcile_locked", return_value=cohort.CohortReport()) as inner, patch(
                "drip._tick_locked", return_value=[]), patch("sidebar.sync"), patch("sidebar.watchdog"):
            report = cohort.reconcile(self.cfg, supervisor=self.supervisor)
        self.assertIn("ZIP-9119", report.released)
        self.assertNotIn("ZIP-9119", cohort.members(self.cfg))
        self.assertEqual(self.supervisor.stopped, ["ZIP-9119"])
        inner.assert_called_once()


if __name__ == "__main__":
    unittest.main()
