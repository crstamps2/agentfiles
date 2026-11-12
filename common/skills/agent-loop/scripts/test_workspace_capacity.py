import copy
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import cohort
import config
import drip
import state
import workspace_capacity as capacity


class WorkspaceCapacityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.path = root / "hopper.toml"
        text = config.EXAMPLE_PATH.read_text().replace(
            'state_root = "~/.local/state/agent-loop"', f'state_root = "{root}/state"')
        self.path.write_text(text)
        self.cfg = config.load(self.path)
        self.cfg.ensure_dirs()
        cohort.initialize(self.cfg)
        self.rows = []
        self.reader = self.enterContext(patch.object(capacity, "read_inventory", side_effect=self.inventory))

    def inventory(self, _cfg):
        self.assertFalse(str(self.cfg.state_root.resolve()) in getattr(cohort._HELD, "roots", set()))
        return {"worktrees": copy.deepcopy(self.rows), "totalCount": len(self.rows),
                "truncated": False, "hostScope": {"hostIds": ["local"], "omittedHostIds": []}}

    def count(self):
        inventory = capacity.observe(self.cfg)
        assert inventory is not None
        return inventory.count

    def fill(self, count):
        self.rows = [{"path": f"{self.tmp.name}/workspace-{i}", "hostId": "local",
                      "isMainWorktree": False, "workspaceStatus": "in-progress"} for i in range(count)]

    def test_production_config_keeps_two_execution_slots_and_ten_workspaces(self):
        self.assertEqual(self.cfg.cohort_capacity, 2)
        self.assertEqual(self.cfg.workspace_limit, 10)
        self.assertEqual(self.cfg.workspace_statuses["approved"], "status-7")
        self.assertEqual(self.cfg.workspace_statuses["on_hold"], "status-6")

    def test_only_the_four_requested_statuses_are_excluded(self):
        statuses = ["in-review", "status-7", "completed", "status-6", "In Review",
                    "APPROVED", "Done", "on_hold", "status-5", "in-progress", None, "todo"]
        self.fill(len(statuses))
        for row, status in zip(self.rows, statuses, strict=True):
            row["workspaceStatus"] = status
        self.assertEqual(self.count(), 4)

    def test_counts_manual_foreground_and_archived_workspaces_once(self):
        self.fill(2)
        self.rows[0]["isArchived"] = True
        self.rows.append(copy.deepcopy(self.rows[0]))
        self.rows.append({"path": self.tmp.name, "isMainWorktree": True, "workspaceStatus": "in-progress"})
        (self.cfg.ticket_dir("ZIP-1")).mkdir()
        (self.cfg.ticket_dir("ZIP-1") / "HUMAN").touch()
        self.assertEqual(self.count(), 2)

    def test_nine_allows_one_pending_workspace_and_blocks_the_next(self):
        self.fill(9)
        cohort.reserve(self.cfg, "ZIP-100", "drip")
        before = (self.cfg.state_root / "cohort.json").read_bytes()
        with self.assertRaisesRegex(cohort.WorkspaceCapacityError, "10/10"):
            cohort.reserve(self.cfg, "ZIP-101", "drip")
        self.assertEqual((self.cfg.state_root / "cohort.json").read_bytes(), before)

    def test_ten_refuses_without_writes_then_review_status_allows_refill(self):
        self.fill(10)
        before = (self.cfg.state_root / "cohort.json").read_bytes()
        with self.assertRaisesRegex(cohort.WorkspaceCapacityError, "10/10"):
            cohort.reserve(self.cfg, "ZIP-100", "drip")
        self.assertEqual((self.cfg.state_root / "cohort.json").read_bytes(), before)
        self.rows[0]["workspaceStatus"] = "in-review"
        self.assertEqual(cohort.reserve(self.cfg, "ZIP-100", "drip").ticket, "ZIP-100")

    def test_existing_counted_workspace_does_not_need_a_new_workspace_slot(self):
        self.fill(10)
        state.save(self.cfg.ticket_dir("ZIP-100"), state.Ticket(
            key="ZIP-100", worktree=self.rows[0]["path"]))
        cohort.reserve(self.cfg, "ZIP-100", "parked")
        self.reader.reset_mock()
        cohort.reserve(self.cfg, "ZIP-100", "parked")
        self.reader.assert_not_called()

    def test_active_on_hold_workspace_is_not_added_back_from_cohort_history(self):
        self.fill(9)
        state.save(self.cfg.ticket_dir("ZIP-100"), state.Ticket(
            key="ZIP-100", worktree=self.rows[0]["path"]))
        cohort.reserve(self.cfg, "ZIP-100", "drip")
        cohort.activate(self.cfg, "ZIP-100")
        self.rows[0]["workspaceStatus"] = "status-6"
        cohort.reserve(self.cfg, "ZIP-101", "drip")
        self.assertEqual(self.count(), 8)

    def test_generation_race_fails_closed(self):
        self.fill(9)
        original = self.inventory

        def race(cfg):
            raw = original(cfg)
            with cohort._lease(cfg):
                value = cohort._load_unlocked(cfg)
                value.generation += 1
                cohort._save_unlocked(cfg, value)
            return raw

        self.reader.side_effect = race
        with self.assertRaisesRegex(cohort.WorkspaceCapacityError, "retry next cycle"):
            cohort.reserve(self.cfg, "ZIP-100", "drip")
        self.assertFalse(any(slot.ticket for slot in cohort.load(self.cfg).slots))

    def test_partial_inventory_and_native_failures_refuse_admission(self):
        self.fill(9)
        good = self.inventory(self.cfg)
        bad = [dict(good, truncated=True), dict(good, totalCount=10),
               dict(good, hostScope={"hostIds": ["local"], "omittedHostIds": ["ssh:offline"]})]
        for raw in bad:
            with self.subTest(raw=raw):
                self.reader.side_effect = None
                self.reader.return_value = raw
                with self.assertRaises(cohort.WorkspaceCapacityError):
                    cohort.reserve(self.cfg, "ZIP-100", "drip")
        self.reader.side_effect = capacity.WorkspaceError("orca-unconfirmed")
        with self.assertRaises(cohort.WorkspaceCapacityError):
            cohort.reserve(self.cfg, "ZIP-100", "drip")
        self.assertFalse(any(slot.ticket for slot in cohort.load(self.cfg).slots))

    def test_dry_run_at_nine_previews_one_and_changes_no_state(self):
        self.fill(9)
        before = (self.cfg.state_root / "cohort.json").read_bytes()
        with patch.object(drip, "parked", return_value=[]), patch.object(
                drip, "candidates", return_value=[{"key": "ZIP-100"}, {"key": "ZIP-101"}]):
            report = cohort.reconcile(self.cfg, dry_run=True)
        self.assertEqual(report.admitted, ["ZIP-100"])
        self.assertIn("10/10", report.waits["admission"])
        self.assertEqual((self.cfg.state_root / "cohort.json").read_bytes(), before)

    def test_drip_at_cap_never_spins_up_or_removes_parked_marker(self):
        self.fill(10)
        tdir = self.cfg.ticket_dir("ZIP-100")
        tdir.mkdir()
        marker = tdir / "PARKED"
        marker.touch()
        with patch.object(drip, "parked", return_value=["ZIP-100"]), patch.object(
                drip, "shared_services_ready", return_value=True), patch.object(
                drip, "spinup_with_panes") as spin, patch.object(drip, "candidates") as board:
            self.assertEqual(drip._tick_locked(self.cfg, log=lambda _: None), [])
        self.assertTrue(marker.exists())
        spin.assert_not_called()
        board.assert_not_called()

    def test_legacy_drip_dry_run_uses_the_same_workspace_budget(self):
        self.fill(9)
        with patch.object(drip, "cohort_enabled", return_value=False), patch.object(
                drip, "in_flight", return_value=[]), patch.object(
                drip, "parked", return_value=[]), patch.object(drip, "spinup_retries", return_value=[]), patch.object(
                drip, "candidates", return_value=[
                    {"key": "ZIP-100", "summary": "a"}, {"key": "ZIP-101", "summary": "b"}]):
            self.assertEqual(drip._tick_locked(self.cfg, log=lambda _: None, dry_run=True), ["ZIP-100"])

    def test_invalid_workspace_caps_are_rejected(self):
        original = self.path.read_text()
        for value in ("0", "11", "true", '"10"'):
            self.path.write_text(original.replace("max_open = 10", f"max_open = {value}"))
            with self.subTest(value=value), self.assertRaises(config.ConfigError):
                config.load(self.path)


if __name__ == "__main__":
    unittest.main()
