import contextlib
import io
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import cohort
import config
import drip
import locks
import state


class Supervisor:
    def __init__(self):
        self.running = {"ZIP-1", "ZIP-2"}

    def stop(self, key):
        self.running.discard(key)

    def ensure_running(self, key, worktree, state_root):
        self.running.add(key)


class InteractiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        text = (pathlib.Path(__file__).parent.parent / "hopper.toml").read_text()
        text = text.replace('state_root = "~/.local/state/agent-loop"', f'state_root = "{root}/state"')
        path = root / "hopper.toml"
        path.write_text(text)
        self.cfg = config.load(path)
        self.cfg.ensure_dirs()
        self.worktrees = {}
        for key in ("ZIP-1", "ZIP-2", "ZIP-3"):
            wt = root / key.lower()
            wt.mkdir()
            self.worktrees[key] = wt
            state.save(self.cfg.ticket_dir(key), state.Ticket(key=key, worktree=str(wt), state="implement"))
        cohort.initialize(self.cfg, capacity=2)
        for key in ("ZIP-1", "ZIP-2"):
            cohort.reserve(self.cfg, key, "drip")
            cohort.activate(self.cfg, key)
        self.supervisor = Supervisor()

    def module(self):
        import importlib.util
        self.assertIsNotNone(importlib.util.find_spec("interactive"), "interactive ownership is not implemented")
        import interactive
        return interactive

    def test_parked_foreground_request_bypasses_full_cohort_without_stopping_either_member(self):
        interactive = self.module()
        tdir = self.cfg.ticket_dir("ZIP-3")
        (tdir / "PARKED").touch()
        before = (tdir / "state.json").read_bytes()
        result = interactive.enter(self.cfg, self.worktrees["ZIP-3"], supervisor=self.supervisor)
        self.assertEqual(result["mode"], "interactive")
        self.assertEqual(cohort.members(self.cfg), ["ZIP-1", "ZIP-2"])
        self.assertEqual(self.supervisor.running, {"ZIP-1", "ZIP-2"})
        self.assertTrue(state.human_owned(tdir))
        self.assertEqual((tdir / "state.json").read_bytes(), before)

    def test_taking_over_member_releases_only_its_slot_and_preserves_admission_order(self):
        interactive = self.module()
        before = state.load(self.cfg.ticket_dir("ZIP-1"))
        interactive.enter(self.cfg, self.worktrees["ZIP-1"], supervisor=self.supervisor)
        self.assertEqual(cohort.members(self.cfg), ["ZIP-2"])
        self.assertEqual(self.supervisor.running, {"ZIP-2"})
        self.assertEqual(state.load(self.cfg.ticket_dir("ZIP-1")), before)
        history = cohort.load(self.cfg).history
        self.assertEqual(history[-1]["event"], "interactive")
        self.assertEqual(history[-1]["ticket"], "ZIP-1")
        self.assertEqual(history[-1]["admission_sequence"], 1)
        interactive.enter(self.cfg, self.worktrees["ZIP-1"], supervisor=self.supervisor)
        self.assertEqual(cohort.load(self.cfg).history, history)
        with self.assertRaises(cohort.CohortError):
            cohort.reserve(self.cfg, "ZIP-1", "racing-drip")

    def test_live_runner_must_yield_before_foreground_tools_or_slot_release(self):
        interactive = self.module()
        lease = locks.Lease(self.cfg.state_root / "locks/runner-zip-1", "live attempt")
        self.assertTrue(lease.acquire(hold=True))
        try:
            result = interactive.enter(self.cfg, self.worktrees["ZIP-1"], supervisor=self.supervisor)
            self.assertEqual(result["mode"], "yielding")
            self.assertEqual(cohort.members(self.cfg), ["ZIP-1", "ZIP-2"])
            self.assertEqual(self.supervisor.running, {"ZIP-1", "ZIP-2"})
            self.assertTrue(state.human_owned(self.cfg.ticket_dir("ZIP-1")))
        finally:
            lease.release()
        result = interactive.enter(self.cfg, self.worktrees["ZIP-1"], supervisor=self.supervisor)
        self.assertEqual(result["mode"], "interactive")
        self.assertEqual(cohort.members(self.cfg), ["ZIP-2"])

    def test_reconciler_finishes_a_pending_handoff_after_runner_yields(self):
        interactive = self.module()
        lease = locks.Lease(self.cfg.state_root / "locks/runner-zip-1", "live attempt")
        self.assertTrue(lease.acquire(hold=True))
        try:
            self.assertEqual(interactive.enter(self.cfg, self.worktrees["ZIP-1"], supervisor=self.supervisor)["mode"], "yielding")
        finally:
            lease.release()
        with patch.object(drip, "_tick_locked", return_value=[]):
            report = cohort.reconcile(self.cfg, supervisor=self.supervisor)
        self.assertEqual(report.released, ["ZIP-1"])
        self.assertEqual(cohort.members(self.cfg), ["ZIP-2"])
        self.assertEqual(interactive.status(self.cfg, self.worktrees["ZIP-1"])["mode"], "interactive")
        self.assertEqual(self.supervisor.running, {"ZIP-2"})

    def test_setup_outside_runner_lease_must_finish_before_foreground_can_edit(self):
        interactive = self.module()
        cohort.park_member(self.cfg, "ZIP-1")
        cohort.reserve(self.cfg, "ZIP-1", "drip")
        lease = locks.Lease(self.cfg.state_root / "locks/drip", "spinup")
        self.assertTrue(lease.acquire(hold=True))
        try:
            self.assertEqual(interactive.enter(self.cfg, self.worktrees["ZIP-1"], supervisor=self.supervisor)["mode"], "yielding")
        finally:
            lease.release()
        self.assertEqual(interactive.enter(self.cfg, self.worktrees["ZIP-1"], supervisor=self.supervisor)["mode"], "interactive")

    def test_interrupted_worker_is_recovered_once_before_manual_edits_not_after(self):
        interactive = self.module()
        import attempt
        import ladder
        import reconcile
        from test_reconcile import make_repo, make_task, FakeAgent
        wt = make_repo(self.worktrees["ZIP-3"])
        rec = attempt.create(self.cfg, "ZIP-3", make_task(), wt, 1, FakeAgent(), "cloud",
                             ladder.Rung("cloud-worker", "cheap", 1), "abandoned-run")
        (wt / "app/a.rb").write_text("half-finished worker\n")
        interactive.enter(self.cfg, wt, supervisor=self.supervisor)
        self.assertEqual((wt / "app/a.rb").read_text(), "a\n")
        (wt / "app/a.rb").write_text("manual edit\n")
        reconcile._sweep(self.cfg)
        interactive.enter(self.cfg, wt, supervisor=self.supervisor)
        self.assertEqual((wt / "app/a.rb").read_text(), "manual edit\n")

    def test_adopting_a_legacy_human_takeover_never_restores_its_already_manual_edits(self):
        interactive = self.module()
        import attempt
        import ladder
        from test_reconcile import make_repo, make_task, FakeAgent
        wt = make_repo(self.worktrees["ZIP-3"])
        attempt.create(self.cfg, "ZIP-3", make_task(), wt, 1, FakeAgent(), "cloud",
                       ladder.Rung("cloud-worker", "cheap", 1), "old-run")
        (self.cfg.ticket_dir("ZIP-3") / "HUMAN").touch()
        (wt / "app/a.rb").write_text("already manual\n")
        interactive.enter(self.cfg, wt, supervisor=self.supervisor)
        self.assertEqual((wt / "app/a.rb").read_text(), "already manual\n")
        interactive.return_to_loop(self.cfg, wt)
        import reconcile
        reconcile._sweep(self.cfg)
        self.assertEqual((wt / "app/a.rb").read_text(), "already manual\n")

    def test_legacy_takeover_with_a_recorded_live_worker_refuses_handoff_without_restoring(self):
        interactive = self.module()
        import os
        import attempt
        import ladder
        import procid
        from test_reconcile import make_repo, make_task, FakeAgent
        wt = make_repo(self.worktrees["ZIP-3"])
        rec = attempt.create(self.cfg, "ZIP-3", make_task(), wt, 1, FakeAgent(), "cloud",
                             ladder.Rung("cloud-worker", "cheap", 1), "old-run")
        rec = attempt.transition(rec, "LAUNCHING")
        attempt.transition(rec, "RUNNING", proc=procid.capture(os.getpid()).to_dict())
        (self.cfg.ticket_dir("ZIP-3") / "HUMAN").touch()
        (wt / "app/a.rb").write_text("manual edit\n")
        with self.assertRaises(ValueError):
            interactive.enter(self.cfg, wt, supervisor=self.supervisor)
        self.assertEqual((wt / "app/a.rb").read_text(), "manual edit\n")

    def test_global_recovery_never_restores_a_foreground_worktree(self):
        interactive = self.module()
        interactive.enter(self.cfg, self.worktrees["ZIP-3"], supervisor=self.supervisor)
        # A later background sweep must not touch old attempts after the operator starts editing.
        leaf = self.cfg.state_root / "attempts/ZIP-3/task/1"
        leaf.mkdir(parents=True)
        import reconcile
        reconcile._sweep(self.cfg)  # an empty non-interactive leaf would correctly fence
        self.assertFalse((leaf / "attempt.json").exists())

    def test_only_explicit_return_queues_background_work_and_never_takes_a_third_slot(self):
        interactive = self.module()
        wt = self.worktrees["ZIP-3"]
        interactive.enter(self.cfg, wt, supervisor=self.supervisor)
        result = interactive.return_to_loop(self.cfg, wt)
        self.assertEqual(result["mode"], "queued")
        self.assertFalse(state.human_owned(self.cfg.ticket_dir("ZIP-3")))
        self.assertIn("ZIP-3", drip.parked(self.cfg))
        self.assertEqual(cohort.members(self.cfg), ["ZIP-1", "ZIP-2"])
        self.assertEqual(self.supervisor.running, {"ZIP-1", "ZIP-2"})

    def test_return_does_not_remove_an_independent_legacy_human_takeover(self):
        interactive = self.module()
        tdir = self.cfg.ticket_dir("ZIP-3")
        (tdir / "HUMAN").touch()
        interactive.enter(self.cfg, self.worktrees["ZIP-3"], supervisor=self.supervisor)
        result = interactive.return_to_loop(self.cfg, self.worktrees["ZIP-3"])
        self.assertEqual(result["mode"], "human")
        self.assertTrue((tdir / "HUMAN").exists())

    def test_untracked_workspace_does_not_create_a_cohort_ticket(self):
        result = self.module().enter(self.cfg, pathlib.Path(self.tmp.name) / "other", supervisor=self.supervisor)
        self.assertEqual(result["mode"], "unmanaged")
        self.assertEqual(cohort.members(self.cfg), ["ZIP-1", "ZIP-2"])

    def test_workspace_matching_is_exact_or_descendant_not_a_name_or_prompt_guess(self):
        interactive = self.module()
        sub = self.worktrees["ZIP-3"] / "app"
        sub.mkdir()
        self.assertEqual(interactive.enter(self.cfg, sub, supervisor=self.supervisor)["ticket"], "ZIP-3")
        self.assertEqual(interactive.enter(self.cfg, pathlib.Path(str(self.worktrees["ZIP-3"]) + "-other"), supervisor=self.supervisor)["mode"], "unmanaged")

    def test_background_candidates_exclude_foreground_even_if_jira_still_says_ready(self):
        self.module().enter(self.cfg, self.worktrees["ZIP-3"], supervisor=self.supervisor)
        row = {"key": "ZIP-3", "status": "Selected for Work", "assignee": "", "summary": "one off"}
        with patch.object(drip, "board", return_value=[row]), patch.object(drip, "done_in_jira", return_value=set()):
            self.assertEqual(drip.candidates(self.cfg, log=lambda _: None), [])

    def test_lifecycle_yields_before_git_recovery_can_modify_the_interactive_tree(self):
        self.module().enter(self.cfg, self.worktrees["ZIP-3"], supervisor=self.supervisor)
        import lifecycle
        with patch.object(lifecycle, "recover_interrupted_git_state", side_effect=AssertionError("must not recover foreground git")):
            step = lifecycle.step(self.cfg, None, None, "ZIP-3", self.worktrees["ZIP-3"], hopper_index=3)
        self.assertTrue(step.wait)
        self.assertIn("interactive", step.action.lower())

    def test_overview_and_sidebar_show_foreground_separately_not_as_queued_or_stalled(self):
        self.module().enter(self.cfg, self.worktrees["ZIP-3"], supervisor=self.supervisor)
        (self.cfg.ticket_dir("ZIP-3") / "PARKED").touch()
        import watch
        import sidebar
        overview = watch.fleet(self.cfg, pr_lookup=lambda _: "ready", sup_status=lambda _: "running")
        self.assertIn("COHORT 2/2", overview)
        self.assertIn("INTERACTIVE (outside cohort)", overview)
        self.assertIn("ZIP-3", overview.split("INTERACTIVE (outside cohort)")[1])
        waiting = overview.split("WAITING ADMISSION")[1].split("\n\n")[0]
        self.assertNotIn("ZIP-3", waiting)
        want = sidebar._desired(self.cfg)["ZIP-3"]
        self.assertEqual(want.text, "interactive")
        self.assertFalse(want.spinner)

    def test_an_old_pane_cannot_steal_foreground_back_from_a_new_user_session(self):
        interactive = self.module()
        wt = self.worktrees["ZIP-3"]
        interactive.enter(self.cfg, wt, session="old", supervisor=self.supervisor)
        interactive.enter(self.cfg, wt, session="new", supervisor=self.supervisor)
        result = interactive.enter(self.cfg, wt, session="old", claim=False, supervisor=self.supervisor)
        self.assertEqual(result["mode"], "other-session")
        self.assertEqual(interactive.status(self.cfg, wt)["session"], "new")
        with self.assertRaises(ValueError):
            interactive.return_to_loop(self.cfg, wt, session="old")

    def test_cli_can_explicitly_return_a_workspace_claimed_by_a_pi_session(self):
        import runner
        args = ["--config", str(pathlib.Path(self.tmp.name) / "hopper.toml"), "interactive"]
        wt = str(self.worktrees["ZIP-3"])
        out = io.StringIO()
        with patch("supervise.stop"), contextlib.redirect_stdout(out):
            self.assertEqual(runner.main([*args, "enter", "--worktree", wt, "--session", "pane"]), 0)
            self.assertEqual(json.loads(out.getvalue())["mode"], "interactive")
            out.seek(0); out.truncate()
            self.assertEqual(runner.main([*args, "return", "--worktree", wt]), 0)
            self.assertEqual(json.loads(out.getvalue())["mode"], "queued")
        self.assertEqual(cohort.members(self.cfg), ["ZIP-1", "ZIP-2"])

    def test_worker_yields_without_turning_interactive_ownership_into_a_lifecycle_pause(self):
        self.module().enter(self.cfg, self.worktrees["ZIP-3"], supervisor=self.supervisor)
        import runner
        tdir = self.cfg.ticket_dir("ZIP-3")
        before = state.load(tdir)
        self.assertEqual(runner.Runner(self.cfg)._stop(tdir, before), "paused")
        self.assertEqual(state.load(tdir), before)

    def test_resume_all_does_not_restart_or_report_foreground_as_autonomous(self):
        self.module().enter(self.cfg, self.worktrees["ZIP-3"], supervisor=self.supervisor)
        import runner
        lines = []
        with patch("supervise.status", return_value="state=running"):
            result = runner.resume_all(self.cfg, log=lines.append)
        self.assertNotIn("ZIP-3", result["running"])
        self.assertNotIn("ZIP-3", result["started"])
        self.assertTrue(any("interactive" in line for line in lines))

    def test_run_once_never_reconciles_or_restores_after_foreground_edits(self):
        self.module().enter(self.cfg, self.worktrees["ZIP-3"], supervisor=self.supervisor)
        import runner
        with patch.object(runner.reconcile, "reconcile", side_effect=AssertionError("foreground reconcile")):
            self.assertEqual(runner.main(["--config", str(pathlib.Path(self.tmp.name) / "hopper.toml"),
                                          "run-once", "--ticket", "ZIP-3", "--worktree", str(self.worktrees["ZIP-3"])]), 0)

    def test_return_invalidates_review_evidence_after_manual_edits(self):
        interactive = self.module()
        tdir = self.cfg.ticket_dir("ZIP-3")
        t = state.load(tdir)
        t.state, t.evidence_sha = "human-gate-1", "old-head"
        state.save(tdir, t)
        interactive.enter(self.cfg, self.worktrees["ZIP-3"], supervisor=self.supervisor)
        interactive.return_to_loop(self.cfg, self.worktrees["ZIP-3"])
        self.assertEqual(state.load(tdir).state, "gates")
        self.assertEqual(state.load(tdir).evidence_sha, "")
