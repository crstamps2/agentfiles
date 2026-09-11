# test_runner.py
import json, os, pathlib, re, subprocess, sys, tempfile, unittest
from unittest.mock import patch
import config, contracts, metrics, procs, reconcile, runner, state, worktree

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


def worker_rows(state_root):
    """Metrics now writes one row per executed stage (worker, verify-0, ...), all stamped
    with the same attempt-level outcome/reason/agent/tier/arm -- filter to the worker row
    to recover the old one-row-per-attempt view used by most assertions here."""
    return [r for r in metrics.read_all(state_root) if r.get("stage_kind") == "worker"]

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
        self.extra_env = {}
        # Admission always green in tests.
        self.adm = patch("runner.admission.probe", return_value=None); self.adm.start()
        self.dec = patch("runner.admission.decide", return_value=runner.admission.Decision(True, [])); self.dec.start()
    def tearDown(self):
        self.adm.stop(); self.dec.stop(); self.tmp.cleanup()

    def launcher(self, argv, cwd, timeout_s, env, stdout_path, stderr_path, on_start=None):
        self.launches += 1
        scenario = self.scenarios.pop(0) if self.scenarios else "pass"
        env = {**(env or os.environ), "AL_SCENARIO": scenario, **self.extra_env}
        fake_argv = [sys.executable, str(FAKE), *argv[2:]]
        return _real_run_stage(fake_argv, cwd, timeout_s, env, stdout_path, stderr_path, on_start=on_start)

    def _implement(self, r, t, task, idx, wt=None):
        """implement_task now requires a RunContext from reconcile.reconcile(); obtain one,
        release it (and its global runner lease) in a finally, exactly as the real CLI does."""
        ctx = reconcile.reconcile(self.cfg, "test")
        try:
            return r.implement_task(ctx, t, task, idx, wt if wt is not None else self.wt)
        finally:
            ctx.close()

    def run_task(self, *scenarios, tasks=None, ticket_key="ZIP-7873"):
        self.scenarios = list(scenarios)
        r = runner.Runner(self.cfg, run_id="test", pi_launcher=self.launcher)
        t = state.load(self.cfg.ticket_dir(ticket_key)); t.worktree = str(self.wt)
        for s in ("spinup", "plan", "plan-review", "implement"):
            t = state.transition(t, s)
        state.save(self.cfg.ticket_dir(ticket_key), t)
        task = (tasks or self.tasks)[0]
        outcome = self._implement(r, t, task, 0)
        return outcome, metrics.read_all(self.cfg.state_root)

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
        # CHANGED: metrics now writes one row per stage (worker, verify-0, ...) per the
        # design's "one row per executed stage"; the accepted attempt also runs its one
        # verification command, so the raw metrics list has 3 rows, not 2. Filter to the
        # worker-stage row to recover the old one-row-per-attempt sequence.
        wr = worker_rows(self.cfg.state_root)
        self.assertEqual([r["outcome"] for r in wr], ["rejected", "accepted"])
        self.assertEqual([r["attempt"] for r in wr], [1, 2])
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

    def test_timeout_keeps_partial_edit_and_advances(self):
        outcome, rows = self.run_task("timeout", "fail", "fail")
        self.assertEqual(rows[0]["outcome"], "timeout"); self.assertEqual(outcome, "blocked")
        # CHANGED: changed_paths now lives on attempt.json (attempt.Record), not on the
        # metrics row -- metrics rows are per-stage, not per-attempt (see the design's
        # "one row per executed stage").
        aj = json.loads((self.cfg.state_root / "attempts" / "ZIP-7873" / "001" / "1" / "attempt.json").read_text())
        self.assertIn("app/components/worker_touch.rb", aj["changed_paths"])
        diff = (self.cfg.state_root / "attempts" / "ZIP-7873" / "001" / "1" / "diff.patch").read_text()
        self.assertIn("edited by fake worker (timeout)", diff)

    def test_no_result_with_429_stderr_is_environment(self):
        # scenario `transport_429`: fake writes "HTTP 429 Too Many Requests" to stderr, no result.md, exits 1
        outcome, rows = self.run_task("transport_429", "transport_429")
        self.assertEqual([r["outcome"] for r in rows], ["environment", "environment"]); self.assertEqual(outcome, "paused")

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
        self._implement(r, t, self.tasks[0], 1)
        self.assertEqual(metrics.read_all(self.cfg.state_root)[-1]["agent"], "local-worker")

    def test_dry_run_cli(self):
        with patch("runner.procs.run_stage", side_effect=self.launcher):
            rc = runner.main(["--config", str(self.cfg.state_root.parent / "hopper.toml"), "dry-run",
                              "--worktree", str(self.wt), "--tasks", str(self.cfg.state_root.parent / "tasks.toml"), "--scenario", "pass"])
        self.assertEqual(rc, 0)
        self.assertEqual(metrics.read_all(self.cfg.state_root)[-1]["outcome"], "accepted")

    def test_paused_two_task_cli_resume_preserves_first_task_history(self):
        tasks_toml = self.cfg.state_root.parent / "tasks2.toml"
        tasks_toml.write_text(TASKS + TASKS.replace('id = "001"', 'id = "002"').replace("touch", "touch2"))
        t = state.load(self.cfg.ticket_dir("DRY-1")); t.worktree = str(self.wt)
        for s in ("spinup", "plan", "plan-review", "implement", "paused"): t = state.transition(t, s)
        state.save(self.cfg.ticket_dir("DRY-1"), t)
        with patch("runner.procs.run_stage", side_effect=self.launcher):
            self.scenarios = ["pass", "pass"]
            rc = runner.main(["--config", str(self.cfg.state_root.parent / "hopper.toml"), "dry-run",
                              "--worktree", str(self.wt), "--tasks", str(tasks_toml)])
        self.assertEqual(rc, 0)
        saved = state.load(self.cfg.ticket_dir("DRY-1"))
        # CHANGED: history is now keyed by `lineage` ("<ticket>/<task.id>"), not by a
        # manifest-fingerprinted "<task.id>@<fingerprint>" key (Plan 1c: the fingerprint is
        # informational only -- see `generation` -- and no longer keys the ladder).
        self.assertEqual(len([k for k in saved.attempts if k.endswith("/001")]), 1)
        self.assertEqual(len([k for k in saved.attempts if k.endswith("/002")]), 1)

    def test_status_cli_prints_ticket_states(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = runner.main(["--config", str(self.cfg.state_root.parent / "hopper.toml"), "status"])
        self.assertEqual(rc, 0); self.assertIn("ZIP-7873", buf.getvalue())

    def test_second_runner_instance_exits_3(self):
        # CHANGED: the concurrency guard now lives entirely in reconcile.reconcile(), which
        # takes the "runner" lease with `hold=True` (a held flock, not the old PID+boot_id
        # owner-file check) -- see test_reconcile.py's
        # test_reconcile_exits_3_if_runner_lease_held for the same contract.
        import locks
        other = locks.Lease(self.cfg.state_root / "locks" / "runner", "runner")
        self.assertTrue(other.acquire(hold=True))
        try:
            rc = runner.main(["--config", str(self.cfg.state_root.parent / "hopper.toml"), "dry-run",
                              "--worktree", str(self.wt), "--tasks", str(self.cfg.state_root.parent / "tasks.toml")])
            self.assertEqual(rc, 3)
        finally:
            other.release()

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
        self._implement(r, t, tasks[0], 0)
        rows = metrics.read_all(self.cfg.state_root)
        self.assertEqual(rows[0]["outcome"], "rejected")
        self.assertIn("verification failed", rows[0]["reason"])

    def test_task_md_points_result_at_attempt_dir(self):
        outcome, rows = self.run_task("pass")
        adir = self.cfg.state_root / "attempts" / "ZIP-7873" / "001" / "1"
        md = (adir / "task.md").read_text()
        self.assertIn(str(adir / "result.md"), md)

    def test_attempt_numbers_continue_from_persisted_history(self):
        # CHANGED: history is keyed by `lineage` ("<ticket>/<task.id>"), not by
        # Runner._history_key(task, wt) -- that method no longer exists (Plan 1c: the
        # manifest/worktree fingerprint is informational-only `generation`, not a history key).
        # Also CHANGED: the two prior attempts are now real, on-disk attempt.Records (via the
        # normal run path) rather than bare pre-made directories -- implement_task's required
        # RunContext runs reconcile.reconcile()'s global sweep first, which fails closed on any
        # attempt leaf directory without a readable attempt.json, so a hand-planted bare
        # directory can no longer coexist with a real run.
        outcome, rows = self.run_task("fail", "fail", "pass")
        self.assertEqual(outcome, "accepted")
        self.assertTrue((self.cfg.state_root / "attempts" / "ZIP-7873" / "001" / "3").exists())
        self.assertEqual(rows[-1]["attempt"], 3)

    def test_env_then_reject_then_env_does_not_pause(self):
        outcome, rows = self.run_task("env", "fail", "env", "pass")
        self.assertEqual(outcome, "accepted"); self.assertEqual(self.launches, 4)

    def test_env_count_survives_restart(self):
        t = self._fresh_ticket()
        lineage = f"ZIP-7873/{self.tasks[0].id}"
        t.attempts[lineage] = [
            {"rung": {"agent": "cloud-worker", "tier": "cheap", "n": 1}, "outcome": "environment", "reason": "e1", "n": 1},
        ]
        state.save(self.cfg.ticket_dir("ZIP-7873"), t)
        self.scenarios = ["env"]
        r = runner.Runner(self.cfg, run_id="test", pi_launcher=self.launcher)
        outcome = self._implement(r, t, self.tasks[0], 0)
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
        outcome = self._implement(r, t, tasks[0], 0)
        self.assertEqual(outcome, "accepted")

    def test_fake_worker_literal_allowlist_entry(self):
        root = self.cfg.state_root.parent
        variant = TASKS.replace('allowed_files = ["app/components/**", "test/**"]', 'allowed_files = ["app/components/exact.rb"]')
        (root / "tasks_literal.toml").write_text(variant)
        tasks = contracts.load_tasks(root / "tasks_literal.toml")
        r = runner.Runner(self.cfg, run_id="test", pi_launcher=self.launcher)
        self.scenarios = ["pass"]
        t = self._fresh_ticket(); state.save(self.cfg.ticket_dir("ZIP-7873"), t)
        outcome = self._implement(r, t, tasks[0], 0)
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

    def test_worker_planted_verify_log_symlink_does_not_clobber_target(self):
        outside = self.cfg.state_root.parent / "outside.txt"; outside.write_text("keep")
        self.extra_env = {"AL_OUTSIDE": str(outside)}
        outcome, rows = self.run_task("plant_verify_symlink")
        self.assertEqual(outside.read_text(), "keep")
        self.assertEqual(rows[0]["outcome"], "protocol")
        self.assertIn("pre-created verification artifact", rows[0]["reason"])

    # ----- Deleted: test_recovery_with_unkillable_group_fences_and_pauses,
    # test_recovery_kills_live_group_then_proceeds, test_zombie_group_marks_orphaned_and_fences_once.
    # Reason: recovery of a live/dead/zombie process group found in a stale attempt.json is no
    # longer runner-owned code (Runner._recover is deleted per the brief); it is
    # reconcile.reconcile()'s global sweep (Plan 1c Tasks 4/5), already covered by
    # test_reconcile.py (e.g. ReapProcTests, OrphanedRecoveryTests). These tests also hand-wrote
    # a legacy attempt.json shape ({"status": "running", "pgid": ...}) with no worktree/repo_id;
    # attempt.load() now raises UnreadableRecord for that shape and reconcile.reconcile()'s sweep
    # fails closed (FenceExit) on any such record it finds anywhere under attempts/, which makes
    # the old premise (a legacy record silently coexisting with a live runner) impossible to
    # reproduce through the public API any more.

    def test_verification_stage_writes_pgid_to_attempt_json(self):
        # CHANGED (was test_verification_stage_writes_pgid_to_attempt_json's old-schema
        # assertions on attempt.json["status"]=="completed"/["verify_pgids"]): the attempt
        # record is now attempt.Record (Plan 1c); this is also one of Task 6's brief-mandated
        # new assertions -- stages == [worker, verify-0], each terminated, and the record ends
        # PROJECTED with all three projection flags set.
        outcome, rows = self.run_task("pass")
        self.assertEqual(outcome, "accepted")
        adir = self.cfg.state_root / "attempts" / "ZIP-7873" / "001" / "1"
        aj = json.loads((adir / "attempt.json").read_text())
        self.assertEqual(aj["status"], "PROJECTED")
        self.assertTrue(aj["history"] and aj["published"] and aj["lifecycle"])
        kinds = [(s["kind"], s["idx"]) for s in aj["stages"]]
        self.assertEqual(kinds, [("worker", 0), ("verify", 0)])
        self.assertTrue(all(s["terminated"] for s in aj["stages"]))

    def test_metrics_has_one_row_per_stage_for_accepted_attempt(self):
        outcome, rows = self.run_task("pass")
        self.assertEqual(outcome, "accepted")
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["stage_kind"] for r in rows}, {"worker", "verify"})

    def test_fail_finalizes_with_restored_tree_and_distinct_observed_tree(self):
        outcome, rows = self.run_task("fail", "fail", "fail")
        self.assertEqual(outcome, "blocked")
        aj = json.loads((self.cfg.state_root / "attempts" / "ZIP-7873" / "001" / "1" / "attempt.json").read_text())
        self.assertEqual(aj["tree"], "restored")
        self.assertNotEqual(aj["observed_tree"], aj["base_tree"])

    def test_owner_block_sets_next_action_block_and_blocks_ticket(self):
        outcome, rows = self.run_task("owner")
        self.assertEqual(outcome, "blocked")
        aj = json.loads((self.cfg.state_root / "attempts" / "ZIP-7873" / "001" / "1" / "attempt.json").read_text())
        self.assertEqual(aj["next_action"], "block")
        t = state.load(self.cfg.ticket_dir("ZIP-7873"))
        self.assertEqual(t.state, "blocked")

    def test_implement_task_without_run_context_raises_type_error(self):
        t = self._fresh_ticket()
        r = runner.Runner(self.cfg, run_id="test", pi_launcher=self.launcher)
        with self.assertRaises(TypeError):
            r.implement_task(object(), t, self.tasks[0], 0, self.wt)
        with self.assertRaises(TypeError):
            r.implement_task(None, t, self.tasks[0], 0, self.wt)

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

    # ----- Deleted: test_clear_fence_force_marks_running_attempt_orphaned.
    # Reason: this test's second half planted a legacy-shape attempt.json under attempts/ and
    # then called implement_task directly -- reconcile.reconcile()'s global sweep (which
    # implement_task now requires via RunContext) walks every attempt dir under attempts/ and
    # fails closed (FenceExit) on any record without worktree/repo_id, so the legacy record
    # can no longer coexist with a real run. clear-fence itself (the CLI branch exercised by
    # test_clear_fence_cli_refuses_live_and_clears_dead, unchanged above) is untouched by this
    # task; reconciling its fence-file shape with reconcile.fence()'s new shape is Task 7's job.

    # ----- Deleted: test_rewrite_artifact_tolerates_leftover_tmp.
    # Reason: Runner._write_artifact/_rewrite_artifact are deleted per the brief; the no-follow
    # create/rewrite discipline they implemented now lives in attempt.safe_write/safe_rewrite,
    # already covered by test_attempt.py.
        self.assertFalse(list(self.cfg.state_root.glob("x.json.*tmp*")))

    # ----- Re-review regression tests: C1, C3, I6 -------------------------------

    def test_dry_run_cli_subprocess_never_calls_pi(self):
        shim = self.cfg.state_root.parent / "shim"; shim.mkdir()
        sentinel = shim / "PI_WAS_CALLED"
        (shim / "pi").write_text(f"#!/bin/sh\ntouch {sentinel}\nexit 1\n"); (shim / "pi").chmod(0o755)
        env = {**os.environ, "PATH": f"{shim}:{os.environ['PATH']}", "AL_SCENARIO": "pass"}
        r = subprocess.run([sys.executable, str(HERE / "runner.py"), "--config", str(self.cfg.state_root.parent / "hopper.toml"),
                            "dry-run", "--worktree", str(self.wt), "--tasks", str(self.cfg.state_root.parent / "tasks.toml"), "--skip-admission"],
                           capture_output=True, text=True, env=env, cwd=HERE, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(sentinel.exists())
        self.assertEqual(metrics.read_all(self.cfg.state_root)[-1]["outcome"], "accepted")

    # ----- Deleted: test_fence_with_dead_pgid_is_cleared_and_run_proceeds,
    # test_fence_with_live_pgid_pauses.
    # Reason: fence checking is no longer implement_task-owned code; it is
    # reconcile.reconcile()'s global fence check (_check_global_fence), which implement_task's
    # required RunContext already went through before implement_task is ever called. That
    # check's fence-file shape is also different now ({attempt_dir, proc, reason}, not the old
    # {pgid, ...}); a fence file in the old shape now raises FenceExit as "corrupt fence:
    # missing required key(s)" -- see test_reconcile.py's corrupt-fence tests and
    # test_fence_with_dead_pgid_is_cleared_by_recovery-equivalent coverage there.

    def test_dry_run_cli_honors_admission_when_red(self):
        # Red admission test: dry-run without --skip-admission should pause when admission is red.
        # Create a hopper.toml copy with compressor_pct_max = 0.0 (forces red: all machines are > 0%).
        root = self.cfg.state_root.parent
        toml = (HERE.parent / "hopper.toml").read_text()
        toml = toml.replace('state_root = "~/.local/state/agent-loop"', f'state_root = "{self.cfg.state_root}"')
        toml = toml.replace('pi_agents_dir = "~/.pi/agent/agents"', f'pi_agents_dir = "{root}/agents"')
        toml = toml.replace("heavy_stage_s = 4500", "heavy_stage_s = 3")
        toml = re.sub(r'alternate = \[[^\]]*\][^\n]*', 'alternate = ["cloud", "local"]', toml)
        # Force red admission: compressor_pct_max = 0.0
        toml = re.sub(r'compressor_pct_max = [\d.]+', 'compressor_pct_max = 0.0', toml)
        red_toml = root / "hopper_red.toml"
        red_toml.write_text(toml)
        # Run dry-run WITHOUT --skip-admission; should hit real admission probe, get red decision, return rc 1, paused
        r = subprocess.run([sys.executable, str(HERE / "runner.py"), "--config", str(red_toml),
                            "dry-run", "--worktree", str(self.wt), "--tasks", str(root / "tasks.toml")],
                           capture_output=True, text=True, cwd=HERE, timeout=120)
        self.assertEqual(r.returncode, 1, f"Expected rc 1 but got {r.returncode}. stderr: {r.stderr}")
        self.assertIn("paused", r.stdout, f"Expected 'paused' in stdout, got: {r.stdout}")
        # Verify no attempt dirs were created
        attempts_root = self.cfg.state_root / "attempts" / "DRY-1" / "001"
        if attempts_root.exists():
            attempt_nums = [d.name for d in attempts_root.iterdir() if d.is_dir() and d.name.isdigit()]
            self.assertEqual(attempt_nums, [], f"Expected no attempt dirs but found {attempt_nums}")
        # Verify state.json reason starts with "resource:"
        t = state.load(self.cfg.ticket_dir("DRY-1"))
        self.assertTrue(t.reason.startswith("resource:"), f"Expected reason to start with 'resource:' but got: {t.reason}")

    def test_stratified_arms_for_mixed_visual_flags(self):
        toml = TASKS
        for i, vis in (("002", "true"), ("003", "false"), ("004", "true")):
            toml += TASKS.replace('id = "001"', f'id = "{i}"').replace("touch", f"touch{i}").replace('acceptance = ["AC-1"]', f'acceptance = ["AC-1"]\nvisual = {vis}')
        tasks_toml = self.cfg.state_root.parent / "tasks4.toml"; tasks_toml.write_text(toml)
        with patch("runner.procs.run_stage", side_effect=self.launcher):
            self.scenarios = ["pass"] * 4
            runner.main(["--config", str(self.cfg.state_root.parent / "hopper.toml"), "dry-run", "--worktree", str(self.wt), "--tasks", str(tasks_toml)])
        # CHANGED: metrics rows no longer have a "stage" == "implement" field (stages are
        # "stage_kind" == "worker"/"verify", one row each); filter to the worker row per attempt.
        arms = [r["arm"] for r in metrics.read_all(self.cfg.state_root) if r["stage_kind"] == "worker"]
        # RunnerHarness.setUp pins arms.alternate = ["cloud", "local"]; strata are F,T,F,T (non-visual
        # stratum indices 0,1 and visual stratum indices 0,1), so arms are cloud,cloud,local,local.
        self.assertEqual(arms, ["cloud", "cloud", "local", "local"])

    def test_arm_persists_across_alternate_change(self):
        # "owner" blocks on attempt 1 without consuming the ladder, so a second implement_task
        # call on the SAME history can be issued after mutating the config -- run_task() itself
        # can't be called twice on one ticket (it re-drives the full spinup..implement chain,
        # which is illegal once the ticket has already reached "implement").
        outcome, rows = self.run_task("owner")                     # attempt 1 on the stratum-0 arm (cloud)
        self.assertEqual(outcome, "blocked"); self.assertEqual(rows[0]["arm"], "cloud")
        tdir = self.cfg.ticket_dir("ZIP-7873")
        t = state.transition(state.load(tdir), "implement"); state.save(tdir, t)
        object.__setattr__(self.cfg, "arms_alternate", ["local", "cloud"])
        r = runner.Runner(self.cfg, run_id="test", pi_launcher=self.launcher)
        self.scenarios = ["pass"]
        outcome2 = self._implement(r, t, self.tasks[0], 0)
        rows = metrics.read_all(self.cfg.state_root)
        self.assertEqual(outcome2, "accepted")
        self.assertEqual({row["arm"] for row in rows}, {"cloud"})

if __name__ == "__main__":
    unittest.main()
