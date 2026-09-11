import contextlib
import io
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


HERE = pathlib.Path(__file__).resolve().parent


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


class FenceCarriesExplicitProcTests(ReconcilePart2TestCase):
    """Fix round 1, finding 2: `_attempt` transitions a stage to STAGE_DONE with `proc=None`
    (the record's own `proc` field is cleared once a stage completes; the stage's own proc
    dict lives on the stage entry) before ever calling fence() on an unverified termination.
    Without an explicit `proc` argument, fence() would serialize `rec.proc` -- already null
    at that point -- and the next reconcile() would treat the fence as corrupt ("proc must
    be a dict, not null") forever, instead of fencing the actual process group."""

    def test_fence_with_explicit_proc_writes_it_instead_of_rec_proc(self):
        stage_proc = {"boot_id": "other-boot", "pgid": 424242, "pid": 424242,
                     "start_time": "x", "cmd": "y"}
        rec = self._create()
        rec = attempt.transition(rec, "LAUNCHING",
                                 stages=[{"kind": "worker", "idx": 0, "proc": None}])
        rec = attempt.transition(rec, "RUNNING", proc=None)
        stages = [{"kind": "worker", "idx": 0, "proc": stage_proc, "terminated": False,
                  "timed_out": False, "rc": None, "elapsed_s": 1.0}]
        rec = attempt.transition(rec, "STAGE_DONE", stages=stages, proc=None)
        self.assertIsNone(rec.proc)   # the record's own proc is null, matching _attempt's flow

        with self.assertRaises(reconcile.FenceExit):
            reconcile.fence(self.cfg, rec, "termination unverified pgid 424242", proc=stage_proc)

        fence_path = self.cfg.state_root / "locks" / "heavy.fence"
        data = json.loads(fence_path.read_text())
        self.assertEqual(data["proc"], stage_proc)
        self.assertIsNotNone(data["proc"])

        # A subsequent reconcile() classifies the (unreachable, foreign-boot-id) proc as
        # "unknown" -- NOT corrupt -- and exits 3 for that reason, never silently resolving.
        with self.assertRaises(reconcile.FenceExit) as cm:
            reconcile.reconcile(self.cfg, "run-y")
        self.assertEqual(cm.exception.code, 3)
        self.assertNotIn("corrupt", cm.exception.reason)
        self.assertIn("unknown", cm.exception.reason)

    def test_fence_without_explicit_proc_falls_back_to_rec_proc(self):
        rec = self._create()
        rec = attempt.transition(rec, "LAUNCHING")
        rec = attempt.transition(rec, "RUNNING",
                                 proc={"boot_id": "other-boot", "pgid": 1, "pid": 1,
                                       "start_time": "x", "cmd": "y"})
        rec = attempt.transition(rec, "STAGE_DONE", stages=[])
        with self.assertRaises(reconcile.FenceExit):
            reconcile.fence(self.cfg, rec, "no explicit proc given")
        fence_path = self.cfg.state_root / "locks" / "heavy.fence"
        data = json.loads(fence_path.read_text())
        self.assertEqual(data["proc"], rec.proc)


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
        # The fence schema requires a real (non-null) proc dict (C2): a genuinely dead proc
        # is one that classifies as dead, not one that's simply absent from the fence file.
        dead_proc = {"boot_id": locks.boot_id(), "pgid": 999999, "pid": 999999,
                     "start_time": "x", "cmd": "y"}
        fence_path.write_text(json.dumps({"attempt_dir": str(rec.path), "proc": dead_proc, "reason": "t"}))

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


