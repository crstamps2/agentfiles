"""Plan 1c Task 9: the crash-window acceptance table. Every row of the design's "Acceptance
criteria (the crash-window table)" (docs/superpowers/specs/2026-09-10-agent-loop-1c-attempt-
lifecycle-design.md) is exactly one `test_cw_NN_<slug>` here, in table order. The table has 40
physical lines (a header + separator + 38 data rows); NN therefore runs 01..38 -- see the task-9
report for the exact count reconciliation.

Technique per row, in order of preference:
  1. The `reconcile._CRASH_HOOK` / `reconcile._maybe_crash` seam (see reconcile.py's docstring
     next to `_CRASH_HOOK`): patch `reconcile._CRASH_HOOK` to a named boundary, drive the real
     runner/reconcile code through that boundary, catch `reconcile.SimulatedCrash`, clear the
     hook, then call `reconcile.reconcile()` (or `reconcile.clear_fence()` / the CLI) and assert
     the post-condition.
  2. A real, killed process group (`start_new_session=True` + `sleep`) for rows that need a live
     group to classify/kill.
  3. A hand-built `attempt.Record` at the exact named status (the same technique test_reconcile.py
     already uses extensively) for rows whose boundary is a status, not an in-flight instruction.
  4. Where none of the above is faithful to the row as literally written, the closest faithful
     test is implemented and the docstring says so explicitly (see task-9-report.md's
     "Infeasible rows" section for the full list and rationale).

Reuses test_runner.RunnerHarness's setUp/tearDown (self.wt, self.cfg, self.launcher, self.tasks,
run_task, tasks_with, extra_env) by subclassing -- see the bottom of this file for how inherited
test_* methods are suppressed so `unittest test_crash_windows` runs only this file's 38 rows.
"""
from __future__ import annotations
import dataclasses
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from unittest.mock import patch

import agentdef
import attempt
import config
import contracts
import ladder
import locks
import metrics
import procid
import procs
import reconcile
import runner
import state
import worktree
import test_runner as _test_runner_mod
from test_runner import TASKS

HERE = pathlib.Path(__file__).resolve().parent

# Deliberately NOT `from test_runner import RunnerHarness`: binding that name at this module's
# top level would make unittest's module test-loader discover and re-run RunnerHarness's own
# tests a second time under `python3 -m unittest test_crash_windows`. `class CrashWindowTests(
# _test_runner_mod.RunnerHarness)` below reuses its setUp/tearDown/helpers without creating a
# second top-level TestCase name in this module.


def _spawn_group(argv=("sleep", "60")):
    """A real, killable process group: start_new_session=True gives it its own pgid; a
    background reaper thread stands in for what a real crash+reparent-to-init would do (without
    it this test process, as the direct parent, would leave a zombie behind after killing the
    group, and killpg(pgid, 0) reports a zombie as still "alive")."""
    p = subprocess.Popen(list(argv), start_new_session=True)
    time.sleep(0.1)
    threading.Thread(target=p.wait, daemon=True).start()
    return p, procid.capture(p.pid)


def _reap(p):
    try:
        os.killpg(os.getpgid(p.pid), 9)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        p.wait(timeout=2)
    except Exception:
        pass


