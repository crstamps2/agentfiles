# test_runner.py
import json, os, pathlib, subprocess, sys, tempfile, unittest
from unittest.mock import patch
import config, contracts, metrics, procs, runner, state, worktree

HERE = pathlib.Path(__file__).resolve().parent
FAKE = HERE / "fake_worker.py"
_real_run_stage = procs.run_stage
TASKS = """
[[tasks]]
id = "001"
slug = "touch"
summary = "touch a file"
allowed_files = ["app/components/**", "test/**"]
verification_commands = ["true"]
acceptance = ["AC-1"]
"""
AGENT = """---
name: {name}
description: d
model: {model}
thinking: low
tools: read, grep, find, ls, bash, edit, write
---
body
"""

def git(wt, *a):
    return subprocess.run(["git", "-C", str(wt), *a], capture_output=True, text=True, check=True).stdout

class RunnerHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); root = pathlib.Path(self.tmp.name)
        self.wt = root / "wt"; self.wt.mkdir()
        git(self.wt, "init", "-q", "-b", "main"); git(self.wt, "config", "user.email", "t@t"); git(self.wt, "config", "user.name", "t")
        (self.wt / "app" / "components").mkdir(parents=True); (self.wt / "app" / "components" / ".keep").write_text("")
        git(self.wt, "add", "-A"); git(self.wt, "commit", "-qm", "init")
        agents = root / "agents"; agents.mkdir()
        for n, m in (("cloud-worker", "ollama-cloud/x"), ("local-worker", "ollama-local/x"), ("premium-worker", "openai-codex/x")):
            (agents / f"{n}.md").write_text(AGENT.format(name=n, model=m))
        toml = (HERE.parent / "hopper.toml").read_text()
        toml = toml.replace('state_root = "~/.local/state/agent-loop"', f'state_root = "{root}/state"')
        toml = toml.replace('pi_agents_dir = "~/.pi/agent/agents"', f'pi_agents_dir = "{agents}"')
        toml = toml.replace("heavy_stage_s = 4500", "heavy_stage_s = 3")
        (root / "hopper.toml").write_text(toml)
        self.cfg = config.load(root / "hopper.toml"); self.cfg.ensure_dirs()
        (root / "tasks.toml").write_text(TASKS)
        self.tasks = contracts.load_tasks(root / "tasks.toml")
        self.scenarios = []      # consumed in order, one per launch
        self.launches = 0
        # Admission always green in tests.
        self.adm = patch("runner.admission.probe", return_value=None); self.adm.start()
        self.dec = patch("runner.admission.decide", return_value=runner.admission.Decision(True, [])); self.dec.start()
    def tearDown(self):
        self.adm.stop(); self.dec.stop(); self.tmp.cleanup()

    def launcher(self, argv, cwd, timeout_s, env, stdout_path, stderr_path):
        self.launches += 1
        scenario = self.scenarios.pop(0) if self.scenarios else "pass"
        env = {**(env or os.environ), "AL_SCENARIO": scenario}
        fake_argv = [sys.executable, str(FAKE), *argv[2:]]
        return _real_run_stage(fake_argv, cwd, timeout_s, env, stdout_path, stderr_path)

    def run_task(self, *scenarios, ticket_key="ZIP-7873"):
        self.scenarios = list(scenarios)
        r = runner.Runner(self.cfg, run_id="test", pi_launcher=self.launcher)
        t = state.load(self.cfg.ticket_dir(ticket_key)); t.worktree = str(self.wt)
        for s in ("spinup", "plan", "plan-review", "implement"):
            t = state.transition(t, s)
        state.save(self.cfg.ticket_dir(ticket_key), t)
        return r.implement_task(t, self.tasks[0], 0, self.wt), metrics.read_all(self.cfg.state_root)

    def test_pass_first_attempt_is_accepted(self):
        outcome, rows = self.run_task("pass")
        self.assertEqual(outcome, "accepted"); self.assertEqual(self.launches, 1)
        self.assertEqual(rows[-1]["outcome"], "accepted"); self.assertEqual(rows[-1]["agent"], "cloud-worker")
        self.assertTrue((self.wt / "app" / "components" / "worker_touch.rb").exists())

    def test_fail_then_pass_with_feedback(self):
        outcome, rows = self.run_task("pass_on_feedback", "pass_on_feedback")
        self.assertEqual(outcome, "accepted"); self.assertEqual(self.launches, 2)
        self.assertEqual([r["outcome"] for r in rows], ["rejected", "accepted"])
        self.assertEqual([r["attempt"] for r in rows], [1, 2])
        fb = list(self.cfg.state_root.glob("attempts/**/feedback.md"))
        self.assertTrue(fb and "Attempt 1" in fb[0].read_text())

    def test_ladder_exhaustion_blocks_and_preserves_diffs(self):
        outcome, rows = self.run_task("fail", "fail", "fail")
        self.assertEqual(outcome, "blocked"); self.assertEqual(self.launches, 3)
        self.assertEqual([r["agent"] for r in rows], ["cloud-worker", "cloud-worker", "premium-worker"])
        self.assertEqual([r["tier"] for r in rows], ["cheap", "cheap", "premium"])
        self.assertEqual(git(self.wt, "status", "--porcelain").strip(), "")       # tree restored
        self.assertEqual(len(list(self.cfg.state_root.glob("attempts/ZIP-7873/001/*/diff.patch"))), 3)

    def test_malformed_result_is_protocol_and_advances(self):
        outcome, rows = self.run_task("malformed", "pass")
        self.assertEqual(outcome, "accepted"); self.assertEqual(rows[0]["outcome"], "protocol")

    def test_out_of_allowlist_edit_is_rejected_and_restored(self):
        outcome, rows = self.run_task("escape", "pass")
        self.assertEqual(rows[0]["outcome"], "rejected"); self.assertIn("protected", rows[0]["reason"])
        self.assertFalse((self.wt / "bin" / "oops").exists())
        self.assertEqual(outcome, "accepted")

    def test_test_edit_without_permission_is_rejected(self):
        outcome, rows = self.run_task("tests", "pass")
        self.assertEqual(rows[0]["outcome"], "rejected"); self.assertIn("may_edit_tests", rows[0]["reason"])
        self.assertFalse((self.wt / "test" / "x_test.rb").exists())

    def test_timeout_kills_and_advances_keeping_clean_tree(self):
        outcome, rows = self.run_task("timeout", "pass")
        self.assertEqual(rows[0]["outcome"], "timeout"); self.assertEqual(outcome, "accepted")

    def test_owner_block_returns_blocked_without_consuming_ladder(self):
        outcome, rows = self.run_task("owner")
        self.assertEqual(outcome, "blocked"); self.assertEqual(self.launches, 1)
        self.assertEqual(rows[0]["outcome"], "blocked"); self.assertEqual(rows[0]["reason"], "owner")

    def test_environment_does_not_consume_rung_then_pauses(self):
        outcome, rows = self.run_task("env", "env")
        self.assertEqual(outcome, "paused"); self.assertEqual(self.launches, 2)
        self.assertEqual([r["outcome"] for r in rows], ["environment", "environment"])

    def test_pause_file_stops_before_launch(self):
        (self.cfg.state_root / "PAUSE").touch()
        outcome, rows = self.run_task("pass")
        self.assertEqual(outcome, "paused"); self.assertEqual(self.launches, 0)

    def test_human_file_stops_before_launch(self):
        (self.cfg.ticket_dir("ZIP-7873")).mkdir(parents=True, exist_ok=True); (self.cfg.ticket_dir("ZIP-7873") / "HUMAN").touch()
        outcome, rows = self.run_task("pass")
        self.assertEqual(outcome, "paused"); self.assertEqual(self.launches, 0)

    def test_heavy_lane_held_by_live_owner_pauses(self):
        import locks
        held = locks.Lease(self.cfg.state_root / "locks" / "heavy", "other"); self.assertTrue(held.acquire())
        outcome, rows = self.run_task("pass")
        self.assertEqual(outcome, "paused"); self.assertEqual(self.launches, 0)

    def test_admission_red_pauses_with_reason(self):
        self.dec.stop()
        with patch("runner.admission.decide", return_value=runner.admission.Decision(False, ["on battery power"])):
            outcome, rows = self.run_task("pass")
        self.dec.start()
        self.assertEqual(outcome, "paused"); self.assertEqual(self.launches, 0)
        t = state.load(self.cfg.ticket_dir("ZIP-7873")); self.assertIn("battery", t.reason)

    def test_local_arm_assigned_for_odd_index(self):
        self.scenarios = ["pass"]
        r = runner.Runner(self.cfg, run_id="test", pi_launcher=self.launcher)
        t = state.load(self.cfg.ticket_dir("ZIP-7873")); t.worktree = str(self.wt)
        for s in ("spinup", "plan", "plan-review", "implement"): t = state.transition(t, s)
        r.implement_task(t, self.tasks[0], 1, self.wt)
        self.assertEqual(metrics.read_all(self.cfg.state_root)[-1]["agent"], "local-worker")

    def test_dry_run_cli(self):
        with patch("runner.procs.run_stage", side_effect=self.launcher):
            rc = runner.main(["--config", str(self.cfg.state_root.parent / "hopper.toml"), "dry-run",
                              "--worktree", str(self.wt), "--tasks", str(self.cfg.state_root.parent / "tasks.toml"), "--scenario", "pass"])
        self.assertEqual(rc, 0)
        self.assertEqual(metrics.read_all(self.cfg.state_root)[-1]["outcome"], "accepted")

    def test_status_cli_prints_ticket_states(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = runner.main(["--config", str(self.cfg.state_root.parent / "hopper.toml"), "status"])
        self.assertEqual(rc, 0); self.assertIn("ZIP-7873", buf.getvalue())

    def test_second_runner_instance_exits_3(self):
        import locks
        other = locks.Lease(self.cfg.state_root / "locks" / "runner", "runner"); self.assertTrue(other.acquire())
        rc = runner.main(["--config", str(self.cfg.state_root.parent / "hopper.toml"), "dry-run",
                          "--worktree", str(self.wt), "--tasks", str(self.cfg.state_root.parent / "tasks.toml")])
        self.assertEqual(rc, 3)

    # ----- Fix round 1: six controller-ruled fixes -----------------------------

    def _fresh_ticket(self, key="ZIP-7873"):
        t = state.load(self.cfg.ticket_dir(key)); t.worktree = str(self.wt)
        for s in ("spinup", "plan", "plan-review", "implement"):
            t = state.transition(t, s)
        return t

    def test_pass_claim_with_failing_verification_is_rejected(self):
        root = self.cfg.state_root.parent
        bad = TASKS.replace('verification_commands = ["true"]', 'verification_commands = ["false"]')
        (root / "tasks_bad_verify.toml").write_text(bad)
        tasks = contracts.load_tasks(root / "tasks_bad_verify.toml")
        r = runner.Runner(self.cfg, run_id="test", pi_launcher=self.launcher)
        self.scenarios = ["pass"]
        t = self._fresh_ticket(); state.save(self.cfg.ticket_dir("ZIP-7873"), t)
        r.implement_task(t, tasks[0], 0, self.wt)
        rows = metrics.read_all(self.cfg.state_root)
        self.assertEqual(rows[0]["outcome"], "rejected")
        self.assertIn("verification failed", rows[0]["reason"])

    def test_task_md_points_result_at_attempt_dir(self):
        outcome, rows = self.run_task("pass")
        adir = self.cfg.state_root / "attempts" / "ZIP-7873" / "001" / "1"
        md = (adir / "task.md").read_text()
        self.assertIn(str(adir / "result.md"), md)

    def test_attempt_numbers_continue_from_persisted_history(self):
        t = self._fresh_ticket()
        t.attempts["001"] = [
            {"rung": {"agent": "cloud-worker", "tier": "cheap", "n": 1}, "outcome": "rejected", "reason": "r1", "n": 1},
            {"rung": {"agent": "cloud-worker", "tier": "cheap", "n": 2}, "outcome": "rejected", "reason": "r2", "n": 2},
        ]
        state.save(self.cfg.ticket_dir("ZIP-7873"), t)
        self.scenarios = ["pass"]
        r = runner.Runner(self.cfg, run_id="test", pi_launcher=self.launcher)
        r.implement_task(t, self.tasks[0], 0, self.wt)
        rows = metrics.read_all(self.cfg.state_root)
        self.assertEqual(rows[-1]["attempt"], 3)
        self.assertTrue((self.cfg.state_root / "attempts" / "ZIP-7873" / "001" / "3").exists())

    def test_env_then_reject_then_env_does_not_pause(self):
        outcome, rows = self.run_task("env", "fail", "env", "pass")
        self.assertEqual(outcome, "accepted"); self.assertEqual(self.launches, 4)

    def test_env_count_survives_restart(self):
        t = self._fresh_ticket()
        t.attempts["001"] = [
            {"rung": {"agent": "cloud-worker", "tier": "cheap", "n": 1}, "outcome": "environment", "reason": "e1", "n": 1},
        ]
        state.save(self.cfg.ticket_dir("ZIP-7873"), t)
        self.scenarios = ["env"]
        r = runner.Runner(self.cfg, run_id="test", pi_launcher=self.launcher)
        outcome = r.implement_task(t, self.tasks[0], 0, self.wt)
        self.assertEqual(outcome, "paused"); self.assertEqual(self.launches, 1)

    def test_environment_keeps_clean_partial_edits(self):
        outcome, rows = self.run_task("env_partial", "env_partial")
        self.assertEqual(outcome, "paused")
        self.assertTrue((self.wt / "app" / "components" / "worker_touch.rb").exists())

    def test_fake_worker_honors_glob_tail(self):
        root = self.cfg.state_root.parent
        variant = TASKS.replace('allowed_files = ["app/components/**", "test/**"]', 'allowed_files = ["docs/**/*.md"]')
        (root / "tasks_docs.toml").write_text(variant)
        tasks = contracts.load_tasks(root / "tasks_docs.toml")
        r = runner.Runner(self.cfg, run_id="test", pi_launcher=self.launcher)
        self.scenarios = ["pass"]
        t = self._fresh_ticket(); state.save(self.cfg.ticket_dir("ZIP-7873"), t)
        outcome = r.implement_task(t, tasks[0], 0, self.wt)
        self.assertEqual(outcome, "accepted")

if __name__ == "__main__":
    unittest.main()