class SweepWalksLeavesTests(ReconcilePart2TestCase):
    """C1: the sweep walks the attempts tree looking for leaf dirs (any all-digit dir at
    least two levels below attempts_root) rather than globbing a fixed `*/*/*` depth, so it
    catches both a leaf created before attempt.json existed and a ticket key containing
    `/`."""

    def test_leaf_dir_without_attempt_json_raises_fence_exit(self):
        leaf = self.cfg.state_root / "attempts" / "T" / "001" / "1"
        leaf.mkdir(parents=True)
        with self.assertRaises(reconcile.FenceExit) as cm:
            reconcile.reconcile(self.cfg, "run-x")
        self.assertEqual(cm.exception.code, 3)

    def test_ticket_key_with_slash_is_swept(self):
        rec = self._create_in("team/TICK", self.wt, n=1)
        rec = attempt.transition(rec, "LAUNCHING")
        rec = attempt.transition(rec, "RUNNING", proc=None)
        ctx = reconcile.reconcile(self.cfg, "run-x")
        try:
            reloaded = attempt.load(rec.path)
            self.assertEqual(reloaded.status, "INTERRUPTED")
        finally:
            ctx.close()


class CorruptFenceSchemaTests(ReconcilePart2TestCase):
    """C2: the fence file must have `attempt_dir`, `proc` (a non-null dict), and `reason`;
    a fence missing `proc` (or with a null `proc`) must never be silently classified as
    `dead` and used to restore a possibly-still-live group."""

    def _write_fence(self, payload):
        fence_path = self.cfg.state_root / "locks" / "heavy.fence"
        fence_path.parent.mkdir(parents=True, exist_ok=True)
        fence_path.write_text(json.dumps(payload))
        return fence_path

    def test_fence_missing_proc_key_raises_fence_exit(self):
        rec = self._create()
        self._write_fence({"attempt_dir": str(rec.path), "reason": "t"})
        with self.assertRaises(reconcile.FenceExit) as cm:
            reconcile.reconcile(self.cfg, "run-x")
        self.assertEqual(cm.exception.code, 3)

    def test_fence_with_null_proc_raises_fence_exit(self):
        rec = self._create()
        self._write_fence({"attempt_dir": str(rec.path), "proc": None, "reason": "t"})
        with self.assertRaises(reconcile.FenceExit) as cm:
            reconcile.reconcile(self.cfg, "run-x")
        self.assertEqual(cm.exception.code, 3)

    def test_fence_missing_attempt_dir_key_raises_fence_exit(self):
        self._write_fence({"proc": {"boot_id": "x", "pgid": 1, "pid": 1, "start_time": "x", "cmd": "y"},
                          "reason": "t"})
        with self.assertRaises(reconcile.FenceExit) as cm:
            reconcile.reconcile(self.cfg, "run-x")
        self.assertEqual(cm.exception.code, 3)

    def test_fence_missing_reason_key_raises_fence_exit(self):
        rec = self._create()
        self._write_fence({"attempt_dir": str(rec.path),
                          "proc": {"boot_id": "x", "pgid": 1, "pid": 1, "start_time": "x", "cmd": "y"}})
        with self.assertRaises(reconcile.FenceExit) as cm:
            reconcile.reconcile(self.cfg, "run-x")
        self.assertEqual(cm.exception.code, 3)

    def test_fence_attempt_dir_escapes_attempts_root_raises_fence_exit(self):
        """A fence whose attempt_dir resolves outside state_root/attempts is corrupt."""
        # Test both absolute path and relative escape
        cases = {
            "absolute_etc": "/etc/passwd",
            "relative_escape": "../../etc/passwd",
        }
        for name, escape_path in cases.items():
            with self.subTest(case=name):
                self._write_fence({
                    "attempt_dir": escape_path,
                    "proc": {"boot_id": "x", "pgid": 1, "pid": 1, "start_time": "x", "cmd": "y"},
                    "reason": "t"
                })
                with self.assertRaises(reconcile.FenceExit) as cm:
                    reconcile.reconcile(self.cfg, "run-x")
                self.assertEqual(cm.exception.code, 3)
                self.assertIn("corrupt", cm.exception.reason.lower())
                # Clean up the fence file for the next subtest
                fence_path = self.cfg.state_root / "locks" / "heavy.fence"
                fence_path.unlink()

    def test_clear_fence_with_escaped_attempt_dir_requires_force(self):
        """clear_fence() treats escaped attempt_dir as corrupt and needs --force."""
        self._write_fence({
            "attempt_dir": "../../etc/passwd",
            "proc": {"boot_id": "x", "pgid": 1, "pid": 1, "start_time": "x", "cmd": "y"},
            "reason": "t"
        })
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = reconcile.clear_fence(self.cfg, force=False)
        self.assertEqual(rc, 3)
        self.assertIn("corrupt", buf.getvalue().lower())
        fence_path = self.cfg.state_root / "locks" / "heavy.fence"
        self.assertTrue(fence_path.exists())
        # With --force, it clears the fence
        rc = reconcile.clear_fence(self.cfg, force=True)
        self.assertEqual(rc, 0)
        self.assertFalse(fence_path.exists())


