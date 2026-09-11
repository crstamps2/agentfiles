import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

import attempt
import contracts
import ladder
import metrics
import reconcile
import state
import worktree


def git(wt, *a):
    return subprocess.run(["git", "-C", str(wt), *a], capture_output=True, text=True, check=True).stdout


def make_repo(tmp) -> pathlib.Path:
    wt = pathlib.Path(tmp)
    git(wt, "init", "-q", "-b", "main")
    git(wt, "config", "user.email", "t@t")
    git(wt, "config", "user.name", "t")
    (wt / "app").mkdir()
    (wt / "app" / "a.rb").write_text("a\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", "init")
    return wt


def make_task(**overrides) -> contracts.Task:
    fields = dict(id="001", slug="s", summary="do the thing", allowed_files=["app/**"],
                  verification_commands=["true"], acceptance=["works"])
    fields.update(overrides)
    return contracts.Task(**fields)


class Cfg:
    def __init__(self, state_root):
        self.state_root = pathlib.Path(state_root)

    def ticket_dir(self, key: str) -> pathlib.Path:
        return self.state_root / "tickets" / key


class FakeAgent:
    name = "cloud-worker"
    body = "you are a worker\n"


class ReconcileTestCase(unittest.TestCase):
    def setUp(self):
        self.repo_tmp = tempfile.TemporaryDirectory()
        self.state_tmp = tempfile.TemporaryDirectory()
        self.wt = make_repo(self.repo_tmp.name)
        self.cfg = Cfg(self.state_tmp.name)
        self.task = make_task()

    def tearDown(self):
        self.repo_tmp.cleanup()
        self.state_tmp.cleanup()

    def _create(self, n=1):
        rung = ladder.Rung("cloud-worker", "cheap", 1)
        return attempt.create(self.cfg, "TICK-1", self.task, self.wt, n,
                              FakeAgent(), "cloud", rung, "run-abc")

    def _to_classified(self, outcome, next_action="none", n=1, stages=None, reason="ok",
                        after_create=None):
        rec = self._create(n=n)
        if after_create is not None:
            after_create()
        rec = attempt.transition(rec, "LAUNCHING")
        rec = attempt.transition(rec, "RUNNING")
        rec = attempt.transition(rec, "STAGE_DONE", stages=stages or [])
        rec = attempt.transition(rec, "CLASSIFYING", observed_tree=worktree.snapshot(self.wt))
        rec = attempt.transition(rec, "CLASSIFIED", outcome=outcome, reason=reason,
                                 next_action=next_action)
        return rec


