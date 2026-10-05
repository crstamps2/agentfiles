import pathlib
import tempfile
import unittest
from unittest.mock import patch

import config
import drip
import state

BOARD = [
    {"key": "ZIP-4281", "summary": "Nav Tabs", "status": "Selected for Work", "assignee": None},
    {"key": "ZIP-4293", "summary": "Section", "status": "Selected for Work", "assignee": None},
    {"key": "ZIP-7872", "summary": "Nav Link", "status": "In Progress", "assignee": "cody@zipline.inc"},
    {"key": "ZIP-7874", "summary": "Dropzone", "status": "Selected for Work", "assignee": None},
    {"key": "ZIP-7875", "summary": "Stepper", "status": "In Progress", "assignee": "cody@zipline.inc"},
    {"key": "ZIP-8025", "summary": "Adopt forms", "status": "In Progress", "assignee": "martin@zipline.inc"},
    {"key": "ZIP-8079", "summary": "Migrate wells", "status": "Triage", "assignee": None},
    {"key": "ZIP-9001", "summary": "Someone else's ready ticket", "status": "Selected for Work", "assignee": "ann@zipline.inc"},
]


class DripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); root = pathlib.Path(self.tmp.name)
        toml = (pathlib.Path(__file__).resolve().parent.parent / "hopper.toml").read_text()
        toml = toml.replace('state_root = "~/.local/state/agent-loop"', f'state_root = "{root}/state"').replace('pi_agents_dir = "~/.pi/agent/agents"', f'pi_agents_dir = "{root}"')
        (root / "hopper.toml").write_text(toml); self.cfg = config.load(root / "hopper.toml"); self.cfg.ensure_dirs()
        self.wts = root / "wts"; (self.wts / "zip-7875").mkdir(parents=True)          # 7875 in flight; 7872 at the gate
        state.save(self.cfg.ticket_dir("ZIP-7875"), state.Ticket(key="ZIP-7875", state="implement"))
        state.save(self.cfg.ticket_dir("ZIP-7872"), state.Ticket(key="ZIP-7872", state="human-gate-1"))
        self.patches = [patch.object(drip, "WORKTREES", self.wts), patch.object(drip, "board", return_value=list(BOARD)),
                        patch.object(drip, "done_in_jira", return_value=set())]
        for p in self.patches: p.start()

    def tearDown(self):
        for p in self.patches: p.stop()
        self.tmp.cleanup()

    def test_candidates_follow_board_order_with_the_rules(self):
        keys = [r["key"] for r in drip.candidates(self.cfg, log=lambda m: None)]
        # 4281 dep on 7872 not Done; 4293 excluded; 7872 gated; 7875 in flight; 8025 In Progress (Martin); 8079 Triage
        self.assertEqual(keys, ["ZIP-7874", "ZIP-9001"])

    def test_others_ready_tickets_are_allowed_unless_configured(self):
        self.cfg.drip["skip_assigned_to_others"] = True
        self.assertEqual([r["key"] for r in drip.candidates(self.cfg, log=lambda m: None)], ["ZIP-7874"])

    def test_dep_becomes_eligible_once_done_in_jira(self):
        with patch.object(drip, "done_in_jira", return_value={"ZIP-7872"}):
            keys = [r["key"] for r in drip.candidates(self.cfg, log=lambda m: None)]
        self.assertEqual(keys[0], "ZIP-4281")                                        # top of the board once unblocked

    def test_tick_fills_free_slots_only(self):
        self.cfg.drip["max_in_flight"] = 2
        self.assertEqual(drip.tick(self.cfg, log=lambda m: None, dry_run=True), ["ZIP-7874"])   # 1 in flight -> 1 free
        self.cfg.drip["max_in_flight"] = 1
        self.assertEqual(drip.tick(self.cfg, log=lambda m: None, dry_run=True), [])

    def test_tick_waits_for_shared_services_before_spinup(self):
        msgs = []
        not_ready = drip.subprocess.CompletedProcess(["bin/dev-services"], 1, "", "elasticsearch not ready")
        import supervise
        with patch.object(drip.subprocess, "run", return_value=not_ready), \
             patch.object(drip, "spinup_with_panes", return_value=self.wts / "zip-7874") as spinup, \
             patch.object(supervise, "start") as start_supervisor:
            started = drip.tick(self.cfg, log=msgs.append)
        self.assertEqual(started, [])
        spinup.assert_not_called()
        start_supervisor.assert_not_called()
        self.assertTrue(any("shared services are not ready" in m for m in msgs), msgs)




class DripSerializationTests(unittest.TestCase):
    """Branch review 2026-09-23: selection -> Jira transition -> spinup ran with no lease, so a
    scheduled tick and an operator start could build two workspaces for one ticket."""

    def test_a_second_tick_skips_while_one_holds_the_lease(self):
        import tempfile, pathlib as _p, locks, config
        with tempfile.TemporaryDirectory() as d:
            root = _p.Path(d)
            toml = (_p.Path(__file__).resolve().parent.parent / "hopper.toml").read_text()
            toml = toml.replace('state_root = "~/.local/state/agent-loop"', f'state_root = "{root}/state"')
            (root / "hopper.toml").write_text(toml)
            cfg = config.load(root / "hopper.toml"); cfg.ensure_dirs()
            held = locks.Lease(_p.Path(cfg.state_root) / "locks" / "drip", "other-drip")
            self.assertTrue(held.acquire(hold=True))
            try:
                msgs = []
                started = drip.tick(cfg, log=msgs.append)
                self.assertEqual(started, [])
                self.assertTrue(any("lease" in m for m in msgs), msgs)
            finally:
                held.release()

    def test_selection_and_spinup_are_inside_the_lease(self):
        import inspect
        src = inspect.getsource(drip.tick)
        self.assertIn("locks.Lease", src)
        self.assertIn("_tick_locked", src)
        self.assertNotIn("in_flight(cfg)", src)      # the read happens under the lease, not before it


if __name__ == "__main__":
    unittest.main()