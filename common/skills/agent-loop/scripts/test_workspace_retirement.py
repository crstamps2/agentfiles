import importlib.util
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import locks
import state
import workspace_binding as wb


class RetirementTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("workspace_retirement"), "missing owner retirement")
        import workspace_retirement as retire
        self.retire = retire
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name).resolve()
        self.cfg = SimpleNamespace(state_root=root, ticket_dir=lambda k: root / "tickets" / k)
        self.wt = root / "zip-1"
        self.wt.mkdir()
        self.td = self.cfg.ticket_dir("ZIP-1")
        self.identity = "repo::" + str(self.wt)
        self.ticket = state.Ticket(key="ZIP-1", worktree=str(self.wt), branch="feature",
                                   state="paused", recovery={"active": {"id": "old"}, "queued": []})
        state.save(self.td, self.ticket)
        wb.write(self.td, wb.Binding("orca", "ZIP-1", worktree=str(self.wt), workspace_id=self.identity,
                                     repo_id="repo", branch="feature", phase="ready"))
        status = patch("workspace_retirement.supervisor_absent", return_value=True)
        status.start()
        self.addCleanup(status.stop)

    def plan(self):
        return self.retire.inspect(self.cfg, "ZIP-1", self.wt, self.identity, "feature")

    def test_quiescent_bound_or_recovery_workspace_can_retire_without_resuming(self):
        plan = self.plan()
        self.assertEqual(state.load(self.td).state, "paused")
        self.retire.commit(self.cfg, plan)
        t = state.load(self.td)
        self.assertEqual(t.state, "retired")
        self.assertFalse(t.worktree)
        self.assertFalse(t.recovery.get("active"))
        self.assertEqual(t.retired_workspace["recovery"]["active"]["id"], "old")
        self.assertIsNone(wb.read(self.td))
        self.assertTrue((self.td / "workspace.retired.json").exists())

    def test_settled_foreground_contract_is_archived_never_returned_to_autonomy(self):
        owner = self.td / "INTERACTIVE.json"
        previous = '{"mode":"interactive","ticket":"ZIP-1","session":"finished-session"}'
        owner.write_text(previous)
        plan = self.plan()
        self.assertEqual(owner.read_text(), previous)
        self.retire.commit(self.cfg, plan)
        self.assertEqual((self.td / "INTERACTIVE.retired.json").read_text(), previous)
        self.assertFalse(owner.exists())
        self.assertFalse((self.td / "PARKED").exists())
        self.assertEqual(state.load(self.td).state, "retired")

    def test_changed_plan_or_held_writer_never_retires(self):
        plan = self.plan()
        self.ticket.reason = "new decision"
        state.save(self.td, self.ticket)
        with self.assertRaises(ValueError):
            self.retire.commit(self.cfg, plan)
        lease = locks.Lease(self.cfg.state_root / "locks/runner-zip-1", "another writer")
        self.assertTrue(lease.acquire(hold=True))
        try:
            with self.assertRaises(ValueError):
                self.plan()
        finally:
            lease.release()
        self.assertEqual(state.load(self.td).state, "paused")
        self.assertIsNotNone(wb.read(self.td))

    def test_foreign_binding_and_unsettled_foreground_handoff_are_not_released(self):
        with self.assertRaises(ValueError):
            self.retire.inspect(self.cfg, "ZIP-1", self.wt, "other::" + str(self.wt), "feature")
        (self.td / "INTERACTIVE.json").write_text('{"mode":"yielding","ticket":"ZIP-1","session":"other"}')
        with self.assertRaisesRegex(ValueError, "foreground"):
            self.plan()