class UnknownStatusDefenseTests(ReconcilePart2TestCase):
    """C3: attempt.from_json validates status in STATES; reconcile also fails closed if it
    is ever handed a record whose status it has no dispatch branch for."""

    def test_unknown_status_in_record_raises_fence_exit_via_unreadable(self):
        rec = self._create()
        raw = json.loads((rec.path / "attempt.json").read_text())
        raw["status"] = "NOT_A_REAL_STATUS"
        (rec.path / "attempt.json").write_text(json.dumps(raw))
        with self.assertRaises(reconcile.FenceExit) as cm:
            reconcile.reconcile(self.cfg, "run-x")
        self.assertEqual(cm.exception.code, 3)

    def test_reconcile_one_defense_in_depth_raises_fence_exit_for_unhandled_status(self):
        rec = self._create()
        rec.status = "NOT_A_REAL_STATUS"
        with self.assertRaises(reconcile.FenceExit):
            reconcile._reconcile_one(self.cfg, rec)


class DeadFenceReferencingInterruptedTests(ReconcilePart2TestCase):
    """I1: a dead fence whose referenced attempt is already INTERRUPTED (a crash between the
    INTERRUPTED write and the fence unlink) must be accepted idempotently, not rejected."""

    def test_dead_fence_referencing_already_interrupted_attempt_is_idempotent(self):
        rec = self._create()
        rec = attempt.transition(rec, "LAUNCHING")
        rec = attempt.transition(rec, "RUNNING", proc=None)
        rec = attempt.transition(rec, "INTERRUPTED", observed_tree=rec.base_tree,
                                 outcome="interrupted", next_action="none", tree="restored")
        fence_path = self.cfg.state_root / "locks" / "heavy.fence"
        fence_path.parent.mkdir(parents=True, exist_ok=True)
        dead_proc = {"boot_id": locks.boot_id(), "pgid": 999999, "pid": 999999,
                    "start_time": "x", "cmd": "y"}
        fence_path.write_text(json.dumps({"attempt_dir": str(rec.path), "proc": dead_proc, "reason": "t"}))

        ctx = reconcile.reconcile(self.cfg, "run-x")
        try:
            self.assertFalse(fence_path.exists())
            reloaded = attempt.load(rec.path)
            self.assertEqual(reloaded.status, "INTERRUPTED")
            self.assertTrue(reloaded.history and reloaded.published and reloaded.lifecycle)
        finally:
            ctx.close()


class FenceWriteFailureTests(ReconcilePart2TestCase):
    """I2: a failure writing the fence file after the FENCING transition must not propagate
    as a raw OSError -- the FENCING record already guarantees the next run exits 3."""

    def test_fence_write_failure_raises_fence_exit_and_leaves_fencing(self):
        rec = self._create()
        rec = attempt.transition(rec, "LAUNCHING")
        rec = attempt.transition(rec, "RUNNING",
                                 proc={"boot_id": "other-boot", "pgid": 999999, "pid": 999999,
                                       "start_time": "x", "cmd": "y"})
        # Only the fence-file write fails -- the FENCING transition itself (which also
        # goes through safe_write, via safe_rewrite's temp file) must still succeed, so the
        # test isolates the failure to `locks/heavy.fence` specifically.
        original_safe_write = attempt.safe_write

        def fake_safe_write(path, text):
            if pathlib.Path(path).name == "heavy.fence":
                raise OSError("disk full")
            return original_safe_write(path, text)

        with mock.patch.object(attempt, "safe_write", side_effect=fake_safe_write):
            with self.assertRaises(reconcile.FenceExit) as cm:
                reconcile.reconcile(self.cfg, "run-x")
        self.assertEqual(cm.exception.code, 3)
        reloaded = attempt.load(rec.path, validate_worktree=False)
        self.assertEqual(reloaded.status, "FENCING")


