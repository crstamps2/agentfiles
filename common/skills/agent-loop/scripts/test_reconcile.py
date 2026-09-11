import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import attempt
import contracts
import ladder
import locks
import metrics
import procid
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
        self.protected_paths = []
        self.test_path_globs = []

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

    def test_finalize_requires_observed_tree(self):
        rec = self._to_classified("accepted")
        rec = attempt.set_flags(rec, observed_tree=None)
        base_tree = rec.base_tree
        before = worktree.snapshot(self.wt)
        with self.assertRaises(reconcile.FinalizeFailed):
            reconcile.finalize(self.cfg, rec)
        # No restore, no patch write, no state advance: the record is still CLASSIFIED
        # and the tree is untouched.
        self.assertEqual(worktree.snapshot(self.wt), before)
        self.assertFalse((rec.path / "diff.patch").exists())
        reloaded = attempt.load(rec.path)
        self.assertEqual(reloaded.status, "CLASSIFIED")
        self.assertEqual(reloaded.base_tree, base_tree)

    def test_finalize_requires_observed_tree_even_for_restore_outcomes(self):
        rec = self._to_classified("rejected")
        rec = attempt.set_flags(rec, observed_tree=None)
        before = worktree.snapshot(self.wt)
        with self.assertRaises(reconcile.FinalizeFailed):
            reconcile.finalize(self.cfg, rec)
        self.assertEqual(worktree.snapshot(self.wt), before)


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

    def test_block_on_paused_ticket_unwinds_to_previous_then_blocks(self):
        tdir = self.cfg.ticket_dir("TICK-1")
        t = state.load(tdir)
        t = state.transition(t, "paused", reason="env down")
        state.save(tdir, t)
        self.assertEqual(t.previous, "queued")

        rec = self._to_classified("blocked", next_action="block")
        rec = reconcile.finalize(self.cfg, rec)
        rec2 = reconcile.project_lifecycle(self.cfg, rec)

        t2 = state.load(tdir)
        self.assertEqual(t2.state, "blocked")
        self.assertTrue(rec2.lifecycle)

    def test_pause_env_on_blocked_ticket_is_noop(self):
        tdir = self.cfg.ticket_dir("TICK-1")
        t = state.load(tdir)
        t = state.transition(t, "blocked", reason="boom")
        state.save(tdir, t)

        rec = self._to_classified("environment", next_action="pause-env")
        rec = reconcile.finalize(self.cfg, rec)
        rec2 = reconcile.project_lifecycle(self.cfg, rec)

        t2 = state.load(tdir)
        self.assertEqual(t2.state, "blocked")
        self.assertTrue(rec2.lifecycle)

    def test_pause_env_on_already_paused_ticket_is_noop(self):
        tdir = self.cfg.ticket_dir("TICK-1")
        t = state.load(tdir)
        t = state.transition(t, "paused", reason="env down")
        state.save(tdir, t)

        rec = self._to_classified("environment", next_action="fence")
        rec = reconcile.finalize(self.cfg, rec)
        reconcile.project_lifecycle(self.cfg, rec)

        t2 = state.load(tdir)
        self.assertEqual(t2.state, "paused")
        self.assertEqual(t2.previous, "queued")


class TicketKeyTests(unittest.TestCase):
    def _rec(self, lineage):
        return attempt.Record(
            attempt_id=f"x@{lineage}/1", lineage=lineage, generation="g", n=1,
            status="CLASSIFIED", worktree="/tmp/wt", repo_id="r", base_tree="b")

    def test_ticket_key_splits_on_last_slash(self):
        rec = self._rec("team/TICK/001")
        self.assertEqual(reconcile._ticket_key(rec), "team/TICK")

    def test_task_id_is_last_component(self):
        rec = self._rec("team/TICK/001")
        self.assertEqual(reconcile._task_id(rec), "001")

    def test_simple_lineage_unaffected(self):
        rec = self._rec("TICK-1/001")
        self.assertEqual(reconcile._ticket_key(rec), "TICK-1")
        self.assertEqual(reconcile._task_id(rec), "001")


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


def spawn_sleep():
    """A real process group to classify/kill against: `procid.capture` requires an actual
    live pid, and `os.getpgid` needs the leader of its own session group. A background
    reaper thread stands in for what a real runner crash + reparenting to init would do:
    without it, this test process (the direct parent) would leave a zombie behind after
    killing the group, and `killpg(pgid, 0)` reports a zombie as still "alive"."""
    p = subprocess.Popen(["sleep", "60"], start_new_session=True)
    time.sleep(0.1)
    threading.Thread(target=p.wait, daemon=True).start()
    return p, procid.capture(p.pid)