class FinalizeTests(ReconcileTestCase):
    def test_rejected_restores_worktree_and_writes_regenerable_patch(self):
        rec = self._to_classified(
            "rejected",
            after_create=lambda: (self.wt / "app" / "worker-junk.rb").write_text("oops\n"))
        base_tree = rec.base_tree
        observed_tree = rec.observed_tree
        rec2 = reconcile.finalize(self.cfg, rec)

        self.assertEqual(rec2.status, "FINALIZED")
        self.assertEqual(rec2.tree, "restored")
        self.assertFalse((self.wt / "app" / "worker-junk.rb").exists())
        self.assertEqual(worktree.snapshot(self.wt), base_tree)

        patch_path = rec.path / "diff.patch"
        self.assertTrue(patch_path.exists())
        patch_text = patch_path.read_text()
        self.assertTrue(patch_text.strip())
        expected = subprocess.run(
            ["git", "-C", str(self.wt), "diff", "--no-color", base_tree, observed_tree],
            capture_output=True, text=True, check=True).stdout
        self.assertEqual(patch_text, expected)

    def test_protocol_and_blocked_also_restore(self):
        for outcome in ("protocol", "blocked"):
            with self.subTest(outcome=outcome):
                rec = self._to_classified(
                    outcome, n={"protocol": 2, "blocked": 3}[outcome],
                    after_create=lambda: (self.wt / "app" / "junk.rb").write_text("x\n"))
                rec2 = reconcile.finalize(self.cfg, rec)
                self.assertEqual(rec2.tree, "restored")
                self.assertFalse((self.wt / "app" / "junk.rb").exists())
                (self.wt / "app" / "junk.rb").unlink(missing_ok=True)

    def test_accepted_keeps_worktree(self):
        rec = self._to_classified(
            "accepted", after_create=lambda: (self.wt / "app" / "b.rb").write_text("b\n"))
        rec2 = reconcile.finalize(self.cfg, rec)
        self.assertEqual(rec2.status, "FINALIZED")
        self.assertEqual(rec2.tree, "kept")
        self.assertTrue((self.wt / "app" / "b.rb").exists())

    def test_finalize_requires_classified(self):
        rec = self._create()
        with self.assertRaises(attempt.IllegalAttemptTransition):
            reconcile.finalize(self.cfg, rec)

    def test_finalize_raises_finalize_failed_when_restore_does_not_converge(self):
        rec = self._to_classified("rejected")
        with mock.patch.object(worktree, "verify_restored", return_value=False):
            with self.assertRaises(reconcile.FinalizeFailed):
                reconcile.finalize(self.cfg, rec)

    def test_finalize_fills_in_missing_observed_tree(self):
        rec = self._to_classified("accepted")
        rec = attempt.set_flags(rec, observed_tree=None)
        rec2 = reconcile.finalize(self.cfg, rec)
        self.assertIsNotNone(rec2.observed_tree)


class ProjectHistoryTests(ReconcileTestCase):
    def _classified_and_finalized(self, outcome="accepted", n=1, next_action="none"):
        rec = self._to_classified(outcome, n=n, next_action=next_action)
        return reconcile.finalize(self.cfg, rec)

    def test_appends_entry_once_and_sets_flag(self):
        rec = self._classified_and_finalized()
        rec2 = reconcile.project_history(self.cfg, rec)
        self.assertTrue(rec2.history)
        t = state.load(self.cfg.ticket_dir("TICK-1"))
        entries = t.attempts[rec.lineage]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["attempt_id"], rec.attempt_id)

    def test_running_twice_does_not_duplicate(self):
        rec = self._classified_and_finalized()
        rec2 = reconcile.project_history(self.cfg, rec)
        rec3 = reconcile.project_history(self.cfg, rec2)
        t = state.load(self.cfg.ticket_dir("TICK-1"))
        self.assertEqual(len(t.attempts[rec.lineage]), 1)
        self.assertTrue(rec3.history)

    def test_no_op_when_history_flag_already_true(self):
        rec = self._classified_and_finalized()
        rec = attempt.set_flags(rec, history=True)
        # No ticket dir was ever created; a no-op must not create one either.
        rec2 = reconcile.project_history(self.cfg, rec)
        self.assertEqual(rec2.rev, rec.rev)
        self.assertFalse((self.cfg.ticket_dir("TICK-1") / "state.json").exists())


class ProjectMetricsTests(ReconcileTestCase):
    def _finalized_with_stages(self):
        stages = [
            {"kind": "worker", "idx": 0, "proc": None, "terminated": True, "timed_out": False,
             "rc": 0, "elapsed_s": 1.5},
            {"kind": "verify", "idx": 0, "proc": None, "terminated": True, "timed_out": False,
             "rc": 0, "elapsed_s": 0.5},
        ]
        rec = self._to_classified("accepted", stages=stages)
        return reconcile.finalize(self.cfg, rec)

    def test_one_row_per_stage(self):
        rec = self._finalized_with_stages()
        rec2 = reconcile.project_metrics(self.cfg, rec)
        rows = metrics.read_all(self.cfg.state_root)
        self.assertEqual(len(rows), 2)
        self.assertTrue(rec2.published)
        kinds = sorted((r["stage_kind"], r["idx"]) for r in rows)
        self.assertEqual(kinds, [("verify", 0), ("worker", 0)])

    def test_running_twice_does_not_duplicate_rows(self):
        rec = self._finalized_with_stages()
        rec2 = reconcile.project_metrics(self.cfg, rec)
        rec3 = reconcile.project_metrics(self.cfg, rec2)
        rows = metrics.read_all(self.cfg.state_root)
        self.assertEqual(len(rows), 2)
        self.assertTrue(rec3.published)

    def test_no_op_when_published_flag_already_true(self):
        rec = self._finalized_with_stages()
        rec = attempt.set_flags(rec, published=True)
        reconcile.project_metrics(self.cfg, rec)
        rows = metrics.read_all(self.cfg.state_root)
        self.assertEqual(rows, [])