class ObservedTreeMissingTests(ReconcilePart2TestCase):
    """O1: an INTERRUPTED record with observed_tree=None (legacy, or hand-built test) and
    no diff.patch cannot be recovered -- the patch cannot be regenerated safely without a
    snapshot of the pre-restore tree. The sweep exits 3 with reason in the FenceExit."""

    def test_interrupted_with_none_observed_tree_and_no_diff_patch_raises_fence_exit(self):
        rec = self._create()
        rec = attempt.transition(rec, "LAUNCHING")
        rec = attempt.transition(rec, "RUNNING", proc=None)
        # Hand-build an INTERRUPTED record with observed_tree=None and no diff.patch,
        # simulating a legacy record or a test record that was not properly set up.
        rec = attempt.transition(rec, "INTERRUPTED", observed_tree=None,
                                 outcome="interrupted", next_action="none", tree="restored")
        self.assertIsNone(rec.observed_tree)
        self.assertFalse((rec.path / "diff.patch").exists())

        with self.assertRaises(reconcile.FenceExit) as cm:
            reconcile.reconcile(self.cfg, "run-x")
        self.assertEqual(cm.exception.code, 3)
        self.assertIn("cannot regenerate diff.patch", cm.exception.reason)
        self.assertIn("observed_tree missing", cm.exception.reason)
        self.assertIn(rec.attempt_id, cm.exception.reason)


class InterruptWritesDiffPatchTests(ReconcilePart2TestCase):
    """I3: interrupt() writes diff.patch capturing the pre-restore tree, and the sweep
    regenerates it for any INTERRUPTED record found without one."""

    def test_interrupt_via_sweep_writes_diff_patch_and_restores_tree(self):
        p, pid = spawn_sleep()
        try:
            rec = self._create()
            (self.wt / "app" / "bin").mkdir()
            (self.wt / "app" / "bin" / "oops").write_text("oops\n")
            rec = attempt.transition(rec, "LAUNCHING")
            rec = attempt.transition(rec, "RUNNING", proc=pid.to_dict())

            ctx = reconcile.reconcile(self.cfg, "run-x")
            try:
                self.assertFalse((self.wt / "app" / "bin" / "oops").exists())
                reloaded = attempt.load(rec.path)
                self.assertEqual(reloaded.status, "INTERRUPTED")
                patch_path = rec.path / "diff.patch"
                self.assertTrue(patch_path.exists())
                self.assertIn("bin/oops", patch_path.read_text())
            finally:
                ctx.close()
        finally:
            reap(p)

    def test_sweep_regenerates_missing_diff_patch_for_interrupted_record(self):
        rec = self._create()
        (self.wt / "app" / "junk.rb").write_text("junk\n")
        observed = worktree.snapshot(self.wt)
        worktree.restore(self.wt, rec.base_tree)
        rec = attempt.transition(rec, "LAUNCHING")
        rec = attempt.transition(rec, "RUNNING", proc=None)
        rec = attempt.transition(rec, "INTERRUPTED", observed_tree=observed,
                                 outcome="interrupted", next_action="none", tree="restored")
        self.assertFalse((rec.path / "diff.patch").exists())

        ctx = reconcile.reconcile(self.cfg, "run-x")
        try:
            patch_path = rec.path / "diff.patch"
            self.assertTrue(patch_path.exists())
            self.assertIn("junk.rb", patch_path.read_text())
        finally:
            ctx.close()


