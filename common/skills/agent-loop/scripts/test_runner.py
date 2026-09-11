# test_runner.py
import json, os, pathlib, re, subprocess, sys, tempfile, threading, unittest
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
        # Pin the arm order the ladder tests assume (index 0 -> cloud). The production default is an
        # operator preference (local-first since 2026-09-10) and must not silently flip these assertions.
        toml = re.sub(r'alternate = \[[^\]]*\][^\n]*', 'alternate = ["cloud", "local"]', toml)
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

    def launcher(self, argv, cwd, timeout_s, env, stdout_path, stderr_path, on_start=None):
        self.launches += 1
        scenario = self.scenarios.pop(0) if self.scenarios else "pass"
        env = {**(env or os.environ), "AL_SCENARIO": scenario}
        fake_argv = [sys.executable, str(FAKE), *argv[2:]]
        return _real_run_stage(fake_argv, cwd, timeout_s, env, stdout_path, stderr_path, on_start=on_start)

    def run_task(self, *scenarios, tasks=None, ticket_key="ZIP-7873"):
        self.scenarios = list(scenarios)
        r = runner.Runner(self.cfg, run_id="test", pi_launcher=self.launcher)
        t = state.load(self.cfg.ticket_dir(ticket_key)); t.worktree = str(self.wt)
        for s in ("spinup", "plan", "plan-review", "implement"):
            t = state.transition(t, s)
        state.save(self.cfg.ticket_dir(ticket_key), t)
        task = (tasks or self.tasks)[0]
        return r.implement_task(t, task, 0, self.wt), metrics.read_all(self.cfg.state_root)

    def tasks_with(self, **overrides):
        """Write a TASKS variant with the given [[tasks]] fields overridden and load it."""
        root = self.cfg.state_root.parent
        text = TASKS
        for key, val in overrides.items():
            if isinstance(val, list):
                rendered = "[" + ", ".join(json.dumps(v) for v in val) + "]"
            elif isinstance(val, bool):
                rendered = "true" if val else "false"
            elif isinstance(val, (int, float)):
                rendered = str(val)
            else:
                rendered = json.dumps(val)
            pattern = re.compile(rf"^{re.escape(key)} = .*$", re.MULTILINE)
            if pattern.search(text):
                text = pattern.sub(f"{key} = {rendered}", text)
            else:
                text = text + f"\n{key} = {rendered}\n"
        path = root / ("tasks_with_" + "_".join(overrides) + ".toml")
        path.write_text(text)
        return contracts.load_tasks(path)

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
        t.attempts[runner.Runner(self.cfg)._history_key(self.tasks[0], self.wt)] = [
            {"rung": {"agent": "cloud-worker", "tier": "cheap", "n": 1}, "outcome": "rejected", "reason": "r1", "n": 1},
            {"rung": {"agent": "cloud-worker", "tier": "cheap", "n": 2}, "outcome": "rejected", "reason": "r2", "n": 2},
        ]
        root = self.cfg.state_root / "attempts" / "ZIP-7873" / "001"
        (root / "1").mkdir(parents=True); (root / "2").mkdir()
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
        t.attempts[runner.Runner(self.cfg)._history_key(self.tasks[0], self.wt)] = [
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

    def test_fake_worker_literal_allowlist_entry(self):
        root = self.cfg.state_root.parent
        variant = TASKS.replace('allowed_files = ["app/components/**", "test/**"]', 'allowed_files = ["app/components/exact.rb"]')
        (root / "tasks_literal.toml").write_text(variant)
        tasks = contracts.load_tasks(root / "tasks_literal.toml")
        r = runner.Runner(self.cfg, run_id="test", pi_launcher=self.launcher)
        self.scenarios = ["pass"]
        t = self._fresh_ticket(); state.save(self.cfg.ticket_dir("ZIP-7873"), t)
        outcome = r.implement_task(t, tasks[0], 0, self.wt)
        self.assertEqual(outcome, "accepted")
        self.assertTrue((self.wt / "app" / "components" / "exact.rb").exists())

    def test_verification_timeout_with_forbidden_change_is_rejected_and_restored(self):
        tasks = self.tasks_with(verification_commands=["mkdir -p bin && touch bin/oops && sleep 60"])
        outcome, rows = self.run_task("pass", tasks=tasks)
        self.assertEqual(rows[0]["outcome"], "rejected")
        self.assertIn("forbidden change", rows[0]["reason"])
        self.assertFalse((self.wt / "bin" / "oops").exists())

    def test_verification_timeout_with_clean_tree_is_environment_and_keeps_edit(self):
        tasks = self.tasks_with(verification_commands=["sleep 60"])
        outcome, rows = self.run_task("pass", "pass", tasks=tasks)   # env does not consume a rung; 2nd env pauses
        self.assertEqual(rows[0]["outcome"], "environment")
        self.assertIn("verification timeout", rows[0]["reason"])
        self.assertTrue((self.wt / "app" / "components" / "worker_touch.rb").exists())

    def test_recovery_with_unkillable_group_fences_and_pauses(self):
        # Simulate: attempt.json says running with a live pgid; kill_group reports failure.
        root = self.cfg.state_root / "attempts" / "ZIP-7873" / "001" / "1"; root.mkdir(parents=True)
        sleeper = subprocess.Popen(["sleep", "60"], start_new_session=True); pgid = os.getpgid(sleeper.pid)
        (root / "attempt.json").write_text(json.dumps({"status": "running", "pgid": pgid}))
        with patch("runner.procs.kill_group", return_value=False):
            outcome, rows = self.run_task("pass")
        self.assertEqual(outcome, "paused"); self.assertEqual(self.launches, 0)
        fence = json.loads((self.cfg.state_root / "locks" / "heavy.fence").read_text())
        self.assertEqual(fence["pgid"], pgid)
        t = state.load(self.cfg.ticket_dir("ZIP-7873")); self.assertIn("fenced", t.reason)
        sleeper.kill(); sleeper.wait()

    def test_recovery_kills_live_group_then_proceeds(self):
        root = self.cfg.state_root / "attempts" / "ZIP-7873" / "001" / "1"; root.mkdir(parents=True)
        sleeper = subprocess.Popen(["sleep", "60"], start_new_session=True); pgid = os.getpgid(sleeper.pid)
        threading.Thread(target=sleeper.wait, daemon=True).start()  # reap promptly, like a real supervisor would
        (root / "attempt.json").write_text(json.dumps({"status": "running", "pgid": pgid}))
        (root / "result.md").write_text("STATUS: pass\nREASON: none\n")      # stale claim must never be read
        outcome, rows = self.run_task("fail", "pass")
        self.assertFalse(procs.group_alive(pgid))
        self.assertEqual(json.loads((root / "attempt.json").read_text())["status"], "interrupted")
        self.assertEqual([r["outcome"] for r in rows if r["stage"] == "implement" and r.get("attempt") != 1],
                         ["rejected", "accepted"])            # stale pass was NOT consumed; real attempts ran
        self.assertEqual(rows[0]["outcome"], "interrupted")

    def test_verification_stage_writes_pgid_to_attempt_json(self):
        seen = {}
        real = procs.run_stage
        def spy(argv, cwd, timeout_s, env, out, err, on_start=None):
            if argv[:2] == ["/bin/sh", "-c"]:
                seen["on_start"] = on_start
            return real(argv, cwd, timeout_s, env, out, err, on_start=on_start)
        with patch("runner.procs.run_stage", side_effect=spy):
            self.run_task("pass")
        self.assertIsNotNone(seen.get("on_start"))
        adir = self.cfg.state_root / "attempts" / "ZIP-7873" / "001" / "1"
        aj = json.loads((adir / "attempt.json").read_text())
        self.assertEqual(aj["status"], "completed"); self.assertIn("verify_pgids", aj)

    def test_zombie_group_marks_orphaned_and_fences_once(self):
        # group_state == "zombie-or-foreign" (PermissionError probing killpg) must not be treated
        # as a plain survivor: the attempt is marked orphaned so a cleared fence doesn't re-fence.
        # Only fake the recovered-attempt's pgid; the real launch below must use the real
        # group_state/kill_group so its own process group is genuinely reaped.
        real_group_state, real_kill_group = procs.group_state, procs.kill_group
        root = self.cfg.state_root / "attempts" / "ZIP-7873" / "001" / "1"; root.mkdir(parents=True)
        pgid = 999999
        (root / "attempt.json").write_text(json.dumps({"status": "running", "pgid": pgid}))
        def fake_group_state(p, *a, **kw):
            return "zombie-or-foreign" if p == pgid else real_group_state(p, *a, **kw)
        def fake_kill_group(p, *a, **kw):
            return False if p == pgid else real_kill_group(p, *a, **kw)
        self.scenarios = ["pass"]
        r = runner.Runner(self.cfg, run_id="test", pi_launcher=self.launcher)
        t = self._fresh_ticket()
        with patch("runner.procs.group_state", side_effect=fake_group_state), \
             patch("runner.procs.kill_group", side_effect=fake_kill_group):
            outcome = r.implement_task(t, self.tasks[0], 0, self.wt)
        self.assertEqual(outcome, "paused"); self.assertEqual(self.launches, 0)
        fence_path = self.cfg.state_root / "locks" / "heavy.fence"
        fence = json.loads(fence_path.read_text())
        self.assertEqual(fence["pgid"], pgid)
        self.assertIn("zombie-or-foreign", fence["reason"])
        aj = json.loads((root / "attempt.json").read_text())
        self.assertEqual(aj["status"], "orphaned")

        # Operator clears the fence; re-entry must not re-fence the orphaned attempt forever.
        fence_path.unlink()
        t2 = state.load(self.cfg.ticket_dir("ZIP-7873"))
        with patch("runner.procs.group_state", side_effect=fake_group_state), \
             patch("runner.procs.kill_group", side_effect=fake_kill_group):
            outcome2 = r.implement_task(t2, self.tasks[0], 0, self.wt)
        self.assertEqual(outcome2, "accepted"); self.assertEqual(self.launches, 1)
        self.assertFalse(fence_path.exists())

    def test_clear_fence_cli_refuses_live_and_clears_dead(self):
        fence_path = self.cfg.state_root / "locks" / "heavy.fence"
        fence_path.parent.mkdir(parents=True, exist_ok=True)
        fence_data = {"pgid": 424242, "ticket": "ZIP-7873", "task": "001", "attempt": "1",
                      "reason": "recovery: group survived kill"}
        cfgfile = str(self.cfg.state_root.parent / "hopper.toml")

        fence_path.write_text(json.dumps(fence_data))
        with patch("runner.procs.group_state", return_value="alive"):
            rc = runner.main(["--config", cfgfile, "clear-fence"])
        self.assertEqual(rc, 1); self.assertTrue(fence_path.exists())

        with patch("runner.procs.group_state", return_value="dead"):
            rc = runner.main(["--config", cfgfile, "clear-fence"])
        self.assertEqual(rc, 0); self.assertFalse(fence_path.exists())

        fence_path.write_text(json.dumps(fence_data))
        with patch("runner.procs.group_state", return_value="alive"):
            rc = runner.main(["--config", cfgfile, "clear-fence", "--force"])
        self.assertEqual(rc, 0); self.assertFalse(fence_path.exists())

    def test_clear_fence_force_marks_running_attempt_orphaned(self):
        # clear-fence --force must mark the referenced attempt as orphaned before unlinking
        # the fence, so re-entry doesn't re-fence forever once the fence is cleared.
        import subprocess, signal
        root = self.cfg.state_root / "attempts" / "ZIP-7873" / "001" / "1"; root.mkdir(parents=True)
        
        # Start a live sleeper process to use as pgid, in a separate thread to allow reaping
        sleeper = subprocess.Popen(["sleep", "30"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        pgid = sleeper.pid
        # Start a reaper thread so the sleeper can be genuinely reaped
        threading.Thread(target=sleeper.wait, daemon=True).start()
        
        # Create attempt.json with status running
        (root / "attempt.json").write_text(json.dumps({"status": "running", "pgid": pgid}))
        
        # Create fence file
        fence_path = self.cfg.state_root / "locks" / "heavy.fence"
        fence_path.parent.mkdir(parents=True, exist_ok=True)
        fence_data = {"pgid": pgid, "ticket": "ZIP-7873", "task": "001", "attempt": "1",
                      "reason": "test fence"}
        fence_path.write_text(json.dumps(fence_data))
        
        # Run clear-fence --force
        cfgfile = str(self.cfg.state_root.parent / "hopper.toml")
        with patch("runner.procs.group_state", return_value="alive"):
            rc = runner.main(["--config", cfgfile, "clear-fence", "--force"])
        
        # Verify fence was cleared
        self.assertEqual(rc, 0); self.assertFalse(fence_path.exists())
        
        # Verify attempt.json was marked orphaned
        aj = json.loads((root / "attempt.json").read_text())
        self.assertEqual(aj["status"], "orphaned")
        self.assertEqual(aj["orphaned_by"], "clear-fence")
        
        # Now run a task; it should succeed without re-fencing
        # Verify orphaned attempt doesn't interfere with new launches
        self.scenarios = ["pass"]
        t = self._fresh_ticket()
        r = runner.Runner(self.cfg, run_id="test2", pi_launcher=self.launcher)
        outcome = r.implement_task(t, self.tasks[0], 0, self.wt)
        self.assertEqual(outcome, "accepted")
        self.assertEqual(self.launches, 1)
        self.assertFalse(fence_path.exists())  # no new fence created
        
        # Kill the sleeper
        try:
            os.kill(sleeper.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass

if __name__ == "__main__":
    unittest.main()
