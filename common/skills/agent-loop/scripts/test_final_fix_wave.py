"""Regression coverage for final runner integration fixes."""
import json, os, pathlib, subprocess, sys, tempfile, time, unittest
from unittest.mock import patch
import admission, contracts, metrics, procs, runner, state, worktree
from test_runner import RunnerHarness, TASKS


class ContractAndMetricsTests(unittest.TestCase):
    def test_manifest_rejects_bad_shapes_and_unsafe_paths(self):
        data = {"tasks": [{"id":"001", "slug":"s", "summary":"x", "allowed_files":["../x", 3],
                 "verification_commands":[""], "acceptance":["a"], "timeout_s": True, "invariants":[1]}]}
        text = "\n".join(contracts.validate_tasks(data))
        self.assertIn("every entry", text); self.assertIn("relative", text); self.assertIn("timeout_s", text)
        self.assertIn("table/dict", "\n".join(contracts.validate_tasks({"tasks": ["bad"]})))

    def test_metrics_skips_torn_final_line(self):
        with tempfile.TemporaryDirectory() as d:
            pathlib.Path(d, "metrics.jsonl").write_text('{"a": 1}\n{"a":')
            self.assertEqual(metrics.read_all(d), [{"a": 1}])

    def test_unknown_admission_signals_fail_closed(self):
        th = type("T", (), dict(compressor_pct_max=25, load_per_core_max=1, disk_free_gb_min=2, require_ac_power=False))()
        r = admission.Reading(None, None, 1, True, False, None, None)
        self.assertEqual(admission.decide(r, th).reasons, ["compressor: unknown", "load: unknown", "pressure: unknown", "disk: unknown"])
        self.assertEqual(admission.parse_memory_pressure("System-wide memory free percentage: 4%"), "critical")


class WorktreeFixTests(unittest.TestCase):
    def test_ignored_tracked_and_exact_path_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            wt = pathlib.Path(d); subprocess.run(["git", "init", "-q", "-b", "main", str(wt)], check=True)
            for k,v in (("user.email","t@t"),("user.name","t")): subprocess.run(["git", "-C", str(wt), "config", k, v], check=True)
            (wt / ".gitignore").write_text("bin/gate\n")
            (wt / "bin").mkdir(); (wt / "bin/gate").write_text("base")
            subprocess.run(["git", "-C", str(wt), "add", "-f", "."], check=True); subprocess.run(["git", "-C", str(wt), "commit", "-qm", "base"], check=True)
            base = worktree.snapshot(wt); (wt / "bin/gate").write_text("changed")
            (wt / "bin/x\ty").write_text("x"); (wt / "app").mkdir(); (wt / "app/a b.rb").write_text("x")
            changed = worktree.changed_paths(wt, base)
            self.assertIn("bin/gate", changed); self.assertIn("bin/x\ty", changed); self.assertIn("app/a b.rb", changed)
            self.assertTrue(worktree.check_allowlist(["bin/x\ty"], type("T", (), dict(id="001", allowed_files=["**"], may_edit_tests=True))(), ["bin/"], []))
            worktree.restore(wt, base); self.assertEqual((wt / "bin/gate").read_text(), "base")


class RunnerFinalFixTests(RunnerHarness):
    def test_violation_overrides_environment_and_restores(self):
        outcome, rows = self.run_task("env_escape", "pass")
        self.assertEqual(rows[0]["outcome"], "rejected"); self.assertFalse((self.wt / "bin/oops").exists()); self.assertEqual(outcome, "accepted")

    def test_verification_change_is_rejected_and_restored(self):
        p = self.cfg.state_root.parent / "verify.toml"
        p.write_text(TASKS.replace('verification_commands = ["true"]', 'verification_commands = ["mkdir -p bin && touch bin/oops"]'))
        self.scenarios=["pass", "pass"]; t=self._fresh_ticket(); r=runner.Runner(self.cfg, pi_launcher=self.launcher)
        r.implement_task(t, contracts.load_tasks(p)[0], 0, self.wt)
        self.assertEqual(metrics.read_all(self.cfg.state_root)[0]["outcome"], "rejected"); self.assertFalse((self.wt/"bin/oops").exists())

    def test_unverified_stage_fences_and_pauses(self):
        def bad(argv, cwd, timeout_s, env, out, err, on_start=None):
            if on_start: on_start(987654)
            return procs.StageResult(None, False, .1, 987654, False)
        t=self._fresh_ticket(); r=runner.Runner(self.cfg, pi_launcher=bad)
        self.assertEqual(r.implement_task(t, self.tasks[0], 0, self.wt), "paused")
        self.assertEqual(json.loads((self.cfg.state_root/"locks/heavy.fence").read_text())["pgid"], 987654)

    def test_paused_resume_and_blocked_do_not_dispatch(self):
        t=self._fresh_ticket(); t=state.transition(t, "paused", "test")
        r=runner.Runner(self.cfg, pi_launcher=self.launcher); self.scenarios=["pass"]
        self.assertEqual(r.implement_task(t, self.tasks[0], 0, self.wt), "accepted")
        t.state="blocked"; self.assertEqual(r.implement_task(t, self.tasks[0], 0, self.wt), "blocked")

    def test_history_fingerprint_relaunches_on_manifest_change(self):
        self.run_task("pass")
        altered = contracts.Task(**{**self.tasks[0].__dict__, "summary": "different"})
        t=state.load(self.cfg.ticket_dir("ZIP-7873")); t.state="implement"; self.scenarios=["pass"]
        self.assertEqual(runner.Runner(self.cfg, pi_launcher=self.launcher).implement_task(t, altered, 0, self.wt), "accepted")
        self.assertEqual(self.launches, 2)

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

    def test_recovery_marks_live_attempt_interrupted_and_uses_new_dir(self):
        t=self._fresh_ticket(); root=self.cfg.state_root/"attempts"/"ZIP-7873"/"001"/"1"; root.mkdir(parents=True)
        p=subprocess.Popen(["sleep", "60"], start_new_session=True); pgid=os.getpgid(p.pid)
        (root/"attempt.json").write_text(json.dumps({"status":"running","pgid":pgid}))
        self.scenarios=["pass"]
        self.assertEqual(runner.Runner(self.cfg, pi_launcher=self.launcher).implement_task(t, self.tasks[0], 0, self.wt), "accepted")
        self.assertEqual(json.loads((root/"attempt.json").read_text())["status"], "interrupted")
        self.assertTrue((root.parent/"2").exists())
        p.wait(timeout=2)


if __name__ == "__main__": unittest.main()