class TimeoutRecoveryTaskReadTests(ReconcilePart2TestCase):
    """I4: the timeout-recovery path reads task.toml through attempt.safe_read + tomllib,
    never a raw open() that would follow a symlink."""

    def test_symlinked_task_toml_raises_fence_exit(self):
        rec = self._create()
        (self.wt / "app" / "b.rb").write_text("b\n")
        rec = attempt.transition(rec, "LAUNCHING")
        rec = attempt.transition(rec, "RUNNING", proc=None)
        stages = [{"kind": "worker", "idx": 0, "proc": None, "terminated": True,
                  "timed_out": True, "rc": None, "elapsed_s": 999.0}]
        rec = attempt.transition(rec, "STAGE_DONE", stages=stages)

        real_toml = rec.path / "task.toml"
        text = real_toml.read_text()
        real_toml.unlink()
        target = rec.path / "task.toml.real"
        target.write_text(text)
        real_toml.symlink_to(target)

        with self.assertRaises(reconcile.FenceExit) as cm:
            reconcile.reconcile(self.cfg, "run-x")
        self.assertEqual(cm.exception.code, 3)


class StageDoneTimedOutCleanTests(ReconcilePart2TestCase):
    """M1: a STAGE_DONE record whose last stage timed out with a clean allowed edit is
    classified `timeout` by the sweep and finalized with the tree KEPT (not restored)."""

    def test_timed_out_clean_edit_is_classified_finalized_and_projected_with_tree_kept(self):
        rec = self._create()
        (self.wt / "app" / "b.rb").write_text("b\n")
        rec = attempt.transition(rec, "LAUNCHING")
        rec = attempt.transition(rec, "RUNNING", proc=None)
        stages = [{"kind": "worker", "idx": 0, "proc": None, "terminated": True,
                  "timed_out": True, "rc": None, "elapsed_s": 999.0}]
        rec = attempt.transition(rec, "STAGE_DONE", stages=stages)

        ctx = reconcile.reconcile(self.cfg, "run-x")
        try:
            reloaded = attempt.load(rec.path)
            self.assertEqual(reloaded.status, "PROJECTED")
            self.assertEqual(reloaded.outcome, "timeout")
            self.assertEqual(reloaded.tree, "kept")
            self.assertTrue((self.wt / "app" / "b.rb").exists())
            self.assertTrue(reloaded.history and reloaded.published and reloaded.lifecycle)

            t = state.load(self.cfg.ticket_dir("TICK-1"))
            self.assertEqual(len(t.attempts[rec.lineage]), 1)
            self.assertEqual(t.attempts[rec.lineage][0]["outcome"], "timeout")

            rows = metrics.read_all(self.cfg.state_root)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["stage_kind"], "worker")
        finally:
            ctx.close()


def dataclasses_replace_repo_id(rec, repo_id):
    import dataclasses
    new = dataclasses.replace(rec, repo_id=repo_id)
    new.path = rec.path
    return new


