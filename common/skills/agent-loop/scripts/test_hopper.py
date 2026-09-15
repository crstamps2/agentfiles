import pathlib
import tempfile
import unittest
from unittest.mock import patch

import config
import hopper
import lifecycle
import state


class HopperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); root = pathlib.Path(self.tmp.name)
        toml = (pathlib.Path(__file__).resolve().parent.parent / "hopper.toml").read_text()
        toml = toml.replace('state_root = "~/.local/state/agent-loop"', f'state_root = "{root}/state"').replace('pi_agents_dir = "~/.pi/agent/agents"', f'pi_agents_dir = "{root}"')
        (root / "hopper.toml").write_text(toml); self.cfg = config.load(root / "hopper.toml"); self.cfg.ensure_dirs()
        self.wts = root / "wts"; self.wts.mkdir()
        self.spun = []; self.runs = []; self.stopped = []
        self.patches = [
            patch.object(hopper, "WORKTREES", self.wts),
            patch.object(hopper, "spinup", side_effect=self.fake_spinup),
            patch.object(hopper, "stop_dev_server", side_effect=lambda wt, log: self.stopped.append(wt.name)),
            patch.object(lifecycle, "run_once", side_effect=self.fake_run_once),
            patch.object(hopper.subprocess, "Popen", side_effect=lambda *a, **k: type("P", (), {"terminate": lambda self: None})()),
            patch.object(hopper.subprocess, "run", return_value=type("R", (), {"stdout": "internal/x", "returncode": 0})()),
            patch.object(hopper.time, "sleep", lambda s: None),
        ]
        for p in self.patches: p.start()
        # ZIP-7873 is already at the gate; ZIP-4281/4282 depend on ZIP-7872
        t = state.Ticket(key="ZIP-7873", state="human-gate-1"); state.save(self.cfg.ticket_dir("ZIP-7873"), t)

    def tearDown(self):
        for p in self.patches: p.stop()
        self.tmp.cleanup()

    def fake_spinup(self, key, log):
        self.spun.append(key); wt = self.wts / key.lower(); (wt / "bin").mkdir(parents=True); return wt

    def fake_run_once(self, cfg, runner, ctx, key, wt, hopper_index, max_steps):
        self.runs.append(key); tdir = cfg.ticket_dir(key); t = state.load(tdir)
        # first visit: pretend we got to "ready" and are waiting on CI; second visit: reach the gate
        if t.state != "ready":
            t.state = "ready"; state.save(tdir, t); return [lifecycle.Step(key, "ready", "CI running", wait=True)]
        t.state = "human-gate-1"; state.save(tdir, t); return [lifecycle.Step(key, "bot-loop", "done", wait=True)]

    def test_spins_up_at_most_max_new_and_round_robins_to_the_gate(self):
        rep = hopper.run_hopper(self.cfg, None, None, max_new_tickets=2, max_hours=1, log=lambda m: None)
        self.assertEqual(self.spun, ["ZIP-7872", "ZIP-7877"])                 # hopper order, 7873 skipped (at gate)
        self.assertEqual(sorted(self.stopped), ["zip-7872", "zip-7877"])       # dev servers stopped at the gate
        self.assertEqual(rep["tickets"]["ZIP-7872"]["state"], "human-gate-1")
        self.assertNotIn("ZIP-4281", rep["tickets"])                           # dep ZIP-7872 not done -> ineligible

    def test_dependency_requires_done_not_gate(self):
        t = state.Ticket(key="ZIP-7872", state="human-gate-1"); state.save(self.cfg.ticket_dir("ZIP-7872"), t)
        self.assertEqual(hopper.eligible(self.cfg, "ZIP-4281"), (False, "dep ZIP-7872 is human-gate-1"))
        t.state = "done"; state.save(self.cfg.ticket_dir("ZIP-7872"), t)
        self.assertEqual(hopper.eligible(self.cfg, "ZIP-4281"), (True, ""))

    def test_pause_file_stops_the_hopper(self):
        (self.cfg.state_root / "PAUSE").touch()
        hopper.run_hopper(self.cfg, None, None, max_new_tickets=2, max_hours=1, log=lambda m: None)
        self.assertEqual(self.spun, [])


if __name__ == "__main__":
    unittest.main()