class CrashWindowTests(_test_runner_mod.RunnerHarness):
    """See module docstring. All 38 rows of the design's crash-window table, in table order."""

    def _ticket(self, key="ZIP-7873"):
        t = state.load(self.cfg.ticket_dir(key))
        t.worktree = str(self.wt)
        for s in ("spinup", "plan", "plan-review", "implement"):
            t = state.transition(t, s)
        state.save(self.cfg.ticket_dir(key), t)
        return t

    def _rung(self, agent="cloud-worker", tier="cheap", n=1):
        return ladder.Rung(agent, tier, n)

    def _runner(self, launcher=None):
        return runner.Runner(self.cfg, run_id="test", pi_launcher=launcher or self.launcher)

    def _direct_attempt(self, r, t, task=None, scenario="pass", n=1, arm="cloud", rung=None,
                        attempts=None, env_failures=0):
        self.scenarios = [scenario]
        task = task or self.tasks[0]
        rung = rung or self._rung()
        return r._attempt(t, task, self.wt, arm, rung, n, attempts or [], env_failures)

    def _crash(self, hookname, fn, *a, **kw):
        """Run `fn(*a, **kw)` with `reconcile._CRASH_HOOK` set to `hookname`; asserts
        `reconcile.SimulatedCrash` is raised (the injected "process death") and returns it."""
        with patch("reconcile._CRASH_HOOK", hookname):
            with self.assertRaises(reconcile.SimulatedCrash) as cm:
                fn(*a, **kw)
        return cm.exception

    def _reconcile(self, run_id="recover"):
        ctx = reconcile.reconcile(self.cfg, run_id)
        ctx.close()

    def _adir(self, t, task, n=1):
        return attempt.attempt_dir(self.cfg, t.key, task.id, n)

    # ----- Row 1 -----------------------------------------------------------------------

    def test_cw_01_dir_artifacts_exist_before_created_record(self):
        """dir + artifacts exist, before a valid CREATED record"""
        t = self._ticket()
        task = self.tasks[0]
        adir = self._adir(t, task)
        attempt.safe_mkdir(adir.parent)
        os.mkdir(adir, 0o700)
        attempt.safe_write(adir / "task.toml", attempt.task_toml(task))
        attempt.safe_write(adir / "task.md", attempt.task_md(task, adir, self.wt))
        attempt.safe_write(adir / "base_tree", worktree.snapshot(self.wt))
        # No attempt.json -- the crash happened before the record itself was ever written.
        self.assertFalse((adir / "attempt.json").exists())
        with self.assertRaises(reconcile.FenceExit) as cm:
            self._reconcile()
        self.assertEqual(cm.exception.code, 3)

    # ----- Row 2 -----------------------------------------------------------------------

    def test_cw_02_dir_created_before_launching(self):
        """dir created, before LAUNCHING"""
        t = self._ticket()
        task = self.tasks[0]
        r = self._runner()
        self._crash("after-created", self._direct_attempt, r, t, task, scenario="pass")
        self.assertEqual(self.launches, 0)   # LAUNCHING (and the launch itself) never happened
        self._reconcile()
        rec = attempt.load(self._adir(t, task))
        self.assertEqual(rec.status, "INTERRUPTED")
        self.assertIsNone(rec.proc)
        self.assertTrue(rec.history and rec.published and rec.lifecycle)
        rows_before = len(metrics.read_all(self.cfg.state_root))
        self._reconcile("recover-2")   # row published once: a second reconcile is a no-op
        self.assertEqual(len(metrics.read_all(self.cfg.state_root)), rows_before)

    # ----- Row 3 -----------------------------------------------------------------------

    def test_cw_03_gated_child_forked_before_proc_committed(self):
        """gated child forked, before proc committed"""
        # Exercised directly at procs.run_stage's level (the actual launch gate primitive),
        # which is the most faithful way to observe "child never execs": on_start raising means
        # the gate pipe is never released and the gate shell exits 97 without ever exec'ing the
        # real command -- verified here by the stdout log staying empty (the real command would
        # have printed something) and the process group being provably dead afterward.
        stdout = pathlib.Path(tempfile.mktemp())
        stderr = pathlib.Path(tempfile.mktemp())

        def crashing_on_start(pgid, pid):
            raise RuntimeError("simulated crash: proc never committed to the attempt record")

        with self.assertRaises(RuntimeError):
            procs.run_stage(["/bin/echo", "should-never-print"], self.wt, 5.0, dict(os.environ),
                            stdout, stderr, on_start=crashing_on_start)
        self.assertEqual(stdout.read_bytes(), b"")
        stdout.unlink(missing_ok=True)
        stderr.unlink(missing_ok=True)

        # Fix round 1 (weak-test finding): "child never execs" was only proven indirectly (an
        # empty stdout log); prove it directly by capturing the real gate-shell subprocess and
        # asserting its own exit code is 97 -- the exact code run_stage's docstring promises
        # ("the child's `read` fails and it exits 97"). `run_stage` re-raises the on_start
        # exception instead of returning a StageResult on this path, so there is no
        # `StageResult.returncode` to read; the seam used here is a `subprocess.Popen` spy
        # (captures the real Popen instance run_stage constructs internally) rather than a new
        # production seam, since run_stage's own `finally` block already reaps it before
        # re-raising. Verified experimentally that in production timing `kill_group`'s SIGTERM
        # (sent immediately after the pipe close, in the same `except` block) almost always wins
        # the race against the shell noticing EOF on its own -- so `procs.kill_group` is wrapped
        # here with a small delay purely to let the natural `read`-failure path win instead of
        # being pre-empted by the signal; this changes only which of the two equally-valid "never
        # exec'd" causes is observed in the test, not run_stage's production behavior (kill_group
        # itself, and its default timing, are untouched -- only the call is deferred).
        stdout2 = pathlib.Path(tempfile.mktemp())
        stderr2 = pathlib.Path(tempfile.mktemp())
        captured = {}
        real_popen = subprocess.Popen
        real_kill_group = procs.kill_group

        def spy_popen(*a, **kw):
            p = real_popen(*a, **kw)
            captured["p"] = p
            return p

        def delayed_kill_group(pgid, grace_s=5.0, reap=None):
            time.sleep(0.2)   # let the gate shell's own `read` failure land first
            return real_kill_group(pgid, grace_s=grace_s, reap=reap)

        with mock.patch("subprocess.Popen", side_effect=spy_popen), \
             mock.patch.object(procs, "kill_group", side_effect=delayed_kill_group):
            with self.assertRaises(RuntimeError):
                procs.run_stage(["/bin/echo", "should-never-print"], self.wt, 5.0, dict(os.environ),
                                stdout2, stderr2, on_start=crashing_on_start)
        p = captured["p"]
        p.wait(timeout=2)   # already reaped by run_stage's finally; this just reads .returncode
        self.assertEqual(p.returncode, 97)
        stdout2.unlink(missing_ok=True)
        stderr2.unlink(missing_ok=True)

        # Fix round 1 (weak-test finding): row 3 and row 15 both describe "child gated, proc not
        # yet durable"; each row must independently prove its own post-condition rather than
        # relying on the other row's test -- so also drive the same fault through the real
        # runner path (row 15's mechanics: `procid.capture` raises inside `on_start`) and assert
        # THIS row's attempt record recovers to INTERRUPTED, not just "never execs" in isolation.
        t = self._ticket()
        task = self.tasks[0]
        r = self._runner()
        with patch("runner.procid.capture",
                  side_effect=RuntimeError("simulated crash: gated child, proc capture never lands")):
            with self.assertRaises(RuntimeError):
                self._direct_attempt(r, t, task, scenario="pass")
        self._reconcile()
        rec = attempt.load(self._adir(t, task))
        self.assertEqual(rec.status, "INTERRUPTED")

    # ----- Row 4 -----------------------------------------------------------------------

    def test_cw_04_gate_released_before_running_persisted(self):
        """gate released, before RUNNING persisted"""
        # Controller ruling, task-9 fix round 1: this table row is superseded by the design's
        # "exec-after-journal" guarantee (`procs.run_stage`'s docstring): on_start (which
        # durably records RUNNING) always completes *strictly before* the gate is released, so
        # the literal ordering this row names is unreachable by construction through the real
        # code path -- not a gap, and not weakenable without breaking that guarantee (out of
        # scope here). This test instead asserts the invariant itself, at the runner-integration
        # layer: a fake `on_start` records a monotonic timestamp on entry, sleeps 0.3s (standing
        # in for "gate release work taking time"), then the real command executes and stamps a
        # file with a wall-clock timestamp; that timestamp must be >= on_start's own completion.
        # This deliberately duplicates test_procs.test_child_does_not_execute_before_on_start's
        # mechanics one layer up (via `procs.run_stage` directly, the same primitive the runner
        # itself calls) -- the point is that the invariant must hold at both layers.
        tmp = pathlib.Path(tempfile.mkdtemp())
        marker = tmp / "ran_at"
        times = {}

        def on_start(pgid, pid):
            times["mono_start"] = time.monotonic()
            time.sleep(0.3)
            times["mono_done"] = time.monotonic()
            times["wall_done"] = time.time()

        result = procs.run_stage(
            [sys.executable, "-c",
             f"import time, pathlib; pathlib.Path({str(marker)!r}).write_text(str(time.time()))"],
            self.wt, 10.0, dict(os.environ), tmp / "out", tmp / "err", on_start=on_start)
        self.assertEqual(result.returncode, 0)
        written_at = float(marker.read_text())
        self.assertGreaterEqual(written_at, times["wall_done"])
        self.assertGreaterEqual(times["mono_done"] - times["mono_start"], 0.3)

        # Post-condition side of the row (unchanged from the original closest-faithful test): if
        # a crash *does* land somewhere in this now-provably-tiny window, recovery still kills
        # the live worker and drives the attempt to INTERRUPTED, regardless of the exact
        # sub-microsecond timing.
        p, pid = _spawn_group()
        try:
            t = self._ticket()
            task = self.tasks[0]
            rec = attempt.create(self.cfg, t.key, task, self.wt, 1,
                                 agentdef.load(self.cfg.pi_agents_dir, "cloud-worker"),
                                 "cloud", self._rung(), "test")
            rec = attempt.transition(rec, "LAUNCHING",
                                     stages=[{"kind": "worker", "idx": 0, "proc": None}])
            rec = attempt.transition(rec, "RUNNING", proc=pid.to_dict())
            self._reconcile()
            reloaded = attempt.load(rec.path)
            self.assertEqual(reloaded.status, "INTERRUPTED")
            self.assertTrue(reloaded.history and reloaded.published and reloaded.lifecycle)
            self.assertNotEqual(procid.classify(pid), "ours-alive")
        finally:
            _reap(p)

    # ----- Row 5 -----------------------------------------------------------------------

    def test_cw_05_leader_exited_live_descendant_unknown_fenced(self):
        """leader exited with a live descendant in the pgid"""
        # The leader (a short-lived /bin/sh) backgrounds a sleep under its own session/pgid and
        # exits immediately; the sleep (a "descendant") keeps the pgid populated. procid.capture
        # must run while the leader is still alive to record its lstart.
        # Pre-existing race, independent of this fix round's changes (present before them too):
        # the leader shell can exit and be fully reaped before `procid.capture` reaches
        # `os.getpgid` on a loaded machine (the exact window this row is about is a handful of
        # milliseconds wide); retry the whole spawn a few times rather than let the test flake.
        p = pgid = pid = None
        for _ in range(8):
            p = subprocess.Popen(["/bin/sh", "-c", "(sleep 60 &) ; exit 0"], start_new_session=True)
            try:
                pgid = os.getpgid(p.pid)
                pid = procid.capture(p.pid)
                break
            except ProcessLookupError:
                p.wait(timeout=2)
                continue
        else:
            self.fail("leader was reaped before capture on every retry")
        p.wait(timeout=2)   # leader exits; the backgrounded sleep survives under the same pgid
        try:
            t = self._ticket()
            task = self.tasks[0]
            rec = attempt.create(self.cfg, t.key, task, self.wt, 1,
                                 agentdef.load(self.cfg.pi_agents_dir, "cloud-worker"),
                                 "cloud", self._rung(), "test")
            # Fix round 1 (weak-test finding): a live group must never be restored under --
            # plant a sentinel in the worktree BEFORE the crash and prove it is untouched
            # afterward, rather than relying on "nothing was ever written".
            sentinel = self.wt / "app" / "components" / "sentinel_row5.rb"
            sentinel.write_text("do not touch -- live group, never restored\n")
            rec = attempt.transition(rec, "LAUNCHING",
                                     stages=[{"kind": "worker", "idx": 0, "proc": None}])
            rec = attempt.transition(rec, "RUNNING", proc=pid.to_dict())
            self.assertEqual(procid.classify(pid), "unknown")
            with self.assertRaises(reconcile.FenceExit) as cm:
                self._reconcile()
            self.assertEqual(cm.exception.code, 3)
            reloaded = attempt.load(rec.path, validate_worktree=False)
            self.assertEqual(reloaded.status, "ORPHANED")
            # Never restored under it: the worktree (nothing was ever written) is untouched and
            # the record is not INTERRUPTED.
            self.assertNotEqual(reloaded.status, "INTERRUPTED")
            self.assertTrue(sentinel.exists())
            self.assertEqual(sentinel.read_text(), "do not touch -- live group, never restored\n")
        finally:
            try:
                os.killpg(pgid, 9)
            except (ProcessLookupError, PermissionError):
                pass

    # ----- Row 6 -----------------------------------------------------------------------

    def test_cw_06_stage_reaped_before_stage_done_receipt(self):
        """stage reaped, before STAGE_DONE receipt"""
        t = self._ticket()
        task = self.tasks[0]
        r = self._runner()

        def crashing_launcher(argv, cwd, timeout_s, env, stdout_path, stderr_path, on_start=None):
            # Let the real (fake) worker actually run and exit -- "the stage is reaped" -- then
            # simulate the runner dying before it writes the STAGE_DONE record.
            result = self.launcher(argv, cwd, timeout_s, env, stdout_path, stderr_path, on_start=on_start)
            raise reconcile.SimulatedCrash("after stage reaped, before STAGE_DONE receipt")

        r2 = self._runner(launcher=crashing_launcher)
        with patch("reconcile._CRASH_HOOK", None):
            with self.assertRaises(reconcile.SimulatedCrash):
                self._direct_attempt(r2, t, task, scenario="pass")
        self._reconcile()
        rec = attempt.load(self._adir(t, task))
        # Treated as INTERRUPTED: recovery cannot know whether the worker's unwritten claim
        # would have been accepted or rejected -- it is not re-classified, just interrupted.
        self.assertEqual(rec.status, "INTERRUPTED")
        self.assertEqual(rec.outcome, "interrupted")

    # ----- Row 7 -----------------------------------------------------------------------

    def test_cw_07_stage_done_timedout_clean_before_classifying(self):
        """STAGE_DONE timed_out, clean allowlist, before CLASSIFYING"""
        t = self._ticket()
        task = self.tasks[0]
        (self.wt / "app" / "components" / "clean_edit.rb").write_text("clean\n")
        rec = attempt.create(self.cfg, t.key, task, self.wt, 1,
                             agentdef.load(self.cfg.pi_agents_dir, "cloud-worker"),
                             "cloud", self._rung(), "test")
        rec = attempt.transition(rec, "LAUNCHING",
                                 stages=[{"kind": "worker", "idx": 0, "proc": None}])
        rec = attempt.transition(rec, "RUNNING", proc=None)
        stages = [{"kind": "worker", "idx": 0, "proc": None, "terminated": True,
                  "timed_out": True, "rc": None, "elapsed_s": 999.0}]
        rec = attempt.transition(rec, "STAGE_DONE", stages=stages, proc=None)

        self._reconcile()
        reloaded = attempt.load(rec.path)
        self.assertEqual(reloaded.status, "PROJECTED")
        self.assertEqual(reloaded.outcome, "timeout")
        self.assertEqual(reloaded.tree, "kept")
        self.assertTrue((self.wt / "app" / "components" / "clean_edit.rb").exists())

    # ----- Row 8 -----------------------------------------------------------------------

    def test_cw_08_observed_tree_after_restore_before_finalized(self):
        """observed_tree recorded, after restore, before FINALIZED"""
        t = self._ticket()
        task = self.tasks[0]
        rec = attempt.create(self.cfg, t.key, task, self.wt, 1,
                             agentdef.load(self.cfg.pi_agents_dir, "cloud-worker"),
                             "cloud", self._rung(), "test")
        (self.wt / "app" / "components" / "junk.rb").write_text("junk\n")
        base_tree = rec.base_tree
        rec = attempt.transition(rec, "LAUNCHING")
        rec = attempt.transition(rec, "RUNNING", proc=None)
        rec = attempt.transition(rec, "STAGE_DONE", stages=[])
        rec = attempt.transition(rec, "CLASSIFYING", observed_tree=worktree.snapshot(self.wt))
        rec = attempt.transition(rec, "CLASSIFIED", outcome="rejected", reason="test",
                                 next_action="none")
        observed_tree = rec.observed_tree

        self._crash("after-restore", reconcile.finalize, self.cfg, rec)
        # The filesystem restore already happened (worktree.restore is not undone by the
        # crash); only the diff.patch write + FINALIZED transition never landed.
        self.assertEqual(worktree.snapshot(self.wt), base_tree)
        reloaded = attempt.load(rec.path)
        self.assertEqual(reloaded.status, "CLASSIFIED")
        self.assertFalse((rec.path / "diff.patch").exists())

        self._reconcile()
        final = attempt.load(rec.path)
        self.assertEqual(final.status, "PROJECTED")
        self.assertEqual(final.tree, "restored")
        patch_text = (rec.path / "diff.patch").read_text()
        expected = subprocess.run(["git", "-C", str(self.wt), "diff", "--no-color", base_tree,
                                   observed_tree], capture_output=True, text=True, check=True).stdout
        self.assertEqual(patch_text, expected)
        self.assertTrue(worktree.verify_restored(self.wt, base_tree))

    # ----- Row 9 -----------------------------------------------------------------------

    def test_cw_09_mid_restore_partial(self):
        """mid-restore (partial)"""
        t = self._ticket()
        task = self.tasks[0]
        rec = attempt.create(self.cfg, t.key, task, self.wt, 1,
                             agentdef.load(self.cfg.pi_agents_dir, "cloud-worker"),
                             "cloud", self._rung(), "test")
        (self.wt / "app" / "components" / "junk.rb").write_text("junk\n")
        base_tree = rec.base_tree
        rec = attempt.transition(rec, "LAUNCHING")
        rec = attempt.transition(rec, "RUNNING", proc=None)
        rec = attempt.transition(rec, "STAGE_DONE", stages=[])
        rec = attempt.transition(rec, "CLASSIFYING", observed_tree=worktree.snapshot(self.wt))
        rec = attempt.transition(rec, "CLASSIFIED", outcome="rejected", reason="test",
                                 next_action="none")

        # Simulate a restore that did not converge (a partial/interrupted git operation): the
        # record stays CLASSIFIED, nothing is corrupted.
        with mock.patch.object(worktree, "verify_restored", return_value=False):
            with self.assertRaises(reconcile.FinalizeFailed):
                reconcile.finalize(self.cfg, rec)
        reloaded = attempt.load(rec.path)
        self.assertEqual(reloaded.status, "CLASSIFIED")

        # Restart: restore is re-run (idempotently) with the real verify_restored, and converges.
        self._reconcile()
        final = attempt.load(rec.path)
        self.assertEqual(final.tree, "restored")
        self.assertEqual(worktree.snapshot(self.wt), base_tree)

    # ----- Row 10 ----------------------------------------------------------------------

    def test_cw_10_history_projected_before_flag(self):
        """history projected, before flag"""
        t = self._ticket()
        task = self.tasks[0]
        rec = attempt.create(self.cfg, t.key, task, self.wt, 1,
                             agentdef.load(self.cfg.pi_agents_dir, "cloud-worker"),
                             "cloud", self._rung(), "test")
        rec = attempt.transition(rec, "LAUNCHING")
        rec = attempt.transition(rec, "RUNNING", proc=None)
        rec = attempt.transition(rec, "STAGE_DONE", stages=[])
        rec = attempt.transition(rec, "CLASSIFYING", observed_tree=worktree.snapshot(self.wt))
        rec = attempt.transition(rec, "CLASSIFIED", outcome="accepted", reason="ok",
                                 next_action="none")
        rec = reconcile.finalize(self.cfg, rec)

        self._crash("after-history", reconcile.project_history, self.cfg, rec)
        reloaded = attempt.load(rec.path)
        self.assertFalse(reloaded.history)
        entries = state.load(self.cfg.ticket_dir(t.key)).attempts[rec.lineage]
        self.assertEqual(len(entries), 1)

        # Second projection is a no-op (attempt_id dedupe): re-run without the crash.
        rec2 = reconcile.project_history(self.cfg, reloaded)
        self.assertTrue(rec2.history)
        entries2 = state.load(self.cfg.ticket_dir(t.key)).attempts[rec.lineage]
        self.assertEqual(len(entries2), 1)

    # ----- Row 11 ----------------------------------------------------------------------

    def test_cw_11_metrics_row_appended_before_flag(self):
        """metrics row appended, before flag"""
        t = self._ticket()
        task = self.tasks[0]
        rec = attempt.create(self.cfg, t.key, task, self.wt, 1,
                             agentdef.load(self.cfg.pi_agents_dir, "cloud-worker"),
                             "cloud", self._rung(), "test")
        stages = [{"kind": "worker", "idx": 0, "proc": None, "terminated": True,
                  "timed_out": False, "rc": 0, "elapsed_s": 1.0}]
        rec = attempt.transition(rec, "LAUNCHING", stages=stages)
        rec = attempt.transition(rec, "RUNNING", proc=None)
        rec = attempt.transition(rec, "STAGE_DONE", stages=stages, proc=None)
        rec = attempt.transition(rec, "CLASSIFYING", observed_tree=worktree.snapshot(self.wt))
        rec = attempt.transition(rec, "CLASSIFIED", outcome="accepted", reason="ok",
                                 next_action="none")
        rec = reconcile.finalize(self.cfg, rec)

        self._crash("after-metrics", reconcile.project_metrics, self.cfg, rec)
        reloaded = attempt.load(rec.path)
        self.assertFalse(reloaded.published)
        rows = metrics.read_all(self.cfg.state_root)
        self.assertEqual(len(rows), 1)

        rec2 = reconcile.project_metrics(self.cfg, reloaded)
        self.assertTrue(rec2.published)
        rows2 = metrics.read_all(self.cfg.state_root)
        self.assertEqual(len(rows2), 1)   # no duplicate

    # ----- Row 12 ----------------------------------------------------------------------

    def test_cw_12_lifecycle_transition_applied_before_flag(self):
        """lifecycle transition applied, before flag"""
        t = self._ticket()
        task = self.tasks[0]
        rec = attempt.create(self.cfg, t.key, task, self.wt, 1,
                             agentdef.load(self.cfg.pi_agents_dir, "cloud-worker"),
                             "cloud", self._rung(), "test")
        rec = attempt.transition(rec, "LAUNCHING")
        rec = attempt.transition(rec, "RUNNING", proc=None)
        rec = attempt.transition(rec, "STAGE_DONE", stages=[])
        rec = attempt.transition(rec, "CLASSIFYING", observed_tree=worktree.snapshot(self.wt))
        rec = attempt.transition(rec, "CLASSIFIED", outcome="blocked", reason="owner",
                                 next_action="block")
        rec = reconcile.finalize(self.cfg, rec)

        self._crash("after-lifecycle", reconcile.project_lifecycle, self.cfg, rec)
        reloaded = attempt.load(rec.path)
        self.assertFalse(reloaded.lifecycle)
        self.assertEqual(state.load(self.cfg.ticket_dir(t.key)).state, "blocked")

        # Idempotent: already in state, a second call must not raise IllegalTransition.
        rec2 = reconcile.project_lifecycle(self.cfg, reloaded)
        self.assertTrue(rec2.lifecycle)
        self.assertEqual(state.load(self.cfg.ticket_dir(t.key)).state, "blocked")

    # ----- Row 13 ----------------------------------------------------------------------

    def test_cw_13_fencing_written_fence_write_fails(self):
        """FENCING written, fence write fails"""
        t = self._ticket()
        task = self.tasks[0]
        rec = attempt.create(self.cfg, t.key, task, self.wt, 1,
                             agentdef.load(self.cfg.pi_agents_dir, "cloud-worker"),
                             "cloud", self._rung(), "test")
        rec = attempt.transition(rec, "LAUNCHING")
        rec = attempt.transition(rec, "RUNNING",
                                 proc={"boot_id": "other-boot", "pgid": 999999, "pid": 999999,
                                       "start_time": "x", "cmd": "y"})
        original_safe_write = attempt.safe_write

        def fake_safe_write(path, text):
            if pathlib.Path(path).name == "heavy.fence":
                raise OSError("disk full")
            return original_safe_write(path, text)

        with mock.patch.object(attempt, "safe_write", side_effect=fake_safe_write):
            with self.assertRaises(reconcile.FenceExit) as cm:
                reconcile.fence(self.cfg, rec, "termination unverified")
        self.assertEqual(cm.exception.code, 3)
        reloaded = attempt.load(rec.path, validate_worktree=False)
        self.assertEqual(reloaded.status, "FENCING")

        # Fix round 1 (weak-test finding): prove the fail-closed state actually PERSISTS across
        # a restart, not just immediately after fence()'s own raise -- call reconcile() again
        # (fence write is still failing is irrelevant here: no fence file exists and FENCING
        # with no fence file is itself the torn-write case, row 14's post-condition) and assert
        # it exits 3 again, citing FENCING.
        with self.assertRaises(reconcile.FenceExit) as cm2:
            self._reconcile("recover-2")
        self.assertEqual(cm2.exception.code, 3)
        self.assertIn("FENCING", cm2.exception.reason)

    # ----- Row 14 ----------------------------------------------------------------------

    def test_cw_14_fence_write_torn_temp_exists_no_fence(self):
        """fence write torn (temp exists, no fence)"""
        t = self._ticket()
        task = self.tasks[0]
        rec = attempt.create(self.cfg, t.key, task, self.wt, 1,
                             agentdef.load(self.cfg.pi_agents_dir, "cloud-worker"),
                             "cloud", self._rung(), "test")
        rec = attempt.transition(rec, "LAUNCHING")
        rec = attempt.transition(rec, "RUNNING", proc=None)
        rec = attempt.transition(rec, "STAGE_DONE", stages=[])
        rec = attempt.transition(rec, "FENCING")
        # A stray temp sibling (as safe_rewrite's own attempt.json rewrite for the FENCING
        # transition would leave behind if THAT write itself were torn) with no heavy.fence
        # ever created.
        (self.cfg.state_root / "locks").mkdir(parents=True, exist_ok=True)
        (self.cfg.state_root / "locks" / "heavy.fence.tmp-99999-deadbeef").write_text("{")
        self.assertFalse((self.cfg.state_root / "locks" / "heavy.fence").exists())

        with self.assertRaises(reconcile.FenceExit) as cm:
            self._reconcile()
        self.assertEqual(cm.exception.code, 3)
        self.assertIn("FENCING", cm.exception.reason)

    # ----- Row 15 ----------------------------------------------------------------------

    def test_cw_15_launching_written_child_gated(self):
        """LAUNCHING written, child gated"""
        t = self._ticket()
        task = self.tasks[0]
        r = self._runner()
        # Capture the gate-shell Popen so we can assert the exact exit code run_stage's docstring
        # promises when on_start raises: the gate pipe closes unwritten, `read` fails, exit 97.
        gate_procs = []
        real_popen = subprocess.Popen
        def spy_popen(*a, **kw):
            proc = real_popen(*a, **kw)
            argv = a[0] if a else kw.get("args", [])
            if isinstance(argv, list) and len(argv) >= 3 and argv[0] == "/bin/sh" and argv[2].startswith("read _ <&"):
                gate_procs.append(proc)          # only the launch-gate shell, not git/ps helpers
            return proc
        with patch("procs.subprocess.Popen", side_effect=spy_popen), \
             patch("runner.procid.capture", side_effect=RuntimeError("simulated crash: gated child, proc capture never lands")):
            with self.assertRaises(RuntimeError):
                self._direct_attempt(r, t, task, scenario="pass")
        self.assertEqual(len(gate_procs), 1, "exactly one gate shell should have been spawned")
        gate = gate_procs[0]
        try:
            gate.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        # Two safe terminal states, both meaning "the real command never exec'd": 97 = the gate
        # shell saw EOF on the pipe before the parent's kill landed; -SIGTERM/-SIGKILL = the
        # parent's exception path (`os.close(w)` then `kill_group`) won the race. Either way the
        # child's real command was never reached (worker_touch.rb absent, asserted below). A
        # positive non-97 code would mean the command ran and exited on its own -- that is the
        # failure this row guards.
        import signal as _sig
        self.assertIn(gate.returncode, (97, -_sig.SIGTERM, -_sig.SIGKILL),
                      f"gate shell must never exec the real command (rc={gate.returncode})")
        self.assertFalse((self.wt / "app" / "components" / "worker_touch.rb").exists())
        self._reconcile()
        rec = attempt.load(self._adir(t, task))
        self.assertEqual(rec.status, "INTERRUPTED")

    # ----- Row 16 ----------------------------------------------------------------------

    def test_cw_16_running_worker_alive_different_task_selected_next(self):
        """RUNNING, worker alive, different task selected next"""
        p, pid = _spawn_group()
        try:
            rec = attempt.create(self.cfg, "OTHER-1", self.tasks[0], self.wt, 1,
                                 agentdef.load(self.cfg.pi_agents_dir, "cloud-worker"),
                                 "cloud", self._rung(), "test")
            rec = attempt.transition(rec, "LAUNCHING",
                                     stages=[{"kind": "worker", "idx": 0, "proc": None}])
            rec = attempt.transition(rec, "RUNNING", proc=pid.to_dict())

            order = []
            real_kill = procs.kill_group

            def spy_kill(pgid, *a, **kw):
                order.append(("kill", pgid))
                return real_kill(pgid, *a, **kw)

            def spy_launch(argv, cwd, timeout_s, env, out, err, on_start=None):
                order.append(("launch",))
                return self.launcher(argv, cwd, timeout_s, env, out, err, on_start=on_start)

            self.scenarios = ["pass"]
            with patch("reconcile.procs_mod.kill_group", side_effect=spy_kill), \
                 patch("runner.procs.run_stage", side_effect=spy_launch):
                rc = runner.main(["--config", str(self.cfg.state_root.parent / "hopper.toml"), "dry-run",
                                  "--worktree", str(self.wt), "--tasks", str(self.cfg.state_root.parent / "tasks.toml"),
                                  "--scenario", "pass"])
            self.assertEqual(rc, 0)
            kills = [i for i, o in enumerate(order) if o[0] == "kill" and o[1] == pid.pgid]
            launches = [i for i, o in enumerate(order) if o[0] == "launch"]
            self.assertTrue(kills and launches)
            self.assertLess(min(kills), min(launches),
                            "the leftover live worker must be killed before any dispatch")
            self.assertEqual(attempt.load(rec.path).status, "INTERRUPTED")
        finally:
            _reap(p)

    # ----- Row 17 ----------------------------------------------------------------------

    def test_cw_17_running_worker_created_bin_oops_killed_before_classify(self):
        """RUNNING, worker created `bin/oops`, killed before classify"""
        t = self._ticket()
        task = self.tasks[0]
        r = self._runner()
        original_base_tree = worktree.snapshot(self.wt)
        self._crash("after-stage-done", self._direct_attempt, r, t, task, scenario="escape")
        self.assertTrue((self.wt / "bin" / "oops").exists())   # still there: never classified/restored yet

        self._reconcile()
        rec = attempt.load(self._adir(t, task))
        self.assertEqual(rec.status, "INTERRUPTED")
        self.assertFalse((self.wt / "bin" / "oops").exists())
        self.assertIn("bin/oops", (rec.path / "diff.patch").read_text())

        # Next attempt's base_tree == original.
        rec2 = attempt.create(self.cfg, t.key, task, self.wt, 2,
                              agentdef.load(self.cfg.pi_agents_dir, "cloud-worker"),
                              "cloud", self._rung(), "test")
        self.assertEqual(rec2.base_tree, original_base_tree)

    # ----- Row 18 ----------------------------------------------------------------------

    def test_cw_18_running_pgid_belongs_to_unrelated_process(self):
        """RUNNING, recorded pgid now belongs to an unrelated process (start_time mismatch)"""
        # Controller ruling, task-9 fix round 1: the table's own phrasing ("not signaled;
        # treated as dead") is superseded by the design's *Process identity* section, which
        # makes a start_time mismatch classify as `unknown` (fenced), not a bare `dead`. This
        # test asserts the design: `unknown` classification, ORPHANED, and (below) that
        # `os.killpg` is never invoked with a real signal against the shared pgid -- "not
        # signaled" is proven directly at the syscall the table's own language refers to, not
        # only via the higher-level `kill_group` spy.
        # Model an actual pgid reuse: `old_id` records a pid/start_time that no longer exists,
        # but shares `new_id`'s pgid (as if that pgid number were recycled to an unrelated live
        # session) -- `killpg(pgid, 0)` therefore succeeds (the group IS populated), and
        # `ps -o lstart= -p old_id.pid` is patched to resolve to the unrelated process's lstart,
        # so the identity check (start_time mismatch) is what fires, not a bare "dead" pgid.
        p2, new_id = _spawn_group()
        try:
            old_id = procid.ProcId(boot_id=locks.boot_id(), pgid=new_id.pgid, pid=999999,
                                   start_time="Thu Jan  1 00:00:00 1970", cmd="oldcmd")
            real_ps = procid._ps

            def fake_ps(pid):
                if pid == old_id.pid:
                    return real_ps(new_id.pid)
                return real_ps(pid)

            with patch("procid._ps", side_effect=fake_ps):
                self.assertEqual(procid.classify(old_id), "unknown")

                t = self._ticket()
                task = self.tasks[0]
                rec = attempt.create(self.cfg, t.key, task, self.wt, 1,
                                     agentdef.load(self.cfg.pi_agents_dir, "cloud-worker"),
                                     "cloud", self._rung(), "test")
                rec = attempt.transition(rec, "LAUNCHING")
                rec = attempt.transition(rec, "RUNNING", proc=old_id.to_dict())

                kill_calls = []
                real_kill = procs.kill_group

                def spy_kill(pgid, *a, **kw):
                    kill_calls.append(pgid)
                    return real_kill(pgid, *a, **kw)

                killpg_calls = []
                real_killpg = os.killpg

                def spy_killpg(pgid, sig):
                    killpg_calls.append((pgid, sig))
                    return real_killpg(pgid, sig)

                with patch("reconcile.procs_mod.kill_group", side_effect=spy_kill), \
                     patch("os.killpg", side_effect=spy_killpg):
                    with self.assertRaises(reconcile.FenceExit) as cm:
                        self._reconcile()
                self.assertEqual(cm.exception.code, 3)
            # Never signaled: kill_group (the only path that sends a real TERM/KILL) is never
            # invoked for an "unknown" classification -- fence() goes straight to fencing.
            self.assertEqual(kill_calls, [])
            # Fix round 1 (controller ruling): also assert at the syscall level -- no call to
            # `os.killpg` against the shared pgid ever used a real (nonzero) signal. `killpg(pgid,
            # 0)` probes (group_alive/classify) are expected and harmless; only sig != 0 would
            # mean the unrelated live process was actually signaled.
            self.assertFalse(any(pgid == new_id.pgid and sig != 0 for pgid, sig in killpg_calls))
            reloaded = attempt.load(rec.path, validate_worktree=False)
            self.assertEqual(reloaded.status, "ORPHANED")
            self.assertEqual(procid.classify(new_id), "ours-alive")   # the real process untouched
        finally:
            _reap(p2)

    # ----- Row 19 ----------------------------------------------------------------------

    def test_cw_19_classified_rejected_before_restore(self):
        """CLASSIFIED `rejected`, before restore"""
        t = self._ticket()
        task = self.tasks[0]
        r = self._runner()
        self._crash("after-classified", self._direct_attempt, r, t, task, scenario="fail")
        rec = attempt.load(self._adir(t, task))
        self.assertEqual(rec.status, "CLASSIFIED")
        self.assertEqual(rec.outcome, "rejected")
        self.assertTrue((self.wt / "app" / "components" / "worker_touch.rb").exists())   # not restored yet

        self._reconcile()
        rec = attempt.load(self._adir(t, task))
        self.assertEqual(rec.tree, "restored")
        self.assertFalse((self.wt / "app" / "components" / "worker_touch.rb").exists())
        # Fix round 1 (weak-test finding): assert the ABSOLUTE row count for this attempt
        # (worker + N verify stages), not just that a before/after count is equal -- two counts
        # being equal to each other says nothing if both are wrong (e.g. both zero).
        rows_for_attempt = [row for row in metrics.read_all(self.cfg.state_root)
                            if row["attempt_id"] == rec.attempt_id]
        expected_stage_count = len(rec.stages)   # rejected before verification ever runs: worker only
        self.assertGreater(expected_stage_count, 0)
        self.assertEqual(len(rows_for_attempt), expected_stage_count)
        rows = len(metrics.read_all(self.cfg.state_root))
        self._reconcile("recover-2")
        self.assertEqual(len(metrics.read_all(self.cfg.state_root)), rows)   # published once

    # ----- Row 20 ----------------------------------------------------------------------

    def test_cw_20_classified_accepted_before_projections(self):
        """CLASSIFIED `accepted`, before projections"""
        t = self._ticket()
        task = self.tasks[0]
        r = self._runner()
        self._crash("after-finalized", self._direct_attempt, r, t, task, scenario="pass")
        rec = attempt.load(self._adir(t, task))
        self.assertEqual(rec.status, "FINALIZED")
        self.assertFalse(rec.history or rec.published or rec.lifecycle)
        launches_before = self.launches

        self._reconcile()
        rec = attempt.load(self._adir(t, task))
        self.assertEqual(rec.status, "PROJECTED")
        t_entries = state.load(self.cfg.ticket_dir(t.key)).attempts[rec.lineage]
        self.assertEqual(len(t_entries), 1)
        rows = [r_ for r_ in metrics.read_all(self.cfg.state_root) if r_["stage_kind"] == "worker"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.launches, launches_before)   # no re-run of the worker

    # ----- Row 21 ----------------------------------------------------------------------

    def test_cw_21_finalized_history_true_before_metrics(self):
        """FINALIZED, history=true, before metrics"""
        t = self._ticket()
        task = self.tasks[0]
        rec = attempt.create(self.cfg, t.key, task, self.wt, 1,
                             agentdef.load(self.cfg.pi_agents_dir, "cloud-worker"),
                             "cloud", self._rung(), "test")
        stages = [{"kind": "worker", "idx": 0, "proc": None, "terminated": True,
                  "timed_out": False, "rc": 0, "elapsed_s": 1.0}]
        rec = attempt.transition(rec, "LAUNCHING", stages=stages)
        rec = attempt.transition(rec, "RUNNING", proc=None)
        rec = attempt.transition(rec, "STAGE_DONE", stages=stages, proc=None)
        rec = attempt.transition(rec, "CLASSIFYING", observed_tree=worktree.snapshot(self.wt))
        rec = attempt.transition(rec, "CLASSIFIED", outcome="accepted", reason="ok", next_action="none")
        rec = reconcile.finalize(self.cfg, rec)

        self._crash("after-history", reconcile.project_all, self.cfg, rec)
        reloaded = attempt.load(rec.path)
        self.assertFalse(reloaded.history)
        self.assertFalse(reloaded.published)
        self.assertEqual(metrics.read_all(self.cfg.state_root), [])

        rec2 = reconcile.project_all(self.cfg, reloaded)
        self.assertEqual(rec2.status, "PROJECTED")
        rows = metrics.read_all(self.cfg.state_root)
        self.assertEqual(len(rows), 1)
        entries = state.load(self.cfg.ticket_dir(t.key)).attempts[rec.lineage]
        self.assertEqual(len(entries), 1)   # dedupe: history entry not duplicated

    # ----- Row 22 ----------------------------------------------------------------------

    def test_cw_22_finalized_blocked_before_lifecycle(self):
        """FINALIZED `blocked`, before lifecycle"""
        t = self._ticket()
        task = self.tasks[0]
        r = self._runner()
        self._crash("after-metrics", self._direct_attempt, r, t, task, scenario="owner")
        self.assertEqual(state.load(self.cfg.ticket_dir(t.key)).state, "implement")   # not yet blocked

        self._reconcile()
        self.assertEqual(state.load(self.cfg.ticket_dir(t.key)).state, "blocked")

        launches_before = self.launches
        t2 = state.load(self.cfg.ticket_dir(t.key)); t2.worktree = str(self.wt)
        out = self._implement(r, t2, task, 0)
        self.assertEqual(out, "blocked")
        self.assertEqual(self.launches, launches_before)   # not dispatched

    # ----- Row 23 ----------------------------------------------------------------------

    def test_cw_23_verify_stage_terminated_false(self):
        """verify stage `terminated=False`"""
        t = self._ticket()
        task = self.tasks[0]
        r = self._runner()
        real_run_stage = procs.run_stage

        def flaky_run_stage(argv, cwd, timeout_s, env, out, err, on_start=None):
            # Only verification commands route through runner.procs.run_stage directly (the
            # worker stage goes through self.launch/self.pi_launcher, patched separately in
            # RunnerHarness.setUp) -- so every call seen here is a verification stage.
            result = real_run_stage(argv, cwd, timeout_s, env, out, err, on_start=on_start)
            return dataclasses.replace(result, terminated=False)

        with patch("runner.procs.run_stage", side_effect=flaky_run_stage):
            self.scenarios = ["pass"]
            with self.assertRaises(reconcile.FenceExit) as cm:
                r._attempt(t, task, self.wt, "cloud", self._rung(), 1, [], 0)
        self.assertEqual(cm.exception.code, 3)
        rec = attempt.load(self._adir(t, task), validate_worktree=False)
        self.assertEqual(rec.status, "ORPHANED")
        self.assertTrue((self.cfg.state_root / "locks" / "heavy.fence").exists())
        # No restore: the worker's edit (accepted-track) is still on disk.
        self.assertTrue((self.wt / "app" / "components" / "worker_touch.rb").exists())

    # ----- Row 23b (C1 regression) -----------------------------------------------------

    def test_cw_23b_stage_receipt_unverified_crash_before_fence_still_fences(self):
        """STAGE_DONE receipt with terminated=False persisted, killed BEFORE fence() -- recovery must
        still see the live stage's identity and fence, not restore under it (C1)."""
        p, pinfo = _spawn_group()
        try:
            live = pinfo.to_dict()
            t = self._ticket(); task = self.tasks[0]
            rec = attempt.create(self.cfg, t.key, task, self.wt, 1,
                                 agentdef.load(self.cfg.pi_agents_dir, "cloud-worker"),
                                 "cloud", self._rung(), "test")
            rec = attempt.transition(rec, "LAUNCHING", stages=[{"kind": "worker", "idx": 0, "proc": None}])
            rec = attempt.transition(rec, "RUNNING", proc=live, stages=[{"kind": "worker", "idx": 0, "proc": live}])
            # The receipt the runner now writes for an unverified stage: proc RETAINED on the record.
            rec = attempt.transition(rec, "STAGE_DONE", proc=live,
                                     stages=[{"kind": "worker", "idx": 0, "proc": live, "terminated": False,
                                              "timed_out": False, "rc": None, "elapsed_s": 1.0}])
            sentinel = self.wt / "app" / "components" / "live_writer_sentinel.rb"; sentinel.write_text("x\n")
            with patch("reconcile.procid_mod.classify", return_value="unknown"):
                with self.assertRaises(reconcile.FenceExit) as cm:
                    self._reconcile()
            self.assertEqual(cm.exception.code, 3)
            self.assertTrue(sentinel.exists(), "must not restore under a possibly-live group")
            fence = json.loads((self.cfg.state_root / "locks" / "heavy.fence").read_text())
            self.assertEqual(fence["proc"]["pgid"], live["pgid"])
            self.assertEqual(attempt.load(rec.path, validate_worktree=False).status, "ORPHANED")
        finally:
            try: os.killpg(pinfo.pgid, 9)
            except ProcessLookupError: pass


    def test_cw_23c_runner_receipt_retains_proc_when_unverified(self):
        """The runner's own STAGE_DONE receipt for an unverified stage must keep `proc` on the record
        (C1). Drive _attempt with a launcher returning terminated=False and inspect the record it
        left behind (fence() raises FenceExit; the record is ORPHANED with proc == the stage proc)."""
        t = self._ticket(); task = self.tasks[0]
        def unverified_launcher(argv, cwd, timeout_s, env, out, err, on_start=None):
            # Emulate a real launch: journal a real pid via on_start, then report unverified.
            p = subprocess.Popen(["sleep", "30"], start_new_session=True)
            try:
                on_start(os.getpgid(p.pid), p.pid)
                return procs.StageResult(returncode=None, timed_out=True, elapsed_s=0.1, pgid=os.getpgid(p.pid), terminated=False)
            finally:
                pass  # leave it running: the runner must fence, never restore under it
        r = self._runner(launcher=unverified_launcher)
        with self.assertRaises(reconcile.FenceExit):
            self._direct_attempt(r, t, task, scenario="pass")
        rec = attempt.load(self._adir(t, task), validate_worktree=False)
        self.assertEqual(rec.status, "ORPHANED")
        self.assertIsNotNone(rec.proc, "unverified stage must keep its identity on the record")
        self.assertEqual(rec.proc["pgid"], rec.stages[-1]["proc"]["pgid"])
        os.killpg(rec.proc["pgid"], 9)

    # ----- Row 24 ----------------------------------------------------------------------

    def test_cw_24_fence_written_before_orphaned(self):
        """fence written, before ORPHANED"""
        t = self._ticket()
        task = self.tasks[0]
        rec = attempt.create(self.cfg, t.key, task, self.wt, 1,
                             agentdef.load(self.cfg.pi_agents_dir, "cloud-worker"),
                             "cloud", self._rung(), "test")
        rec = attempt.transition(rec, "LAUNCHING")
        rec = attempt.transition(rec, "RUNNING",
                                 proc={"boot_id": "other-boot", "pgid": 999999, "pid": 999999,
                                       "start_time": "x", "cmd": "y"})
        rec = attempt.transition(rec, "STAGE_DONE", stages=[])
        self._crash("after-fence-write", reconcile.fence, self.cfg, rec, "test reason")
        fence_path = self.cfg.state_root / "locks" / "heavy.fence"
        self.assertTrue(fence_path.exists())
        reloaded = attempt.load(rec.path, validate_worktree=False)
        self.assertEqual(reloaded.status, "FENCING")   # ORPHANED transition never landed

        # Fence present -> exit 3, fail closed (the fence references a FENCING, not ORPHANED,
        # record: corrupt-fence semantics, never silently resolved).
        with self.assertRaises(reconcile.FenceExit) as cm:
            self._reconcile()
        self.assertEqual(cm.exception.code, 3)
        self.assertTrue(fence_path.exists())

    # ----- Row 25 ----------------------------------------------------------------------

    def test_cw_25_clear_fence_while_runner_is_live(self):
        """`clear-fence` while a runner is live"""
        fence_path = self.cfg.state_root / "locks" / "heavy.fence"
        fence_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"attempt_dir": str(self._adir(self._ticket(), self.tasks[0])),
                              "proc": {"boot_id": "x", "pgid": 1, "pid": 1, "start_time": "x", "cmd": "y"},
                              "reason": "t"})
        fence_path.write_text(payload)
        lease_path = self.cfg.state_root / "locks" / "runner"
        script = (f"import sys, time\nsys.path.insert(0, {str(HERE)!r})\nimport locks\n"
                 f"lease = locks.Lease({str(lease_path)!r}, 'runner')\n"
                 f"assert lease.acquire(hold=True)\nprint('ready', flush=True)\ntime.sleep(30)\n")
        proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
        try:
            line = proc.stdout.readline()
            self.assertEqual(line.strip(), "ready")
            import io, contextlib
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                rc = reconcile.clear_fence(self.cfg, force=False)
            self.assertEqual(rc, 3)
            self.assertIn(str(proc.pid), buf.getvalue())
            self.assertEqual(fence_path.read_text(), payload)   # untouched
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    # ----- Row 26 ----------------------------------------------------------------------

    def test_cw_26_clear_fence_on_dead_fence_produced_by_fence(self):
        """`clear-fence` on a dead fence produced by `_fence()`"""
        t = self._ticket()
        task = self.tasks[0]
        rec = attempt.create(self.cfg, t.key, task, self.wt, 1,
                             agentdef.load(self.cfg.pi_agents_dir, "cloud-worker"),
                             "cloud", self._rung(), "test")
        (self.wt / "app" / "components" / "junk.rb").write_text("junk\n")
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
        self.assertFalse((self.wt / "app" / "components" / "junk.rb").exists())

        # Next run proceeds (no fence, nothing to reconcile for this attempt).
        self._reconcile()

    # ----- Row 27 ----------------------------------------------------------------------

    def test_cw_27_clear_fence_force_on_live_group(self):
        """`clear-fence --force` on a live group"""
        p, pid = _spawn_group()
        try:
            t = self._ticket()
            task = self.tasks[0]
            rec = attempt.create(self.cfg, t.key, task, self.wt, 1,
                                 agentdef.load(self.cfg.pi_agents_dir, "cloud-worker"),
                                 "cloud", self._rung(), "test")
            (self.wt / "app" / "components" / "live_junk.rb").write_text("junk\n")
            rec = attempt.transition(rec, "LAUNCHING")
            rec = attempt.transition(rec, "RUNNING", proc=pid.to_dict())
            stages = [{"kind": "worker", "idx": 0, "proc": pid.to_dict(), "terminated": False,
                      "timed_out": False, "rc": None, "elapsed_s": 1.0}]
            rec = attempt.transition(rec, "STAGE_DONE", stages=stages, proc=None)
            with self.assertRaises(reconcile.FenceExit):
                reconcile.fence(self.cfg, rec, "termination unverified", proc=pid.to_dict())

            fence_path = self.cfg.state_root / "locks" / "heavy.fence"
            rc = reconcile.clear_fence(self.cfg, force=True)
            self.assertEqual(rc, 0)
            self.assertFalse(fence_path.exists())
            reloaded = attempt.load(rec.path, validate_worktree=False)
            self.assertEqual(reloaded.status, "ORPHANED")
            self.assertTrue(reloaded.operator_forced)
            self.assertTrue((self.wt / "app" / "components" / "live_junk.rb").exists())   # NOT restored

            with self.assertRaises(reconcile.FenceExit) as cm:
                self._reconcile()
            self.assertEqual(cm.exception.code, 3)
            self.assertTrue(fence_path.exists())   # re-fenced
            self.assertEqual(attempt.load(rec.path, validate_worktree=False).status, "ORPHANED")
        finally:
            _reap(p)

    # ----- Row 28 ----------------------------------------------------------------------

    def test_cw_28_clear_fence_killed_after_transition_before_unlink(self):
        """`clear-fence` killed after attempt transition, before fence unlink"""
        p, pid = _spawn_group()
        try:
            t = self._ticket()
            task = self.tasks[0]
            rec = attempt.create(self.cfg, t.key, task, self.wt, 1,
                                 agentdef.load(self.cfg.pi_agents_dir, "cloud-worker"),
                                 "cloud", self._rung(), "test")
            rec = attempt.transition(rec, "LAUNCHING")
            rec = attempt.transition(rec, "RUNNING", proc=pid.to_dict())
            stages = [{"kind": "worker", "idx": 0, "proc": pid.to_dict(), "terminated": False,
                      "timed_out": False, "rc": None, "elapsed_s": 1.0}]
            rec = attempt.transition(rec, "STAGE_DONE", stages=stages, proc=None)
            # Build the exact torn state fence() can leave behind: FENCING durably written,
            # fence file durably written, but the ORPHANED transition never landed (fence()
            # itself always completes both before raising, so hand-build this rather than
            # calling it, mirroring row 24's technique).
            rec = attempt.transition(rec, "FENCING")
            fence_path = self.cfg.state_root / "locks" / "heavy.fence"
            fence_path.parent.mkdir(parents=True, exist_ok=True)
            fence_path.write_text(json.dumps({"attempt_dir": str(rec.path), "proc": pid.to_dict(),
                                              "reason": "termination unverified"}))

            self._crash("clear-fence-after-transition", reconcile.clear_fence, self.cfg, True)
            self.assertTrue(fence_path.exists())   # unlink never landed
            self.assertEqual(attempt.load(rec.path, validate_worktree=False).status, "ORPHANED")

            _reap(p)   # operator independently kills the group before the next start
            self._reconcile()
            self.assertFalse(fence_path.exists())
            self.assertEqual(attempt.load(rec.path).status, "INTERRUPTED")
        finally:
            _reap(p)

    # ----- Row 29 ----------------------------------------------------------------------

    def test_cw_29_orphaned_fence_deleted_by_hand(self):
        """ORPHANED attempt whose fence file was deleted by hand"""
        p, pid = _spawn_group()
        try:
            t = self._ticket()
            task = self.tasks[0]
            rec = attempt.create(self.cfg, t.key, task, self.wt, 1,
                                 agentdef.load(self.cfg.pi_agents_dir, "cloud-worker"),
                                 "cloud", self._rung(), "test")
            rec = attempt.transition(rec, "LAUNCHING")
            rec = attempt.transition(rec, "RUNNING", proc=pid.to_dict())
            rec = attempt.transition(rec, "STAGE_DONE", stages=[])
            rec = attempt.transition(rec, "FENCING")
            rec = attempt.transition(rec, "ORPHANED")
            # Fence file absent -- deleted by hand while the group is still alive.
            with self.assertRaises(reconcile.FenceExit) as cm:
                self._reconcile()
            self.assertEqual(cm.exception.code, 3)
            reloaded = attempt.load(rec.path, validate_worktree=False)
            self.assertEqual(reloaded.status, "ORPHANED")   # re-fenced
            self.assertTrue((self.cfg.state_root / "locks" / "heavy.fence").exists())
        finally:
            _reap(p)

        # Sub-case: group now dead (reaped above by the finally) -> the next reconcile drives it
        # to INTERRUPTED directly instead of re-fencing (no fence file was ever written back).
        self.assertEqual(procid.classify(pid), "dead")
        self._reconcile()
        final = attempt.load(rec.path)
        self.assertEqual(final.status, "INTERRUPTED")
        self.assertFalse((self.cfg.state_root / "locks" / "heavy.fence").exists())

    # ----- Row 30 ----------------------------------------------------------------------

    def test_cw_30_metrics_torn_trailing_line(self):
        """metrics.jsonl has a torn trailing line"""
        p = self.cfg.state_root / "metrics.jsonl"
        prior = json.dumps({"a": 1}) + "\n"
        p.write_text(prior + '{"torn": tr')
        metrics.append(self.cfg.state_root, {"b": 2})
        data = p.read_text()
        self.assertTrue(data.startswith(prior))
        rows = metrics.read_all(self.cfg.state_root)
        self.assertEqual([r.get("a", r.get("b")) for r in rows], [1, 2])

    # ----- Row 31 ----------------------------------------------------------------------

    def test_cw_31_metrics_invalid_row_before_tail(self):
        """metrics.jsonl has an invalid row BEFORE the tail"""
        p = self.cfg.state_root / "metrics.jsonl"
        original = json.dumps({"a": 1}) + "\n" + "not json at all\n" + json.dumps({"c": 3}) + "\n"
        p.write_text(original)
        with self.assertRaises(metrics.MetricsCorrupt):
            metrics.append(self.cfg.state_root, {"d": 4})
        self.assertEqual(p.read_text(), original)   # no truncation

    # ----- Row 32 ----------------------------------------------------------------------

    def test_cw_32_worker_plants_dotdot_feedback_symlink(self):
        """worker plants `../feedback.md` symlink"""
        # Isolated proof that ladder.append_feedback's own no-follow write guard fires before
        # any bytes are written -- independent of the runner, whose attempt.create() read-side
        # guard (row 33) would otherwise always win the race in the full flow (see below).
        tmp = pathlib.Path(tempfile.mkdtemp())
        outside = tmp / "outside.md"
        outside.write_text("do not touch")
        task_root = tmp / "task_root"
        task_root.mkdir()
        (task_root / "feedback.md").symlink_to(outside)
        with self.assertRaises(attempt.UnsafePath):
            ladder.append_feedback(task_root, 1, "rejected: something")
        self.assertEqual(outside.read_text(), "do not touch")   # detected before write

        # Runner-level: a worker with bash access can compute task_root from AL_TASK_DIR and
        # plant the symlink DURING its own run, before this same attempt's rejected outcome
        # triggers the runner's own append_feedback call -- this is the one window where
        # attempt.create()'s read-side guard (already run, before the worker started) cannot
        # have caught it first. Defect exposed and fixed (see runner.py's append_feedback call
        # site): the runner no longer crashes -- it skips the append and continues.
        #
        # Controller ruling, task-9 fix round 1: this table row's literal post-condition
        # ("outcome protocol") is superseded -- ladder.append_feedback's write-side guard fires
        # AFTER this attempt is already CLASSIFIED, so ITS outcome is whatever the worker's own
        # run produced (here: `rejected`, from the `fail` scenario), not `protocol`. The design's
        # actual promise for this window is: detected before write, outside target unchanged,
        # the runner does not crash, and the ladder keeps advancing (proven here by a second
        # attempt actually being attempted, per the ticket's history, rather than the whole
        # process dying). The stderr log line the fix emits is also asserted directly.
        outside2 = self.cfg.state_root.parent / "outside_ladder2.md"
        outside2.write_text("keep")
        real_task_root = self.cfg.state_root / "attempts" / "ZIP-7873" / self.tasks[0].id

        def planting_launcher(argv, cwd, timeout_s, env, stdout_path, stderr_path, on_start=None):
            result = self.launcher(argv, cwd, timeout_s, env, stdout_path, stderr_path, on_start=on_start)
            real_task_root.mkdir(parents=True, exist_ok=True)
            fb = real_task_root / "feedback.md"
            if fb.exists() or fb.is_symlink():
                fb.unlink()
            fb.symlink_to(outside2)
            return result

        t = self._ticket()
        r = self._runner(launcher=planting_launcher)
        self.scenarios = ["fail"]
        launches_before = self.launches
        # Once planted, the symlink is never cleared by the (correctly fail-safe) production
        # fix -- attempt.create()'s own read-side guard (row 33) then refuses every subsequent
        # attempt the same way (no attempt directory, no worker launch, a synthetic `protocol`
        # history entry per iteration), exhausting the ladder to "blocked" rather than crashing.
        # The point of this row is exactly that: no crash, no write-through, ever, and the loop
        # keeps iterating ("the ladder advances") instead of dying on the spot.
        import io, contextlib
        stderr_buf = io.StringIO()
        with contextlib.redirect_stderr(stderr_buf):
            out = self._implement(r, t, self.tasks[0], 0)
        self.assertEqual(out, "blocked")
        self.assertEqual(outside2.read_text(), "keep")   # never written through
        entries = state.load(self.cfg.ticket_dir(t.key)).attempts[f"{t.key}/{self.tasks[0].id}"]
        # This attempt's OWN classification is unchanged by the guard: `rejected`, from the
        # `fail` scenario -- not `protocol` (that outcome belongs to attempt 2+, whose
        # attempt.create() itself refuses the now-symlinked task-level feedback.md).
        self.assertEqual(entries[0]["outcome"], "rejected")
        self.assertTrue(all(e["outcome"] in ("rejected", "protocol") for e in entries))
        # The ladder advanced: more than this one attempt was recorded (the loop did not stop
        # or crash after the symlink was planted), and the worker itself was launched exactly
        # once (attempt 2+ never launch a worker -- attempt.create() refuses them first).
        self.assertGreater(len(entries), 1)
        self.assertEqual(self.launches, launches_before + 1)
        self.assertIn("unsafe feedback.md, skipping feedback append for attempt", stderr_buf.getvalue())

    # ----- Row 33 ----------------------------------------------------------------------

    def test_cw_33_task_level_feedback_symlink_at_next_attempts_copy(self):
        """task-level `feedback.md` is a symlink at next attempt's copy/read"""
        outside = self.cfg.state_root.parent / "outside_feedback_copy.md"
        outside.write_text("secret bytes that must never reach the prompt")
        t0 = self._ticket("ZIP-9001")
        task = self.tasks[0]
        task_level = self.cfg.state_root / "attempts" / t0.key / task.id
        task_level.mkdir(parents=True, exist_ok=True)
        (task_level / "feedback.md").symlink_to(outside)

        with self.assertRaises(attempt.UnsafePath):
            attempt.create(self.cfg, t0.key, task, self.wt, 1,
                           agentdef.load(self.cfg.pi_agents_dir, "cloud-worker"),
                           "cloud", self._rung(), "test")
        # attempt.create()'s cleanup removed the leaf dir it made; no adir/feedback.md copy exists
        # anywhere, so the outside bytes never reached a prompt.
        self.assertFalse(list(task_level.glob("*/feedback.md")))
        self.assertEqual(outside.read_text(), "secret bytes that must never reach the prompt")
        # Clean up the scaffold dir (a bare digit-named leaf with no attempt.json would make
        # reconcile()'s sweep -- invoked below via implement_task -- fence closed on every
        # subsequent test in this method, since it globally scans cfg.state_root/attempts/**).
        import shutil as _shutil
        _shutil.rmtree(self.cfg.state_root / "attempts" / "ZIP-9001", ignore_errors=True)

        # Fix round 1 (weak-test finding): the isolated `attempt.create()` proof above is
        # necessary but not sufficient -- also drive it THROUGH `implement_task` (the real
        # runner loop), planting the symlink via the harness launcher BETWEEN attempt 1 and
        # attempt 2 (mirroring row 32's technique: a worker with bash access can compute
        # task_root from AL_TASK_DIR's parent and plant it during its own run), and assert the
        # full set of promises: attempt 2's history entry is `protocol`, no attempt directory
        # `2` was ever created, and the outside bytes never appear in any `prompt.md`/`task.md`
        # under `attempts/` (not just "no feedback.md copy").
        outside2 = self.cfg.state_root.parent / "outside_feedback_copy2.md"
        outside2.write_text("secret bytes v2 that must never reach the prompt")
        real_task_root = self.cfg.state_root / "attempts" / "ZIP-7873" / task.id

        def planting_launcher(argv, cwd, timeout_s, env, stdout_path, stderr_path, on_start=None):
            result = self.launcher(argv, cwd, timeout_s, env, stdout_path, stderr_path, on_start=on_start)
            if self.launches == 1:   # plant once, right after attempt 1's worker stage completes
                fb = real_task_root / "feedback.md"
                if fb.exists() or fb.is_symlink():
                    fb.unlink()
                fb.symlink_to(outside2)
            return result

        t = self._ticket()
        r = self._runner(launcher=planting_launcher)
        self.scenarios = ["fail"]
        out = self._implement(r, t, task, 0)
        self.assertEqual(out, "blocked")
        entries = state.load(self.cfg.ticket_dir(t.key)).attempts[f"{t.key}/{task.id}"]
        self.assertEqual(entries[0]["outcome"], "rejected")   # attempt 1: unaffected, ran normally
        self.assertEqual(entries[1]["outcome"], "protocol")   # attempt 2: refused at create()
        self.assertIsNone(entries[1]["n"])                    # no attempt record was ever made
        self.assertFalse((real_task_root / "2").exists())     # no attempt dir 2 was ever created
        for f in (self.cfg.state_root / "attempts").glob("**/prompt.md"):
            self.assertNotIn("secret bytes v2", f.read_text())
        for f in (self.cfg.state_root / "attempts").glob("**/task.md"):
            self.assertNotIn("secret bytes v2", f.read_text())
        self.assertEqual(outside2.read_text(), "secret bytes v2 that must never reach the prompt")

    # ----- Row 34 ----------------------------------------------------------------------

    def test_cw_34_worker_writes_ignored_path_then_rejected(self):
        """worker writes an ignored path (`tmp/x`) then is rejected"""
        (self.wt / ".gitignore").write_text("tmp/\n")
        subprocess.run(["git", "-C", str(self.wt), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.wt), "commit", "-qm", "gitignore"], check=True, capture_output=True)
        t = self._ticket()
        task = self.tasks[0]
        rec = attempt.create(self.cfg, t.key, task, self.wt, 1,
                             agentdef.load(self.cfg.pi_agents_dir, "cloud-worker"),
                             "cloud", self._rung(), "test")
        base_tree = rec.base_tree
        (self.wt / "tmp").mkdir()
        (self.wt / "tmp" / "x").write_text("ignored write\n")
        observed = worktree.snapshot(self.wt)
        self.assertNotEqual(observed, base_tree)   # snapshot() captures ignored writes too

        rec = attempt.transition(rec, "LAUNCHING")
        rec = attempt.transition(rec, "RUNNING", proc=None)
        rec = attempt.transition(rec, "STAGE_DONE", stages=[])
        rec = attempt.transition(rec, "CLASSIFYING", observed_tree=observed)
        rec = attempt.transition(rec, "CLASSIFIED", outcome="rejected", reason="test",
                                 next_action="none")
        rec = reconcile.finalize(self.cfg, rec)

        self.assertFalse((self.wt / "tmp" / "x").exists())
        self.assertEqual(worktree.snapshot(self.wt), base_tree)

    # ----- Row 35 ----------------------------------------------------------------------

    def test_cw_35_attempt_record_points_at_different_repo_id(self):
        """attempt record points at a different repo_id than the worktree"""
        t = self._ticket()
        task = self.tasks[0]
        rec = attempt.create(self.cfg, t.key, task, self.wt, 1,
                             agentdef.load(self.cfg.pi_agents_dir, "cloud-worker"),
                             "cloud", self._rung(), "test")
        (self.wt / "app" / "components" / "junk.rb").write_text("junk\n")
        rec = attempt.transition(rec, "LAUNCHING")
        rec = attempt.transition(rec, "RUNNING", proc=None)
        bad = dataclasses.replace(rec, repo_id="not-the-real-repo-id")
        bad.path = rec.path
        (rec.path / "attempt.json").write_text(attempt.to_json(bad))

        with self.assertRaises(reconcile.FenceExit) as cm:
            self._reconcile()
        self.assertEqual(cm.exception.code, 3)
        self.assertTrue((self.wt / "app" / "components" / "junk.rb").exists())   # nothing restored

    # ----- Row 36 ----------------------------------------------------------------------

    def test_cw_36_manifest_resliced_after_one_cheap_failure(self):
        """manifest re-sliced (same task.id, new fingerprint) after one cheap failure"""
        # Fix round 1 (weak-test finding): the original version hand-fed a synthetic
        # `ladder.Attempt(..., "rejected")` history list into a second, isolated `_attempt`
        # call -- it never proved the REAL ladder loop (`implement_task`) reads its own
        # persisted history back and continues correctly after a manifest re-slice. Drive it
        # through two real runner invocations instead: attempt 1 runs to completion via the
        # real `implement_task` loop (using a PAUSE file, planted by a wrapping launcher right
        # after the worker stage, to stop the loop after exactly one attempt -- standing in for
        # the process being restarted between attempts, the same boundary a manifest re-slice
        # would actually happen across); the manifest is then re-sliced and a second, separate
        # `implement_task` call resumes the SAME persisted ticket/ladder state.
        pause_flag = self.cfg.state_root / "PAUSE"

        def pausing_launcher(argv, cwd, timeout_s, env, stdout_path, stderr_path, on_start=None):
            result = self.launcher(argv, cwd, timeout_s, env, stdout_path, stderr_path, on_start=on_start)
            pause_flag.touch()
            return result

        self.scenarios = ["fail"]
        r1 = runner.Runner(self.cfg, run_id="test", pi_launcher=pausing_launcher)
        t = state.load(self.cfg.ticket_dir("ZIP-7873")); t.worktree = str(self.wt)
        for s in ("spinup", "plan", "plan-review", "implement"): t = state.transition(t, s)
        state.save(self.cfg.ticket_dir("ZIP-7873"), t)
        out1 = self._implement(r1, t, self.tasks[0], 0)
        self.assertEqual(out1, "paused")   # stopped after exactly one attempt, not exhausted

        lineage = f"ZIP-7873/{self.tasks[0].id}"
        entries1 = state.load(self.cfg.ticket_dir("ZIP-7873")).attempts[lineage]
        self.assertEqual(len(entries1), 1)
        self.assertEqual(entries1[0]["outcome"], "rejected")
        self.assertEqual(entries1[0]["rung"]["n"], 1)
        self.assertEqual(entries1[0]["rung"]["tier"], "cheap")
        gen1 = entries1[0]["generation"]
        arm1 = entries1[0]["arm"]

        # Re-slice the manifest: same task.id, different summary (-> different generation).
        pause_flag.unlink()
        resliced = self.tasks_with(summary="a re-sliced summary for the same task id")
        self.assertEqual(resliced[0].id, self.tasks[0].id)

        self.scenarios = ["pass"]
        r2 = runner.Runner(self.cfg, run_id="test", pi_launcher=self.launcher)
        t2 = state.load(self.cfg.ticket_dir("ZIP-7873")); t2.worktree = str(self.wt)
        out2 = self._implement(r2, t2, resliced[0], 0)
        self.assertEqual(out2, "accepted")

        entries2 = state.load(self.cfg.ticket_dir("ZIP-7873")).attempts[lineage]
        self.assertEqual(len(entries2), 2)
        # Ladder continued at cheap attempt 2 (rung n=2), not reset to attempt 1 of a fresh ladder.
        self.assertEqual(entries2[-1]["rung"]["n"], 2)
        self.assertEqual(entries2[-1]["rung"]["tier"], "cheap")
        self.assertNotEqual(entries2[-1]["generation"], gen1)   # generation change is logged
        self.assertEqual(entries2[-1]["arm"], arm1)             # arm assignment unchanged

    # ----- Row 37 ----------------------------------------------------------------------

    def test_cw_37_local_model_ctx4k_or_empty_is_config_error(self):
        """`[local].model = "…-ctx4k:…"` or empty with `[local]` present"""
        root = self.cfg.state_root.parent
        base = (HERE.parent / "hopper.toml").read_text()
        base = base.replace('state_root = "~/.local/state/agent-loop"', f'state_root = "{root}/state2"')
        base = base.replace('pi_agents_dir = "~/.pi/agent/agents"', f'pi_agents_dir = "{root}/agents"')
        for bad_model in ('ollama-local/gpt-oss-ctx4k:20b', ''):
            with self.subTest(bad_model=bad_model):
                text = re.sub(r'model = "[^"]*"(?=\s*#? *MUST be ctx-pinned)',
                              f'model = "{bad_model}"', base)
                self.assertIn(f'model = "{bad_model}"', text)
                p = root / "hopper_bad_local.toml"
                p.write_text(text)
                with self.assertRaises(config.ConfigError):
                    config.load(p)

    # ----- Row 38 ----------------------------------------------------------------------

    def test_cw_38_rendered_local_worker_model_mismatches_config(self):
        """rendered local-worker model ≠ `[local].model`"""
        bad_model = "ollama-local/other-ctx32k:20b"
        (self.cfg.pi_agents_dir / "local-worker.md").write_text(
            f"---\nname: local-worker\ndescription: d\nmodel: {bad_model}\nthinking: low\n"
            f"tools: read, grep, find, ls, bash, edit, write\n---\nbody\n")
        t = self._ticket()
        r = self._runner()
        rung = self._rung(agent="local-worker", tier="cheap", n=1)
        with self.assertRaises(RuntimeError) as cm:
            r._attempt(t, self.tasks[0], self.wt, "local", rung, 1, [], 0)
        msg = str(cm.exception)
        self.assertIn(bad_model, msg)
        self.assertIn(self.local_model_default, msg)
        self.assertFalse(self._adir(t, self.tasks[0]).exists())   # no attempt record was ever created


# Suppress RunnerHarness's own test_* methods on this subclass (see the module docstring): only
# this file's test_cw_* methods should run under `unittest test_crash_windows`.
for _name in dir(_test_runner_mod.RunnerHarness):
    if _name.startswith("test_") and _name not in vars(CrashWindowTests):
        setattr(CrashWindowTests, _name, None)


if __name__ == "__main__":
    unittest.main()
