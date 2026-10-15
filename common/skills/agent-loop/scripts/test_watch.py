import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import config
import state
import watch


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); root = pathlib.Path(self.tmp.name)
        toml = (pathlib.Path(__file__).resolve().parent.parent / "hopper.toml").read_text()
        toml = toml.replace('state_root = "~/.local/state/agent-loop"', f'state_root = "{root}/state"').replace('pi_agents_dir = "~/.pi/agent/agents"', f'pi_agents_dir = "{root}"')
        (root / "hopper.toml").write_text(toml); self.cfg = config.load(root / "hopper.toml"); self.cfg.ensure_dirs()
        t = state.Ticket(key="ZIP-1", state="implement"); state.save(self.cfg.ticket_dir("ZIP-1"), t)
        a = self.cfg.state_root / "attempts" / "ZIP-1" / "003" / "2"; a.mkdir(parents=True)
        (a / "attempt.json").write_text(json.dumps({"status": "RUNNING", "agent": "premium-worker", "started_utc": "2026-09-16T10:00:00Z",
                                                    "stages": [{"kind": "worker"}], "outcome": None, "reason": ""}))
        (self.cfg.ticket_dir("ZIP-1") / "ci.json").write_text(json.dumps({"pr": 4711, "actions": ["rebase"], "bot_rounds": 1}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_snapshot_and_line_carry_stage_task_agent_pr_and_reason(self):
        with patch.object(watch.supervise, "status", return_value="state=running, pid=1"), patch.object(watch, "pr_state", return_value="draft"):
            s = watch.snapshot(self.cfg, "ZIP-1")
        self.assertEqual(s["state"], "implement"); self.assertTrue(s["attempt"].startswith("task 003 #2 premium running [worker]"))
        self.assertEqual(s["pr"], 4711); self.assertEqual(s["ci_actions"], ["rebase"]); self.assertEqual(s["bot_rounds"], 1)
        line = watch.fmt("ZIP-1", s, 12.5)
        for frag in ("ZIP-1 [implement]", "task 003 #2 premium", "PR #4711 (draft)", "ci:rebase", "bot-round 1", "$12.50"):
            self.assertIn(frag, line)

    def test_a_ticket_waiting_on_the_operator_says_so(self):
        t = state.load(self.cfg.ticket_dir("ZIP-1")); t.state = "paused"; t.reason = "plan failed: critic verdict 'revise' after repair"
        t.needs_operator = "replan already tried 3x for this reason; budget 3 spent"; state.save(self.cfg.ticket_dir("ZIP-1"), t)
        with patch.object(watch.supervise, "status", return_value="-"), patch.object(watch, "pr_state", return_value="draft"):
            line = watch.fmt("ZIP-1", watch.snapshot(self.cfg, "ZIP-1"), 1.0)
        self.assertIn("NEEDS OPERATOR: replan already tried 3x", line)

    def test_paused_reason_is_shown_and_once_mode_returns(self):
        t = state.load(self.cfg.ticket_dir("ZIP-1")); t.state = "paused"; t.reason = "guard failed on the BASE tree too: x"; state.save(self.cfg.ticket_dir("ZIP-1"), t)
        lines = []
        with patch.object(watch.supervise, "status", return_value="-"), patch.object(watch, "spend", return_value=0.0), patch.object(watch, "pr_state", return_value="draft"), patch("builtins.print", side_effect=lambda *a, **k: lines.append(" ".join(map(str, a)))):
            rc = watch.run(self.cfg, "ZIP-1", once=True)
        self.assertEqual(rc, 0); self.assertTrue(any("↳ guard failed on the BASE tree" in l for l in lines))

    def test_gate_prints_ready_for_cody_with_pr_and_exits(self):
        t = state.load(self.cfg.ticket_dir("ZIP-1")); t.state = "human-gate-1"; state.save(self.cfg.ticket_dir("ZIP-1"), t)
        lines = []
        with patch.object(watch.supervise, "status", return_value="-"), patch.object(watch, "spend", return_value=0.0), patch.object(watch, "pr_state", return_value="ready"), patch("builtins.print", side_effect=lambda *a, **k: lines.append(" ".join(map(str, a)))):
            watch.run(self.cfg, "ZIP-1", once=False, interval=0)
        self.assertTrue(any("READY FOR CODY: https://github.com/retailzipline/zipline-app/pull/4711" in l for l in lines))


class CohortWatchTests(WatchTests):
    def cohort_member(self):
        import cohort
        import recovery
        cohort.initialize(self.cfg)
        cohort.reserve(self.cfg, "ZIP-0", "test")
        cohort.reserve(self.cfg, "ZIP-1", "test")
        cohort.activate(self.cfg, "ZIP-1")
        contract = recovery.new_contract("ZIP-1", {"state": "gates"}, {"kind": "resume-stage", "state": "gates"}, ("85564066c4d9",))
        recovery.activate(self.cfg, "ZIP-1", contract)

    def test_snapshot_and_line_show_slot_recovery_and_progress(self):
        self.cohort_member()
        with patch.object(watch.supervise, "status", return_value="state=running, pid=1"), patch.object(watch, "pr_state", return_value="draft"):
            s = watch.snapshot(self.cfg, "ZIP-1")
        self.assertEqual((s["slot"], s["admission_sequence"]), (2, 2))
        self.assertIn("resume-stage->gates", s["recovery"])
        self.assertIn("defect=85564066c4d9", s["recovery"])
        self.assertTrue(s["progress_utc"])
        line = watch.fmt("ZIP-1", s, 0.0)
        self.assertIn("slot 2 (#2)", line)
        self.assertIn("recovery waiting-repair resume-stage->gates", line)

    def test_open_decision_is_visible_with_age_and_notification_status(self):
        t = state.load(self.cfg.ticket_dir("ZIP-1")); t.state = "decision-required"
        t.decision_request = {"id": "decision-abc", "question": "Which public API?", "created_utc": "2026-10-15T00:00:00Z", "notified_utc": "2026-10-15T00:00:01Z"}
        state.save(self.cfg.ticket_dir("ZIP-1"), t)
        with patch.object(watch.supervise, "status", return_value="-"), patch.object(watch, "pr_state", return_value="draft"):
            line = watch.fmt("ZIP-1", watch.snapshot(self.cfg, "ZIP-1"), 0.0)
        self.assertIn("decision decision-abc", line)
        self.assertIn("notified", line)
        self.assertIn("Which public API?", line)

    def test_fleet_overview_has_cohort_waiting_and_review_sections(self):
        self.cohort_member()
        parked = self.cfg.ticket_dir("ZIP-9"); state.save(parked, state.Ticket(key="ZIP-9", state="implement"))
        (parked / "PARKED").touch(); (parked / "PARKED.json").write_text(json.dumps({"admission_sequence": 7}))
        gate = self.cfg.ticket_dir("ZIP-5"); state.save(gate, state.Ticket(key="ZIP-5", state="human-gate-1"))
        (gate / "ci.json").write_text(json.dumps({"pr": 48650}))
        text = watch.fleet(self.cfg, pr_lookup=lambda pr: "ready", sup_status=lambda key: "state=running")
        self.assertIn("COHORT 2/4 (hard ceiling 7)", text)
        self.assertIn("ZIP-0", text)
        self.assertIn("recovery waiting-repair resume-stage->gates defect=85564066c4d9", text)
        self.assertIn("WAITING ADMISSION\nZIP-9 #7", text)
        self.assertIn("next: ZIP-9", text)
        self.assertIn("REVIEW READY\nZIP-5  PR #48650 (ready)", text)

    def test_fleet_overview_reports_configuration_above_hard_ceiling(self):
        self.cohort_member()
        self.cfg.drip["max_in_flight"] = 9
        text = watch.fleet(self.cfg, pr_lookup=lambda pr: "ready", sup_status=lambda key: "-")
        self.assertIn("VIOLATION drip.max_in_flight=9 exceeds hard ceiling 7", text)


if __name__ == "__main__":
    unittest.main()
