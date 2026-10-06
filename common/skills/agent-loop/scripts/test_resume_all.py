import contextlib
import io
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import config
import runner
import state
import supervise


class ResumeAllTests(unittest.TestCase):
    """After a reboot or `supervise stop`, every in-flight ticket gets its supervisor back without
    any ticket state being rewritten; tickets that need Cody are reported, never restarted."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); root = pathlib.Path(self.tmp.name)
        toml = (pathlib.Path(__file__).resolve().parent.parent / "hopper.toml").read_text()
        toml = toml.replace('state_root = "~/.local/state/agent-loop"', f'state_root = "{root}/state"').replace('pi_agents_dir = "~/.pi/agent/agents"', f'pi_agents_dir = "{root}"')
        self.config_path = root / "hopper.toml"; self.config_path.write_text(toml)
        self.cfg = config.load(self.config_path); self.cfg.ensure_dirs()
        self.wts = root / "wts"
        self.running = set()
        self.start = patch.object(supervise, "start", side_effect=lambda key, wt, root: f"label-{key}").start()
        patch.object(supervise, "status", side_effect=lambda key: "state=running" if key in self.running else "not loaded").start()
        self.addCleanup(patch.stopall); self.addCleanup(self.tmp.cleanup)

    def ticket(self, key, ticket_state, worktree=True, reason=""):
        wt = self.wts / key.lower()
        if worktree:
            wt.mkdir(parents=True, exist_ok=True)
        state.save(self.cfg.ticket_dir(key), state.Ticket(key=key, state=ticket_state, worktree=str(wt), reason=reason))
        return wt

    def started_keys(self):
        return [c.args[0] for c in self.start.call_args_list]

    def test_restarts_in_flight_supervisors_without_changing_state(self):
        wt = self.ticket("ZIP-1", "implement")
        self.ticket("ZIP-2", "paused", reason="resource: heavy lane busy")
        result = runner.resume_all(self.cfg, log=lambda m: None)
        self.assertEqual(sorted(self.started_keys()), ["ZIP-1", "ZIP-2"])
        worktrees = {c.args[0]: c.args[1] for c in self.start.call_args_list}
        self.assertEqual(worktrees["ZIP-1"], str(wt))
        self.assertEqual(state.load(self.cfg.ticket_dir("ZIP-1")).state, "implement")
        self.assertEqual(state.load(self.cfg.ticket_dir("ZIP-2")).state, "paused")
        self.assertEqual(sorted(result["started"]), ["ZIP-1", "ZIP-2"])

    def test_leaves_a_running_supervisor_alone(self):
        self.ticket("ZIP-1", "implement"); self.running.add("ZIP-1")
        result = runner.resume_all(self.cfg, log=lambda m: None)
        self.assertEqual(self.started_keys(), [])
        self.assertEqual(result["running"], ["ZIP-1"])

    def test_human_takeover_and_blocked_tickets_are_reported_not_started(self):
        self.ticket("ZIP-1", "implement")
        (self.cfg.ticket_dir("ZIP-1") / "HUMAN").touch()
        self.ticket("ZIP-2", "blocked", reason="planner: owner question")
        result = runner.resume_all(self.cfg, log=lambda m: None)
        self.assertEqual(self.started_keys(), [])
        self.assertEqual(result["needs_operator"], ["ZIP-1", "ZIP-2"])

    def test_gated_done_queued_and_worktreeless_tickets_are_ignored(self):
        self.ticket("ZIP-1", "human-gate-1"); self.ticket("ZIP-2", "done"); self.ticket("ZIP-3", "queued")
        self.ticket("ZIP-4", "implement", worktree=False)
        result = runner.resume_all(self.cfg, log=lambda m: None)
        self.assertEqual(self.started_keys(), [])
        self.assertEqual(result, {"started": [], "running": [], "needs_operator": []})

    def test_resuming_one_ticket_resets_its_heal_budgets_and_operator_flag(self):
        import heal
        self.ticket("ZIP-1", "paused", reason="plan failed: x")
        tdir = self.cfg.ticket_dir("ZIP-1"); t = state.load(tdir); t.previous = "plan"; t.needs_operator = "budget spent"; state.save(tdir, t)
        for _ in range(3): heal.record(tdir, "replan", "plan failed: x", "plan")
        with contextlib.redirect_stdout(io.StringIO()):
            runner.main(["--config", str(self.config_path), "resume", "--ticket", "ZIP-1"])
        self.assertEqual(heal.decide(tdir, "plan failed: x")[0], "replan")
        self.assertEqual(state.load(tdir).needs_operator, "")

    def test_resume_retry_task_restarts_that_task_and_the_supervisor(self):
        wt = self.ticket("ZIP-1", "blocked", reason="task 003: ladder exhausted")
        with patch.object(runner, "retry_task") as retry, contextlib.redirect_stdout(io.StringIO()):
            runner.main(["--config", str(self.config_path), "resume", "--ticket", "ZIP-1", "--retry-task", "003"])
        retry.assert_called_once()
        self.assertEqual(retry.call_args.args[1:3], ("ZIP-1", "003"))
        self.assertEqual(self.started_keys(), ["ZIP-1"])

    def test_cli_accepts_all_and_rejects_ambiguous_forms(self):
        self.ticket("ZIP-1", "implement")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runner.main(["--config", str(self.config_path), "resume", "--all"]), 0)
        self.assertEqual(self.started_keys(), ["ZIP-1"])
        for argv in (["resume"], ["resume", "--all", "--ticket", "ZIP-1"], ["resume", "--all", "--from", "implement"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                runner.main(["--config", str(self.config_path), *argv])


if __name__ == "__main__":
    unittest.main()
