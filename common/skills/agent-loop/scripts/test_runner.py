# test_runner.py
import contextlib, io, json, os, pathlib, re, subprocess, sys, tempfile, threading, time, unittest
from unittest.mock import patch
import agentdef, attempt, config, contracts, ladder, locks, metrics, procid, procs, reconcile, runner, state, worktree

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
        toml_source = (HERE.parent / "hopper.toml").read_text()
        # local-worker's rendered model must match [local].model (the launch-model check,
        # Task 7) -- read it straight out of the real hopper.toml rather than hard-coding a
        # second copy that could silently drift from the production ctx32k pin.
        local_model_match = re.search(r'model = "([^"]+)"', toml_source)
        self.local_model_default = local_model_match.group(1)
        for n, m in (("cloud-worker", "ollama-cloud/x"), ("local-worker", self.local_model_default), ("premium-worker", "openai-codex/x")):
            (agents / f"{n}.md").write_text(AGENT.format(name=n, model=m))
        toml = toml_source
        toml = toml.replace('state_root = "~/.local/state/agent-loop"', f'state_root = "{root}/state"')
        toml = toml.replace('pi_agents_dir = "~/.pi/agent/agents"', f'pi_agents_dir = "{agents}"')
        toml = toml.replace("heavy_stage_s = 4500", "heavy_stage_s = 3")
        # Pin the arm order the ladder tests assume (index 0 -> cloud). The production default is an
        # operator preference (local-first since 2026-09-10) and must not silently flip these assertions.
        toml = re.sub(r'alternate = \[[^\]]*\][^\n]*', 'alternate = ["cloud", "local"]', toml)
        toml = re.sub(r'^pin_arm = .*$', '', toml, flags=re.M)          # operator pins are production state, not test fixture
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
        outcome, rows = self.run_task("fail", "fail", "fail", "fail")
        self.assertEqual(outcome, "blocked"); self.assertEqual(self.launches, 4)
        self.assertEqual([r["agent"] for r in rows], ["cloud-worker", "cloud-worker", "premium-worker", "premium-worker"])
        self.assertEqual([r["tier"] for r in rows], ["cheap", "cheap", "premium", "premium"])
        self.assertEqual(git(self.wt, "status", "--porcelain").strip(), "")       # tree restored
        self.assertEqual(len(list(self.cfg.state_root.glob("attempts/ZIP-7873/001/*/diff.patch"))), 4)

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
        outcome, rows = self.run_task("timeout", "fail", "fail", "fail")
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

    def test_cheap_owner_claim_does_not_block_the_ticket(self):
        """Changed 2026-09-14: a cheap worker's blocked/owner is a claim that escalates; only premium blocks."""
        outcome, rows = self.run_task("owner", "pass")
        self.assertEqual(outcome, "accepted"); self.assertEqual(rows[0]["outcome"], "protocol"); self.assertIn("escalating", rows[0]["reason"])

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

    def test_heavy_lane_busy_defers_verification_as_environment(self):
        """Worker stages are light and run regardless; VERIFICATION needs the heavy lane. If it stays busy
        past the wait budget the attempt is `environment` (tree restored, rung NOT consumed) and the
        ticket pauses -- never a hang, never a rejected worker."""
        import locks
        held = locks.Lease(self.cfg.state_root / "locks" / "heavy", "other"); self.assertTrue(held.acquire())
        with patch.object(runner, "HEAVY_WAIT_MAX_S", 0):
            outcome, rows = self.run_task("pass", "pass")
        self.assertEqual(self.launches, 2)                                   # two workers ran (light); both deferred
        self.assertEqual([r["outcome"] for r in rows], ["environment", "environment"]); self.assertIn("heavy lane busy", rows[0]["reason"])
        self.assertEqual(outcome, "paused")                                  # env pause after 2; no rung consumed

    def test_worker_slots_exhausted_pauses_without_launching(self):
        import locks
        n = self.cfg.workers_parallel
        slots = [locks.slot(self.cfg.state_root / "locks" / "worker", n, f"other-{i}") for i in range(n)]
        self.assertTrue(all(slots))                                          # every slot held by "others"
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
            rc = runner.main(["--config", str(self.cfg.state_root.parent / "hopper.toml"), "dry-run", "--no-publish",
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
            rc = runner.main(["--config", str(self.cfg.state_root.parent / "hopper.toml"), "dry-run", "--no-publish",
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
        """A second runner on the SAME ticket is fenced (exit 3); a global runner holding the sweep lease
        does not fence a per-ticket runner (it just narrows the sweep to that ticket)."""
        import locks
        mine = locks.Lease(self.cfg.state_root / "locks" / "runner-dry-1", "runner-dry-1"); self.assertTrue(mine.acquire(hold=True))
        try:
            rc = runner.main(["--config", str(self.cfg.state_root.parent / "hopper.toml"), "dry-run", "--no-publish",
                              "--worktree", str(self.wt), "--tasks", str(self.cfg.state_root.parent / "tasks.toml"), "--scenario", "pass"])
            self.assertEqual(rc, 3)
        finally:
            mine.release()

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
        outcome, rows = self.run_task("fail", "fail", "fail", "fail")
        self.assertEqual(outcome, "blocked")
        aj = json.loads((self.cfg.state_root / "attempts" / "ZIP-7873" / "001" / "1" / "attempt.json").read_text())
        self.assertEqual(aj["tree"], "restored")
        self.assertNotEqual(aj["observed_tree"], aj["base_tree"])

    def test_owner_block_sets_next_action_block_and_blocks_ticket(self):
        outcome, rows = self.run_task("fail", "fail", "owner")          # only the PREMIUM rung may block
        self.assertEqual(outcome, "blocked")
        aj = json.loads((self.cfg.state_root / "attempts" / "ZIP-7873" / "001" / "3" / "attempt.json").read_text())
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



    # ----- Deleted: test_rewrite_artifact_tolerates_leftover_tmp.
    # Reason: Runner._write_artifact/_rewrite_artifact are deleted per the brief; the no-follow
    # create/rewrite discipline they implemented now lives in attempt.safe_write/safe_rewrite,
    # already covered by test_attempt.py.

    def test_result_written_relative_to_worktree_is_detected_in_feedback(self):
        # fake scenario `misplaced_result`: does the pass edit, but writes result.md to
        # <worktree>/<attempt-dir-as-relative>/result.md instead of the absolute attempt dir.
        outcome, rows = self.run_task("misplaced_result", "pass")
        self.assertEqual(rows[0]["outcome"], "rejected")
        self.assertIn("WRONG place", rows[0]["reason"]); self.assertIn("ABSOLUTE", rows[0]["reason"].upper())
        self.assertEqual(outcome, "accepted")

    def test_finalize_failure_pauses_ticket_instead_of_crashing(self):
        """Overnight-run finding: FinalizeFailed escaped implement_task as a traceback. It must pause
        the ticket with the reason and leave the record CLASSIFIED for reconcile to retry."""
        with patch("runner.reconcile.finalize", side_effect=reconcile.FinalizeFailed("boom: did not converge")):
            outcome, rows = self.run_task("fail")
        self.assertEqual(outcome, "paused")
        t = state.load(self.cfg.ticket_dir("ZIP-7873"))
        self.assertEqual(t.state, "paused"); self.assertIn("finalize failed", t.reason); self.assertIn("boom", t.reason)
        rec = attempt.load(self.cfg.state_root / "attempts" / "ZIP-7873" / "001" / "1", validate_worktree=False)
        self.assertEqual(rec.status, "CLASSIFIED")

    def test_missing_result_with_clean_evidence_is_accepted_by_the_gate(self):
        """Overnight finding: the worker did the work but wrote no result.md. Evidence (allowlisted
        tree change + rc 0 + verification passes) accepts; the reason says so."""
        outcome, rows = self.run_task("no_result")
        self.assertEqual(outcome, "accepted"); self.assertEqual(self.launches, 1)
        self.assertIn("accepted from evidence", rows[0]["reason"])

    def test_missing_result_with_failing_verification_is_still_rejected(self):
        tasks = self.tasks_with(verification_commands=["false"])
        outcome, rows = self.run_task("no_result", "pass", tasks=tasks)
        self.assertEqual(rows[0]["outcome"], "rejected"); self.assertIn("verification failed", rows[0]["reason"])

    def test_missing_result_with_no_source_change_is_still_protocol(self):
        # scenario `transport_429` exits nonzero with no edit -> environment; use `owner`-less path:
        # a worker that exits 0 having changed nothing is still a protocol failure.
        outcome, rows = self.run_task("noop", "pass")
        self.assertEqual(rows[0]["outcome"], "protocol")

    def test_dry_run_publishes_accepted_task_paths(self):
        calls = {}
        def fake_publish(cfg, key, wt, task):
            calls["paths"] = runner._accepted_source_paths(cfg, key, task.id, wt); calls["key"] = key
        with patch("runner.procs.run_stage", side_effect=self.launcher), patch("runner.publish_accepted", side_effect=fake_publish):
            rc = runner.main(["--config", str(self.cfg.state_root.parent / "hopper.toml"), "dry-run",
                              "--worktree", str(self.wt), "--tasks", str(self.cfg.state_root.parent / "tasks.toml"), "--scenario", "pass"])
        self.assertEqual(rc, 0); self.assertEqual(calls["key"], "DRY-1")
        self.assertTrue(calls["paths"]); self.assertTrue(all(not p.startswith(".pi/") for p in calls["paths"]))

    def test_unexpected_publish_exception_pauses_ticket(self):
        with patch("runner.publish.guard_branch", side_effect=UnicodeDecodeError("utf-8", b"\x81", 0, 1, "bad")):
            runner.publish_accepted(self.cfg, "ZIP-7873", self.wt, contracts.load_tasks(self.cfg.state_root.parent / "tasks.toml")[0])
        t = state.load(self.cfg.ticket_dir("ZIP-7873")); self.assertEqual(t.state, "paused"); self.assertIn("UnicodeDecodeError", t.reason)

    def test_publish_failure_pauses_ticket_and_keeps_acceptance(self):
        import publish
        with patch("runner.publish.guard_branch", side_effect=publish.PublishError("wrong branch")):
            runner.publish_accepted(self.cfg, "ZIP-7873", self.wt, contracts.load_tasks(self.cfg.state_root.parent / "tasks.toml")[0])
        t = state.load(self.cfg.ticket_dir("ZIP-7873"))
        self.assertEqual(t.state, "paused"); self.assertIn("publish failed", t.reason)

    def test_environment_only_history_does_not_pin_the_arm(self):
        """ZIP-7873/001: three local attempts died on the context window (environment); a later pin_arm
        must take effect. Only ADVANCING/accepted outcomes make the arm sticky."""
        tdir = self.cfg.ticket_dir("ZIP-7873"); t = state.load(tdir)
        t.attempts["ZIP-7873/001"] = [{"n": 1, "outcome": "environment", "arm": "local", "rung": {"agent": "local-worker", "tier": "cheap", "n": 1}},
                                      {"n": 2, "outcome": "interrupted", "arm": "local", "rung": {"agent": "local-worker", "tier": "cheap", "n": 2}}]
        state.save(tdir, t)
        import config as config_mod
        with patch.object(config_mod.TicketSpec, "pin_arm", "cloud", create=True):
            spec = self.cfg.tickets.get("ZIP-7873")
            if spec is not None:
                object.__setattr__(spec, "pin_arm", "cloud")
            outcome, rows = self.run_task("pass")
        self.assertEqual(rows[0]["outcome"], "accepted"); self.assertEqual(rows[0]["agent"], "cloud-worker")

    def test_cheap_owner_block_escalates_instead_of_blocking_ticket(self):
        """Live 2026-09-14: the cloud 20B model reported blocked/owner for a task it could not do.
        Cheap rungs may not block the ticket; the claim advances the ladder to premium."""
        outcome, rows = self.run_task("owner", "owner", "pass")
        self.assertEqual(outcome, "accepted")
        worker_rows = [r for r in rows if r.get("stage_kind", "worker") == "worker" or r.get("kind") == "worker"]
        self.assertEqual([r["outcome"] for r in rows][:3], ["protocol", "protocol", "accepted"])
        self.assertEqual(rows[2]["agent"], "premium-worker"); self.assertIn("escalating", rows[0]["reason"])

    def test_premium_owner_block_still_blocks_ticket(self):
        outcome, rows = self.run_task("fail", "fail", "owner")
        self.assertEqual(outcome, "blocked"); self.assertEqual(rows[2]["outcome"], "blocked")

    def test_parallel_implement_lands_independent_tasks_in_manifest_order(self):
        """workers_parallel > 1: independent tasks run concurrently in their own worktrees; accepted diffs
        land on the ticket worktree in manifest order and each is published; dependent tasks wait."""
        import parallel
        (self.cfg.state_root.parent / "tasks3.toml").write_text('''
[[tasks]]
id = "001"
slug = "a"
summary = "a"
allowed_files = ["app/a/**"]
verification_commands = ["true"]
acceptance = ["x"]
[[tasks]]
id = "002"
slug = "b"
summary = "b"
allowed_files = ["app/b/**"]
verification_commands = ["true"]
acceptance = ["x"]
[[tasks]]
id = "003"
slug = "c"
summary = "c"
allowed_files = ["app/c/**"]
verification_commands = ["true"]
acceptance = ["x"]
after = ["001"]
''')
        tasks = contracts.load_tasks(self.cfg.state_root.parent / "tasks3.toml")
        self.scenarios = ["pass", "pass", "pass"]
        # the fake launcher pops scenarios from a shared list -- fine across threads for 3 passes
        r = runner.Runner(self.cfg, run_id="test", pi_launcher=self.launcher)
        t = state.load(self.cfg.ticket_dir("ZIP-7873")); t.worktree = str(self.wt)
        for s_ in ("spinup", "plan", "plan-review", "implement"): t = state.transition(t, s_)
        state.save(self.cfg.ticket_dir("ZIP-7873"), t)
        published = []
        object.__setattr__(self.cfg, "workers_parallel", 2)
        ctx = reconcile.reconcile(self.cfg, "test")
        try:
            out = parallel.implement_parallel(self.cfg, r, ctx, t, tasks, self.wt,
                                              is_done=lambda tk: False, publish_one=lambda tk, paths: published.append((tk.id, sorted(paths))),
                                              log=lambda m: None)
        finally:
            ctx.close()
        self.assertEqual(out, "accepted")
        self.assertEqual([p[0] for p in published], ["001", "002", "003"])           # manifest order
        for sub in ("a", "b", "c"):
            self.assertTrue((self.wt / "app" / sub / "worker_touch.rb").exists(), sub)  # landed on the ticket worktree
        self.assertFalse((self.wt.parent / ".al-tasks" / "zip-7873").exists() and any((self.wt.parent / ".al-tasks" / "zip-7873").iterdir()))

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
        # Attempt 1 is rejected on the stratum-0 arm (cloud). Flip arms.alternate BETWEEN attempts of the
        # same lineage (from inside the launcher); attempt 2 must stay on cloud because the arm is pinned
        # by the first real outcome.
        real_launcher = self.launcher
        def flipping_launcher(*a, **k):
            res = real_launcher(*a, **k)
            object.__setattr__(self.cfg, "arms_alternate", ["local", "cloud"])
            return res
        self.launcher = flipping_launcher
        outcome, rows = self.run_task("fail", "pass")
        self.assertEqual(outcome, "accepted")
        self.assertEqual({row["arm"] for row in rows}, {"cloud"}); self.assertGreaterEqual(len(rows), 2)

    # ----- Fix round 1, finding 3: attempt.create() UnsafePath -> no record; ladder
    # advances by hand, append_feedback is skipped, and a later attempt (once the trap is
    # gone) proceeds normally. --------------------------------------------------------

    def test_unsafe_task_feedback_advances_ladder_without_attempt_dir_then_recovers(self):
        # A pre-existing bare task-level directory with nothing but a planted symlink (no
        # attempt subdir yet) is ambiguous to reconcile.reconcile()'s sweep (a task-id dir
        # with zero attempt children is indistinguishable from an attempt-number leaf with a
        # missing record -- a separate, pre-existing limitation, not this fix's concern), so
        # this test instead models an attacker/operator swapping a *real* feedback.md (left
        # behind by a normal first attempt) for a symlink in between two attempts -- the
        # scenario attempt.create()'s no-follow read is actually guarding against.
        outside = self.cfg.state_root.parent / "outside_feedback.md"
        outside.write_text("sneaky")
        task_level = self.cfg.state_root / "attempts" / "ZIP-7873" / "001"
        real_create = attempt.create
        calls = {"n": 0}

        def trap_second_create(cfg, ticket_key, task, wt, n, agent, arm, rung, run_id):
            calls["n"] += 1
            if calls["n"] != 2:
                return real_create(cfg, ticket_key, task, wt, n, agent, arm, rung, run_id)
            fb = task_level / "feedback.md"
            if fb.exists() or fb.is_symlink():
                fb.unlink()
            fb.symlink_to(outside)
            try:
                return real_create(cfg, ticket_key, task, wt, n, agent, arm, rung, run_id)
            except attempt.UnsafePath:
                fb.unlink()   # the trap is discovered and cleared before the next attempt
                raise

        t = self._fresh_ticket()
        state.save(self.cfg.ticket_dir("ZIP-7873"), t)
        r = runner.Runner(self.cfg, run_id="test", pi_launcher=self.launcher)
        self.scenarios = ["fail", "pass"]
        with patch("attempt.create", side_effect=trap_second_create):
            outcome = self._implement(r, t, self.tasks[0], 0)

        self.assertEqual(outcome, "accepted")
        dirs = sorted(p.name for p in task_level.iterdir() if p.is_dir() and p.name.isdigit())
        # Attempt 1 (real, rejected) and the eventual accepted attempt reuse n=2, since the
        # trapped create() cleaned up (rmtree'd) its own leaf dir before raising.
        self.assertEqual(dirs, ["1", "2"])
        self.assertEqual(outside.read_text(), "sneaky")  # create() never wrote through the trap

        saved = state.load(self.cfg.ticket_dir("ZIP-7873"))
        entries = saved.attempts[f"ZIP-7873/{self.tasks[0].id}"]
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0]["outcome"], "rejected")
        self.assertEqual(entries[1]["n"], None)
        self.assertEqual(entries[1]["outcome"], "protocol")
        self.assertEqual(entries[1]["attempt_id"], None)
        self.assertIn("unsafe feedback.md", entries[1]["reason"])
        self.assertEqual(entries[2]["outcome"], "accepted")
        # append_feedback was skipped for the no-record outcome: the trapped iteration never
        # touched feedback.md content (only the symlink itself, already cleaned up above).
        self.assertFalse((task_level / "feedback.md").is_symlink())

    # ----- Fix round 1, finding 4: implement_task requires a RunContext actually produced
    # by reconcile.reconcile() -- closed or hand-built contexts are refused. ------------

    def test_closed_run_context_raises_type_error(self):
        t = self._fresh_ticket()
        r = runner.Runner(self.cfg, run_id="test", pi_launcher=self.launcher)
        ctx = reconcile.reconcile(self.cfg, "test")
        ctx.close()
        with self.assertRaises(TypeError):
            r.implement_task(ctx, t, self.tasks[0], 0, self.wt)

    def test_hand_built_run_context_raises_type_error(self):
        t = self._fresh_ticket()
        r = runner.Runner(self.cfg, run_id="test", pi_launcher=self.launcher)
        lease = reconcile.locks.Lease(self.cfg.state_root / "locks" / "runner", "runner")
        ctx = reconcile.RunContext(self.cfg, "x", lease)
        try:
            with self.assertRaises(TypeError):
                r.implement_task(ctx, t, self.tasks[0], 0, self.wt)
        finally:
            pass  # never acquired the lease; nothing to release

    # ----- Fix round 1, finding 5: stderr.log is read via attempt.safe_read; a worker that
    # replaces it with a symlink is protocol, and the outside target is never touched. -----

    def test_worker_replaced_stderr_log_is_protocol_and_leaves_outside_file(self):
        outside = self.cfg.state_root.parent / "outside_stderr.txt"
        outside.write_text("keep")
        real_launcher = self.launcher

        def corrupting_launcher(argv, cwd, timeout_s, env, stdout_path, stderr_path, on_start=None):
            result = real_launcher(argv, cwd, timeout_s, env, stdout_path, stderr_path, on_start=on_start)
            stderr_path = pathlib.Path(stderr_path)
            if stderr_path.exists():
                stderr_path.unlink()
            stderr_path.symlink_to(outside)
            return result

        r = runner.Runner(self.cfg, run_id="test", pi_launcher=corrupting_launcher)
        self.scenarios = ["pass"]
        t = self._fresh_ticket()
        state.save(self.cfg.ticket_dir("ZIP-7873"), t)
        self._implement(r, t, self.tasks[0], 0)

        rows = metrics.read_all(self.cfg.state_root)
        self.assertEqual(rows[0]["outcome"], "protocol")
        self.assertIn("stderr.log", rows[0]["reason"])
        self.assertEqual(outside.read_text(), "keep")

    # ----- Task 7: `main` reconciles first (every subcommand but `status`); `clear-fence`
    # routes to reconcile.clear_fence; launch-model check. ------------------------------
    #
    # Deleted: test_clear_fence_cli_refuses_live_and_clears_dead. Reason: it read/wrote the
    # pre-Plan-1c fence shape ({pgid, ticket, task, attempt}) and patched
    # runner.procs.group_state, both gone now that the CLI branch is `reconcile.clear_fence`
    # operating on the {attempt_dir, proc, reason} schema over a real attempt.Record
    # produced by reconcile.fence(). Replaced by test_clear_fence_cli_dead_fence_interrupts
    # below (CLI wiring) plus reconcile.py's own ClearFenceTests (semantics: lease conflict,
    # dead/live/force/corrupt), per the brief.

    def _spawn_sleep(self):
        p = subprocess.Popen(["sleep", "60"], start_new_session=True)
        time.sleep(0.1)
        threading.Thread(target=p.wait, daemon=True).start()
        return p, procid.capture(p.pid)

    def _reap(self, p):
        try:
            os.killpg(os.getpgid(p.pid), 9)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            p.wait(timeout=2)
        except Exception:
            pass

    def test_clear_fence_cli_dead_fence_interrupts_via_reconcile(self):
        # Wiring test: the CLI branch is a thin call into reconcile.clear_fence; the full
        # semantics matrix (lease conflict, dead/live/force, corrupt) lives in
        # test_reconcile.py's ClearFenceTests.
        rung = ladder.Rung("cloud-worker", "cheap", 1)
        agent = agentdef.load(self.cfg.pi_agents_dir, "cloud-worker")
        rec = attempt.create(self.cfg, "ZIP-7873", self.tasks[0], self.wt, 1, agent, "cloud", rung, "prior-run")
        rec = attempt.transition(rec, "LAUNCHING", stages=[{"kind": "worker", "idx": 0, "proc": None}])
        rec = attempt.transition(rec, "RUNNING", proc=None)
        stages = [{"kind": "worker", "idx": 0, "proc": None, "terminated": False,
                  "timed_out": False, "rc": None, "elapsed_s": 1.0}]
        rec = attempt.transition(rec, "STAGE_DONE", stages=stages, proc=None)
        dead_proc = {"boot_id": locks.boot_id(), "pgid": 999999, "pid": 999999,
                    "start_time": "x", "cmd": "y"}
        with self.assertRaises(reconcile.FenceExit):
            reconcile.fence(self.cfg, rec, "termination unverified", proc=dead_proc)

        cfgfile = str(self.cfg.state_root.parent / "hopper.toml")
        rc = runner.main(["--config", cfgfile, "clear-fence"])
        self.assertEqual(rc, 0)
        self.assertFalse((self.cfg.state_root / "locks" / "heavy.fence").exists())
        reloaded = attempt.load(rec.path)
        self.assertEqual(reloaded.status, "INTERRUPTED")

    def test_dry_run_kills_dead_group_before_launching_new_worker(self):
        # A RUNNING record with a live `sleep` group, left behind under a DIFFERENT ticket,
        # must be reconciled (killed + interrupted) before dispatch ever launches the fake
        # worker for the ticket dry-run actually drives.
        p, pid = self._spawn_sleep()
        try:
            rung = ladder.Rung("cloud-worker", "cheap", 1)
            agent = agentdef.load(self.cfg.pi_agents_dir, "cloud-worker")
            rec = attempt.create(self.cfg, "OTHER-1", self.tasks[0], self.wt, 1, agent, "cloud", rung, "prior-run")
            rec = attempt.transition(rec, "LAUNCHING", stages=[{"kind": "worker", "idx": 0, "proc": None}])
            rec = attempt.transition(rec, "RUNNING", proc=pid.to_dict())

            order = []
            real_kill = procs.kill_group

            def spy_kill(pgid, *a, **kw):
                order.append(("kill", pgid))
                return real_kill(pgid, *a, **kw)

            def spy_run_stage(argv, cwd, timeout_s, env, out, err, on_start=None):
                order.append(("launch",))
                return self.launcher(argv, cwd, timeout_s, env, out, err, on_start=on_start)

            self.scenarios = ["pass"]
            with patch("reconcile.procs_mod.kill_group", side_effect=spy_kill), \
                 patch("runner.procs.run_stage", side_effect=spy_run_stage):
                rc = runner.main(["--config", str(self.cfg.state_root.parent / "hopper.toml"), "dry-run", "--no-publish",
                                  "--worktree", str(self.wt), "--tasks", str(self.cfg.state_root.parent / "tasks.toml"),
                                  "--scenario", "pass"])
            self.assertEqual(rc, 0)
            leftover_kills = [i for i, o in enumerate(order) if o[0] == "kill" and o[1] == pid.pgid]
            launches = [i for i, o in enumerate(order) if o[0] == "launch"]
            self.assertTrue(leftover_kills, "the leftover group was never killed")
            self.assertTrue(launches, "the fake worker was never launched")
            self.assertLess(min(leftover_kills), min(launches),
                            "the leftover group must be killed before the new worker launches")
            reloaded = attempt.load(rec.path)
            self.assertEqual(reloaded.status, "INTERRUPTED")
            self.assertEqual(reloaded.outcome, "interrupted")
            self.assertTrue(reloaded.history and reloaded.published and reloaded.lifecycle)
        finally:
            self._reap(p)

    def test_dry_run_with_orphaned_record_and_live_fence_exits_3_before_dispatch(self):
        p, pid = self._spawn_sleep()
        try:
            rung = ladder.Rung("cloud-worker", "cheap", 1)
            agent = agentdef.load(self.cfg.pi_agents_dir, "cloud-worker")
            rec = attempt.create(self.cfg, "OTHER-2", self.tasks[0], self.wt, 1, agent, "cloud", rung, "prior-run")
            rec = attempt.transition(rec, "LAUNCHING", stages=[{"kind": "worker", "idx": 0, "proc": None}])
            rec = attempt.transition(rec, "RUNNING", proc=None)
            stages = [{"kind": "worker", "idx": 0, "proc": pid.to_dict(), "terminated": False,
                      "timed_out": False, "rc": None, "elapsed_s": 1.0}]
            rec = attempt.transition(rec, "STAGE_DONE", stages=stages, proc=None)
            with self.assertRaises(reconcile.FenceExit):
                reconcile.fence(self.cfg, rec, "termination unverified", proc=pid.to_dict())

            with patch("runner.procs.run_stage", side_effect=self.launcher):
                rc = runner.main(["--config", str(self.cfg.state_root.parent / "hopper.toml"), "dry-run", "--no-publish",
                                  "--worktree", str(self.wt), "--tasks", str(self.cfg.state_root.parent / "tasks.toml"),
                                  "--scenario", "pass"])
            self.assertEqual(rc, 3)
            self.assertEqual(self.launches, 0)
        finally:
            self._reap(p)

    def test_dry_run_local_worker_model_mismatch_fails_before_launch(self):
        bad_model = "ollama-local/other-ctx32k:20b"
        (self.cfg.pi_agents_dir / "local-worker.md").write_text(AGENT.format(name="local-worker", model=bad_model))
        tasks_toml = self.cfg.state_root.parent / "tasks_mismatch.toml"
        tasks_toml.write_text(TASKS + TASKS.replace('id = "001"', 'id = "002"').replace("touch", "touch2"))
        with patch("runner.procs.run_stage", side_effect=self.launcher):
            self.scenarios = ["pass"]
            with self.assertRaises(RuntimeError) as cm:
                runner.main(["--config", str(self.cfg.state_root.parent / "hopper.toml"), "dry-run", "--no-publish",
                             "--worktree", str(self.wt), "--tasks", str(tasks_toml)])
        msg = str(cm.exception)
        self.assertIn(bad_model, msg)
        self.assertIn(self.local_model_default, msg)
        # task 001 (cloud arm, index 0) launched fine and was accepted -- one worker-stage
        # launch plus its one verification command, both routed through the same patched
        # procs.run_stage.
        self.assertEqual(self.launches, 2)
        self.assertFalse((self.cfg.state_root / "attempts" / "DRY-1" / "002" / "1").exists())

if __name__ == "__main__":
    unittest.main()
