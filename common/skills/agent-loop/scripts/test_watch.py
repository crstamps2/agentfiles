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


if __name__ == "__main__":
    unittest.main()