def reap(p):
    try:
        os.killpg(os.getpgid(p.pid), 9)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        p.wait(timeout=2)
    except Exception:
        pass


class ReconcilePart2TestCase(ReconcileTestCase):
    """Adds the hand-building helpers reconcile part 2's tests need: forcing an in-memory
    record onto disk at an arbitrary status (bypassing the CAS check, simulating "this
    attempt was found already sitting in state X on disk"), and a second ticket/worktree
    pair for the global-sweep tests."""

    def _persist(self, rec):
        (rec.path / "attempt.json").write_text(attempt.to_json(rec))

    def _create_in(self, ticket_key, wt, n=1, arm="cloud"):
        rung = ladder.Rung(f"{arm}-worker", "cheap", 1)
        return attempt.create(self.cfg, ticket_key, self.task, wt, n,
                              FakeAgent(), arm, rung, "run-abc")


class GlobalFenceCheckTests(ReconcilePart2TestCase):
    def test_live_runner_lease_raises_fence_exit(self):
        held = locks.Lease(self.cfg.state_root / "locks" / "runner", "runner")
        self.assertTrue(held.acquire(hold=True))
        try:
            with self.assertRaises(reconcile.FenceExit) as cm:
                reconcile.reconcile(self.cfg, "run-x")
            self.assertEqual(cm.exception.code, 3)
        finally:
            held.release()

    def test_fencing_record_with_no_fence_file_raises_fence_exit(self):
        rec = self._create()
        rec = attempt.transition(rec, "FENCING")
        with self.assertRaises(reconcile.FenceExit) as cm:
            reconcile.reconcile(self.cfg, "run-x")
        self.assertEqual(cm.exception.code, 3)

    def test_unreadable_record_raises_fence_exit(self):
        rec = self._create()
        (rec.path / "attempt.json").write_text("not json{")
        with self.assertRaises(reconcile.FenceExit) as cm:
            reconcile.reconcile(self.cfg, "run-x")
        self.assertEqual(cm.exception.code, 3)

    def test_global_fence_ours_alive_raises_fence_exit(self):
        p, pid = spawn_sleep()
        try:
            rec = self._create()
            rec = attempt.transition(rec, "LAUNCHING")
            rec = attempt.transition(rec, "RUNNING", proc=pid.to_dict())
            rec = attempt.transition(rec, "STAGE_DONE", stages=[])
            rec = attempt.transition(rec, "FENCING")
            rec = attempt.transition(rec, "ORPHANED")
            fence_path = self.cfg.state_root / "locks" / "heavy.fence"
            fence_path.parent.mkdir(parents=True, exist_ok=True)
            fence_path.write_text(json.dumps({"attempt_dir": str(rec.path), "proc": pid.to_dict(),
                                              "reason": "test"}))
            with self.assertRaises(reconcile.FenceExit) as cm:
                reconcile.reconcile(self.cfg, "run-x")
            self.assertEqual(cm.exception.code, 3)
            self.assertTrue(fence_path.exists())
        finally:
            reap(p)

    def test_global_fence_unknown_raises_fence_exit(self):
        rec = self._create()
        rec = attempt.transition(rec, "LAUNCHING")
        rec = attempt.transition(rec, "RUNNING", proc={"boot_id": "other", "pgid": 1, "pid": 1,
                                                       "start_time": "x", "cmd": "y"})
        rec = attempt.transition(rec, "STAGE_DONE", stages=[])
        rec = attempt.transition(rec, "FENCING")
        rec = attempt.transition(rec, "ORPHANED")
        fence_path = self.cfg.state_root / "locks" / "heavy.fence"
        fence_path.parent.mkdir(parents=True, exist_ok=True)
        fence_path.write_text(json.dumps({"attempt_dir": str(rec.path), "proc": rec.proc, "reason": "t"}))
        with self.assertRaises(reconcile.FenceExit) as cm:
            reconcile.reconcile(self.cfg, "run-x")
        self.assertEqual(cm.exception.code, 3)

    def test_global_fence_corrupt_raises_fence_exit(self):
        fence_path = self.cfg.state_root / "locks" / "heavy.fence"
        fence_path.parent.mkdir(parents=True, exist_ok=True)
        fence_path.write_text("not json")
        with self.assertRaises(reconcile.FenceExit) as cm:
            reconcile.reconcile(self.cfg, "run-x")
        self.assertEqual(cm.exception.code, 3)

    def test_global_fence_dead_drives_orphaned_attempt_to_interrupted_and_removes_fence(self):
        rec = self._create(n=1)
        (self.wt / "app" / "junk.rb").write_text("junk\n")
        rec = attempt.transition(rec, "LAUNCHING")
        rec = attempt.transition(rec, "RUNNING", proc=None)
        rec = attempt.transition(rec, "STAGE_DONE", stages=[])
        rec = attempt.transition(rec, "FENCING")
        rec = attempt.transition(rec, "ORPHANED")
        fence_path = self.cfg.state_root / "locks" / "heavy.fence"
        fence_path.parent.mkdir(parents=True, exist_ok=True)
        fence_path.write_text(json.dumps({"attempt_dir": str(rec.path), "proc": None, "reason": "t"}))

        ctx = reconcile.reconcile(self.cfg, "run-x")
        try:
            self.assertFalse(fence_path.exists())
            reloaded = attempt.load(rec.path)
            self.assertEqual(reloaded.status, "INTERRUPTED")
            self.assertEqual(reloaded.tree, "restored")
            self.assertFalse((self.wt / "app" / "junk.rb").exists())
        finally:
            ctx.close()