class ClearFenceTests(ReconcilePart2TestCase):
    """`reconcile.clear_fence` -- the operator-facing `clear-fence` CLI's implementation.
    See the design's "## `clear-fence`" section. The CLI's own routing to this function is
    covered by test_runner.py's test_clear_fence_cli_dead_fence_interrupts_via_reconcile."""

    def test_refuses_while_runner_lease_held_by_another_process(self):
        lease_path = self.cfg.state_root / "locks" / "runner"
        lease_path.parent.mkdir(parents=True, exist_ok=True)
        script = (
            f"import sys, time\n"
            f"sys.path.insert(0, {str(HERE)!r})\n"
            f"import locks\n"
            f"lease = locks.Lease({str(lease_path)!r}, 'runner')\n"
            f"assert lease.acquire(hold=True)\n"
            f"print('ready', flush=True)\n"
            f"time.sleep(30)\n"
        )
        proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
        try:
            line = proc.stdout.readline()
            self.assertEqual(line.strip(), "ready")
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                rc = reconcile.clear_fence(self.cfg, force=False)
            self.assertEqual(rc, 3)
            self.assertIn(str(proc.pid), buf.getvalue())
            self.assertIn("runner live", buf.getvalue())
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def test_dead_fence_produced_by_fence_interrupts_attempt_and_removes_fence(self):
        rec = self._create()
        (self.wt / "app" / "junk.rb").write_text("junk\n")
        rec = attempt.transition(rec, "LAUNCHING")
        rec = attempt.transition(rec, "RUNNING", proc=None)
        stages = [{"kind": "worker", "idx": 0, "proc": None, "terminated": False,
                  "timed_out": False, "rc": None, "elapsed_s": 1.0}]
        rec = attempt.transition(rec, "STAGE_DONE", stages=stages, proc=None)
        dead_proc = {"boot_id": locks.boot_id(), "pgid": 999999, "pid": 999999,
                    "start_time": "x", "cmd": "y"}
        with self.assertRaises(reconcile.FenceExit):
            reconcile.fence(self.cfg, rec, "termination unverified", proc=dead_proc)

        fence_path = self.cfg.state_root / "locks" / "heavy.fence"
        self.assertTrue(fence_path.exists())
        rc = reconcile.clear_fence(self.cfg, force=False)
        self.assertEqual(rc, 0)
        self.assertFalse(fence_path.exists())
        reloaded = attempt.load(rec.path)
        self.assertEqual(reloaded.status, "INTERRUPTED")
        self.assertTrue(reloaded.history and reloaded.published and reloaded.lifecycle)
        self.assertFalse((self.wt / "app" / "junk.rb").exists())

    def test_force_on_live_group_marks_operator_forced_and_does_not_restore(self):
        p, pid = spawn_sleep()
        try:
            rec = self._create()
            (self.wt / "app" / "live_junk.rb").write_text("junk\n")
            rec = attempt.transition(rec, "LAUNCHING")
            rec = attempt.transition(rec, "RUNNING", proc=pid.to_dict())
            stages = [{"kind": "worker", "idx": 0, "proc": pid.to_dict(), "terminated": False,
                      "timed_out": False, "rc": None, "elapsed_s": 1.0}]
            rec = attempt.transition(rec, "STAGE_DONE", stages=stages, proc=None)
            with self.assertRaises(reconcile.FenceExit):
                reconcile.fence(self.cfg, rec, "termination unverified", proc=pid.to_dict())

            fence_path = self.cfg.state_root / "locks" / "heavy.fence"
            self.assertTrue(fence_path.exists())

            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                rc = reconcile.clear_fence(self.cfg, force=False)
            self.assertEqual(rc, 3)
            self.assertTrue(fence_path.exists())

            rc = reconcile.clear_fence(self.cfg, force=True)
            self.assertEqual(rc, 0)
            self.assertFalse(fence_path.exists())
            reloaded = attempt.load(rec.path)
            self.assertEqual(reloaded.status, "ORPHANED")
            self.assertTrue(reloaded.operator_forced)
            self.assertTrue((self.wt / "app" / "live_junk.rb").exists())    # NOT restored

            # Next runner start: the group is still alive, no fence on disk -> re-fenced.
            with self.assertRaises(reconcile.FenceExit) as cm:
                reconcile.reconcile(self.cfg, "run-y")
            self.assertEqual(cm.exception.code, 3)
            self.assertTrue(fence_path.exists())
            reloaded2 = attempt.load(rec.path)
            self.assertEqual(reloaded2.status, "ORPHANED")
            self.assertTrue((self.wt / "app" / "live_junk.rb").exists())   # still not restored
        finally:
            reap(p)

    def test_corrupt_fence_requires_force(self):
        fence_path = self.cfg.state_root / "locks" / "heavy.fence"
        fence_path.parent.mkdir(parents=True, exist_ok=True)
        fence_path.write_text("not json{")

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = reconcile.clear_fence(self.cfg, force=False)
        self.assertEqual(rc, 3)
        self.assertTrue(fence_path.exists())
        self.assertIn("corrupt fence", buf.getvalue())

        rc = reconcile.clear_fence(self.cfg, force=True)
        self.assertEqual(rc, 0)
        self.assertFalse(fence_path.exists())

    def test_no_fence_present_is_a_no_op(self):
        rc = reconcile.clear_fence(self.cfg, force=False)
        self.assertEqual(rc, 0)

    def _write_fence(self, payload: dict) -> pathlib.Path:
        fence_path = self.cfg.state_root / "locks" / "heavy.fence"
        fence_path.parent.mkdir(parents=True, exist_ok=True)
        fence_path.write_text(json.dumps(payload))
        return fence_path

    def _valid_proc(self) -> dict:
        return {"boot_id": locks.boot_id(), "pgid": 999999, "pid": 999999,
                "start_time": "x", "cmd": "y"}

    def test_malformed_fence_fields_are_corrupt_not_a_crash(self):
        rec = self._create()
        cases = {
            "attempt_dir_null": {"attempt_dir": None, "proc": self._valid_proc(), "reason": "r"},
            "attempt_dir_numeric": {"attempt_dir": 5, "proc": self._valid_proc(), "reason": "r"},
            "proc_bad_pgid_type": {"attempt_dir": str(rec.path),
                                   "proc": {**self._valid_proc(), "pgid": "x"}, "reason": "r"},
            "proc_not_a_dict": {"attempt_dir": str(rec.path), "proc": 7, "reason": "r"},
        }
        for name, payload in cases.items():
            with self.subTest(name=name):
                fence_path = self._write_fence(payload)

                buf = io.StringIO()
                with contextlib.redirect_stderr(buf):
                    rc = reconcile.clear_fence(self.cfg, force=False)
                self.assertEqual(rc, 3)
                self.assertTrue(fence_path.exists())
                self.assertIn("corrupt fence", buf.getvalue())

                rc = reconcile.clear_fence(self.cfg, force=True)
                self.assertEqual(rc, 0)
                self.assertFalse(fence_path.exists())

    def test_force_promotes_fencing_record_to_orphaned_and_re_fences_next_run(self):
        p, pid = spawn_sleep()
        try:
            rec = self._create()
            (self.wt / "app" / "live_junk.rb").write_text("junk\n")
            rec = attempt.transition(rec, "LAUNCHING")
            rec = attempt.transition(rec, "RUNNING", proc=pid.to_dict())
            stages = [{"kind": "worker", "idx": 0, "proc": pid.to_dict(), "terminated": False,
                      "timed_out": False, "rc": None, "elapsed_s": 1.0}]
            rec = attempt.transition(rec, "STAGE_DONE", stages=stages, proc=None)
            # Hand-simulate the crash window inside fence(): FENCING was written and the
            # fence file exists, but the ORPHANED transition never happened.
            rec = attempt.transition(rec, "FENCING")
            fence_path = self.cfg.state_root / "locks" / "heavy.fence"
            fence_path.parent.mkdir(parents=True, exist_ok=True)
            attempt.safe_write(fence_path, json.dumps(
                {"attempt_dir": str(rec.path), "proc": pid.to_dict(), "reason": "crash test"}))

            rc = reconcile.clear_fence(self.cfg, force=True)
            self.assertEqual(rc, 0)
            self.assertFalse(fence_path.exists())
            reloaded = attempt.load(rec.path)
            self.assertEqual(reloaded.status, "ORPHANED")
            self.assertTrue(reloaded.operator_forced)
            self.assertTrue((self.wt / "app" / "live_junk.rb").exists())    # NOT restored

            # Next runner start: the group is still alive, no fence on disk -> re-fenced.
            with self.assertRaises(reconcile.FenceExit) as cm:
                reconcile.reconcile(self.cfg, "run-z")
            self.assertEqual(cm.exception.code, 3)
            self.assertTrue(fence_path.exists())
            reloaded2 = attempt.load(rec.path)
            self.assertEqual(reloaded2.status, "ORPHANED")
        finally:
            reap(p)


if __name__ == "__main__":
    unittest.main()