class ProjectLifecycleTests(ReconcileTestCase):
    def test_block_transitions_ticket_to_blocked(self):
        rec = self._to_classified("blocked", next_action="block")
        rec = reconcile.finalize(self.cfg, rec)
        rec2 = reconcile.project_lifecycle(self.cfg, rec)
        t = state.load(self.cfg.ticket_dir("TICK-1"))
        self.assertEqual(t.state, "blocked")
        self.assertTrue(rec2.lifecycle)

    def test_running_twice_does_not_raise_illegal_transition(self):
        rec = self._to_classified("blocked", next_action="block")
        rec = reconcile.finalize(self.cfg, rec)
        rec2 = reconcile.project_lifecycle(self.cfg, rec)
        rec3 = reconcile.project_lifecycle(self.cfg, rec2)
        self.assertTrue(rec3.lifecycle)
        t = state.load(self.cfg.ticket_dir("TICK-1"))
        self.assertEqual(t.state, "blocked")

    def test_pause_env_transitions_ticket_to_paused(self):
        rec = self._to_classified("environment", next_action="pause-env")
        rec = reconcile.finalize(self.cfg, rec)
        reconcile.project_lifecycle(self.cfg, rec)
        t = state.load(self.cfg.ticket_dir("TICK-1"))
        self.assertEqual(t.state, "paused")

    def test_none_does_nothing_to_ticket_state(self):
        rec = self._to_classified("accepted", next_action="none")
        rec = reconcile.finalize(self.cfg, rec)
        reconcile.project_lifecycle(self.cfg, rec)
        t = state.load(self.cfg.ticket_dir("TICK-1"))
        self.assertEqual(t.state, "queued")

    def test_no_op_when_lifecycle_flag_already_true(self):
        rec = self._to_classified("blocked", next_action="block")
        rec = reconcile.finalize(self.cfg, rec)
        rec = attempt.set_flags(rec, lifecycle=True)
        reconcile.project_lifecycle(self.cfg, rec)
        self.assertFalse((self.cfg.ticket_dir("TICK-1") / "state.json").exists())


class ProjectAllTests(ReconcileTestCase):
    def test_project_all_reaches_projected_and_is_idempotent(self):
        rec = self._to_classified("accepted", next_action="none", stages=[
            {"kind": "worker", "idx": 0, "proc": None, "terminated": True, "timed_out": False,
             "rc": 0, "elapsed_s": 1.0},
        ])
        rec = reconcile.finalize(self.cfg, rec)
        rec2 = reconcile.project_all(self.cfg, rec)
        self.assertEqual(rec2.status, "PROJECTED")
        self.assertTrue(rec2.history and rec2.published and rec2.lifecycle)

        rec3 = reconcile.project_all(self.cfg, rec2)
        self.assertEqual(rec3.status, "PROJECTED")
        rows = metrics.read_all(self.cfg.state_root)
        self.assertEqual(len(rows), 1)
        t = state.load(self.cfg.ticket_dir("TICK-1"))
        self.assertEqual(len(t.attempts[rec.lineage]), 1)


if __name__ == "__main__":
    unittest.main()