class SweepTests(ReconcilePart2TestCase):
    def test_ours_alive_that_survives_kill_is_fenced(self):
        p, pid = spawn_sleep()
        try:
            rec = self._create()
            rec = attempt.transition(rec, "LAUNCHING")
            rec = attempt.transition(rec, "RUNNING", proc=pid.to_dict())
            with mock.patch("reconcile.procs_mod.kill_group", return_value=False):
                with self.assertRaises(reconcile.FenceExit) as cm:
                    reconcile.reconcile(self.cfg, "run-x")
            self.assertEqual(cm.exception.code, 3)
            reloaded = attempt.load(rec.path)
            self.assertEqual(reloaded.status, "ORPHANED")
            self.assertTrue((self.cfg.state_root / "locks" / "heavy.fence").exists())
        finally:
            reap(p)

    def test_unknown_proc_is_fenced(self):
        rec = self._create()
        rec = attempt.transition(rec, "LAUNCHING")
        rec = attempt.transition(rec, "RUNNING",
                                 proc={"boot_id": "other-boot", "pgid": 999999, "pid": 999999,
                                       "start_time": "x", "cmd": "y"})
        with self.assertRaises(reconcile.FenceExit) as cm:
            reconcile.reconcile(self.cfg, "run-x")
        self.assertEqual(cm.exception.code, 3)
        reloaded = attempt.load(rec.path)
        self.assertEqual(reloaded.status, "ORPHANED")
        self.assertTrue((self.cfg.state_root / "locks" / "heavy.fence").exists())

    def test_repo_id_mismatch_raises_fence_exit_via_interrupt(self):
        rec = self._create()
        rec = attempt.transition(rec, "LAUNCHING")
        rec = attempt.transition(rec, "RUNNING", proc=None)
        bad = dataclasses_replace_repo_id(rec, "not-the-real-repo-id")
        self._persist(bad)
        with self.assertRaises(reconcile.FenceExit) as cm:
            reconcile.interrupt(self.cfg, bad)
        self.assertEqual(cm.exception.code, 3)

    def test_running_record_with_real_group_is_killed_interrupted_restored_and_projected(self):
        p, pid = spawn_sleep()
        try:
            rec = self._create()
            (self.wt / "app" / "worker-junk.rb").write_text("oops\n")
            rec = attempt.transition(rec, "LAUNCHING")
            rec = attempt.transition(rec, "RUNNING", proc=pid.to_dict())

            ctx = reconcile.reconcile(self.cfg, "run-x")
            try:
                self.assertFalse((self.wt / "app" / "worker-junk.rb").exists())
                reloaded = attempt.load(rec.path)
                self.assertEqual(reloaded.status, "INTERRUPTED")
                self.assertEqual(reloaded.tree, "restored")
                self.assertEqual(reloaded.outcome, "interrupted")
                self.assertTrue(reloaded.history and reloaded.published and reloaded.lifecycle)
            finally:
                ctx.close()
            self.assertFalse(reconcile.procid_mod.classify(pid) == "ours-alive")
        finally:
            reap(p)

    def test_classified_rejected_is_finalized_and_projected_by_sweep(self):
        rec = self._to_classified(
            "rejected",
            after_create=lambda: (self.wt / "app" / "junk.rb").write_text("x\n"))
        ctx = reconcile.reconcile(self.cfg, "run-x")
        try:
            reloaded = attempt.load(rec.path)
            self.assertEqual(reloaded.status, "PROJECTED")
            self.assertEqual(reloaded.tree, "restored")
            self.assertFalse((self.wt / "app" / "junk.rb").exists())
        finally:
            ctx.close()

    def test_orphaned_with_fence_deleted_by_hand_is_refenced(self):
        p, pid = spawn_sleep()
        try:
            rec = self._create()
            rec = attempt.transition(rec, "LAUNCHING")
            rec = attempt.transition(rec, "RUNNING", proc=pid.to_dict())
            rec = attempt.transition(rec, "STAGE_DONE", stages=[])
            rec = attempt.transition(rec, "FENCING")
            rec = attempt.transition(rec, "ORPHANED")
            # No fence file on disk: an operator deleted it (or a crash lost it) while the
            # group is still alive.
            with self.assertRaises(reconcile.FenceExit) as cm:
                reconcile.reconcile(self.cfg, "run-x")
            self.assertEqual(cm.exception.code, 3)
            reloaded = attempt.load(rec.path)
            self.assertEqual(reloaded.status, "ORPHANED")
            self.assertTrue((self.cfg.state_root / "locks" / "heavy.fence").exists())
        finally:
            reap(p)

    def test_orphaned_and_now_dead_becomes_interrupted_with_no_fence_present(self):
        rec = self._create()
        (self.wt / "app" / "junk.rb").write_text("x\n")
        rec = attempt.transition(rec, "LAUNCHING")
        rec = attempt.transition(rec, "RUNNING", proc=None)
        rec = attempt.transition(rec, "STAGE_DONE", stages=[])
        rec = attempt.transition(rec, "FENCING")
        rec = attempt.transition(rec, "ORPHANED")

        ctx = reconcile.reconcile(self.cfg, "run-x")
        try:
            reloaded = attempt.load(rec.path)
            self.assertEqual(reloaded.status, "INTERRUPTED")
            self.assertFalse((self.cfg.state_root / "locks" / "heavy.fence").exists())
            self.assertFalse((self.wt / "app" / "junk.rb").exists())
        finally:
            ctx.close()


class TwoTicketGlobalSweepTests(ReconcilePart2TestCase):
    def setUp(self):
        super().setUp()
        self.repo_tmp2 = tempfile.TemporaryDirectory()
        self.wt2 = make_repo(self.repo_tmp2.name)

    def tearDown(self):
        self.repo_tmp2.cleanup()
        super().tearDown()

    def test_two_interrupted_records_in_different_tickets_both_projected_in_one_call(self):
        rec1 = self._create_in("TICK-1", self.wt, n=1)
        rec1 = attempt.transition(rec1, "LAUNCHING")
        rec1 = attempt.transition(rec1, "RUNNING", proc=None)
        rec1 = attempt.transition(rec1, "INTERRUPTED", observed_tree=rec1.base_tree,
                                  outcome="interrupted", next_action="none", tree="restored")

        rec2 = self._create_in("TICK-2", self.wt2, n=1)
        rec2 = attempt.transition(rec2, "LAUNCHING")
        rec2 = attempt.transition(rec2, "RUNNING", proc=None)
        rec2 = attempt.transition(rec2, "INTERRUPTED", observed_tree=rec2.base_tree,
                                  outcome="interrupted", next_action="none", tree="restored")

        self.assertFalse(rec1.history or rec1.published or rec1.lifecycle)
        self.assertFalse(rec2.history or rec2.published or rec2.lifecycle)

        ctx = reconcile.reconcile(self.cfg, "run-x")
        try:
            r1 = attempt.load(rec1.path)
            r2 = attempt.load(rec2.path)
            # INTERRUPTED is terminal on its own (per the design's state list); projecting
            # it sets the three flags but never transitions it to PROJECTED (there is no
            # INTERRUPTED -> PROJECTED edge -- only FINALIZED -> PROJECTED).
            self.assertEqual(r1.status, "INTERRUPTED")
            self.assertEqual(r2.status, "INTERRUPTED")
            self.assertTrue(r1.history and r1.published and r1.lifecycle)
            self.assertTrue(r2.history and r2.published and r2.lifecycle)
            t1 = state.load(self.cfg.ticket_dir("TICK-1"))
            t2 = state.load(self.cfg.ticket_dir("TICK-2"))
            self.assertEqual(len(t1.attempts[rec1.lineage]), 1)
            self.assertEqual(len(t2.attempts[rec2.lineage]), 1)
        finally:
            ctx.close()


def dataclasses_replace_repo_id(rec, repo_id):
    import dataclasses
    new = dataclasses.replace(rec, repo_id=repo_id)
    new.path = rec.path
    return new


if __name__ == "__main__":
    unittest.main()
