import dataclasses
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import types
import unittest

import attempt
import contracts
import ladder


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


class FakeAgent:
    name = "cloud-worker"
    body = "you are a worker\n"


class AttemptTestCase(unittest.TestCase):
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

    def _persist(self, rec):
        # Test-only: force the on-disk record to match an in-memory mutation (simulating
        # "this attempt was recovered already sitting in state X on disk"), bypassing the
        # CAS check that transition()/set_flags() now apply to real callers.
        (rec.path / "attempt.json").write_text(attempt.to_json(rec))


class CreateTests(AttemptTestCase):
    def test_create_writes_artifacts_and_created_record(self):
        rec = self._create()
        adir = attempt.attempt_dir(self.cfg, "TICK-1", "001", 1)
        for name in ("task.toml", "task.md", "body.md", "prompt.md", "base_tree", "attempt.json"):
            self.assertTrue((adir / name).exists(), name)
        self.assertEqual(rec.status, "CREATED")
        self.assertEqual(rec.worktree, str(self.wt.resolve()))
        self.assertTrue(rec.repo_id)
        self.assertTrue(pathlib.Path(rec.repo_id).is_absolute())
        self.assertEqual(rec.base_tree, (adir / "base_tree").read_text())
        self.assertEqual(rec.attempt_id, f"TICK-1/001@TICK-1/001/1")
        self.assertEqual(rec.lineage, "TICK-1/001")
        self.assertTrue(rec.generation)
        self.assertEqual(rec.proc, None)
        self.assertEqual(rec.stages, [])
        self.assertFalse(rec.published)
        self.assertFalse(rec.history)
        self.assertFalse(rec.lifecycle)
        # round-trips through load()
        loaded = attempt.load(adir)
        self.assertEqual(loaded.attempt_id, rec.attempt_id)
        self.assertEqual(loaded.status, "CREATED")

    def test_create_dir_valid_only_once_attempt_json_written(self):
        # attempt.json is written last; if it's missing, load() must refuse the dir.
        adir = attempt.attempt_dir(self.cfg, "TICK-1", "001", 1)
        attempt.safe_mkdir(adir)
        attempt.safe_write(adir / "task.toml", attempt.task_toml(self.task))
        with self.assertRaises(attempt.UnreadableRecord):
            attempt.load(adir)

    def test_create_raises_on_symlinked_feedback_and_leaves_no_attempt_dir(self):
        adir = attempt.attempt_dir(self.cfg, "TICK-1", "001", 1)
        task_level = adir.parent
        task_level.mkdir(parents=True)
        target = task_level / "elsewhere.md"; target.write_text("sneaky")
        (task_level / "feedback.md").symlink_to(target)
        with self.assertRaises(attempt.UnsafePath):
            self._create()
        self.assertFalse(adir.exists())

    def test_create_raises_attempt_dir_exists_and_leaves_pre_existing_dir_untouched(self):
        # A duplicate create() call (or a colliding n) must never delete an attempt dir it
        # did not itself make -- only the leaf dir *we* mkdir'd gets cleaned up on failure.
        adir = attempt.attempt_dir(self.cfg, "TICK-1", "001", 1)
        adir.mkdir(parents=True)
        marker = adir / "marker.txt"
        marker.write_text("keep me")
        with self.assertRaises(attempt.AttemptDirExists):
            self._create()
        self.assertTrue(adir.exists())
        self.assertEqual(marker.read_text(), "keep me")


class IdentityTests(AttemptTestCase):
    def test_same_task_and_worktree_same_lineage_and_generation(self):
        a1 = attempt.identity("TICK-1", self.task, self.wt, 1)
        a2 = attempt.identity("TICK-1", self.task, self.wt, 2)
        self.assertEqual(a1[1], a2[1])          # lineage
        self.assertEqual(a1[2], a2[2])          # generation
        self.assertNotEqual(a1[0], a2[0])       # attempt_id differs by n

    def test_changed_summary_same_lineage_different_generation(self):
        a1 = attempt.identity("TICK-1", self.task, self.wt, 1)
        task2 = dataclasses.replace(self.task, summary="a different thing entirely")
        a2 = attempt.identity("TICK-1", task2, self.wt, 1)
        self.assertEqual(a1[1], a2[1])
        self.assertNotEqual(a1[2], a2[2])

    def test_attempt_id_form(self):
        attempt_id, lineage, _ = attempt.identity("TICK-1", self.task, self.wt, 3)
        self.assertEqual(attempt_id, f"TICK-1/001@{lineage}/3")


class TransitionTests(AttemptTestCase):
    def test_forward_path_to_projected(self):
        rec = self._create()
        rec = attempt.transition(rec, "LAUNCHING")
        rec = attempt.transition(rec, "RUNNING")
        rec = attempt.transition(rec, "STAGE_DONE")
        rec = attempt.transition(rec, "CLASSIFYING")
        rec = attempt.transition(rec, "CLASSIFIED", outcome="accepted", reason="none", next_action="none")
        rec = attempt.transition(rec, "FINALIZED", tree="kept")
        rec = attempt.set_flags(rec, published=True, history=True, lifecycle=True)
        rec = attempt.maybe_project(rec)
        self.assertEqual(rec.status, "PROJECTED")
        loaded = attempt.load(rec.path)
        self.assertEqual(loaded.status, "PROJECTED")

    def test_stage_done_can_loop_to_launching_for_next_stage(self):
        rec = self._create()
        rec = attempt.transition(rec, "LAUNCHING")
        rec = attempt.transition(rec, "RUNNING")
        rec = attempt.transition(rec, "STAGE_DONE")
        rec = attempt.transition(rec, "LAUNCHING")
        self.assertEqual(rec.status, "LAUNCHING")

    def test_recovery_edges(self):
        for i, start in enumerate(("CREATED", "LAUNCHING", "RUNNING", "STAGE_DONE", "CLASSIFYING")):
            with self.subTest(start=start):
                rec = self._create(n=i + 1)
                rec.status = start
                self._persist(rec)
                rec = attempt.transition(rec, "FENCING")
                rec = attempt.transition(rec, "ORPHANED")
                rec2 = attempt.transition(rec, "INTERRUPTED")
                self.assertEqual(rec2.status, "INTERRUPTED")
                # ORPHANED -> FENCING (re-fence) also legal, from a *fresh* ORPHANED copy
                # (the earlier `rec` var is now stale: the on-disk record moved to INTERRUPTED).
                rec.status = "ORPHANED"
                self._persist(rec)
                rec = attempt.transition(rec, "FENCING")
                self.assertEqual(rec.status, "FENCING")

    def test_classified_can_finalize_directly_on_recovery(self):
        rec = self._create()
        rec.status = "CLASSIFIED"
        self._persist(rec)
        rec = attempt.transition(rec, "FINALIZED", tree="restored")
        self.assertEqual(rec.status, "FINALIZED")

    def test_illegal_skip_raises(self):
        rec = self._create()
        with self.assertRaises(attempt.IllegalAttemptTransition):
            attempt.transition(rec, "RUNNING")   # CREATED -> RUNNING skips LAUNCHING

    def test_illegal_from_terminal_raises(self):
        rec = self._create()
        rec.status = "PROJECTED"
        with self.assertRaises(attempt.IllegalAttemptTransition):
            attempt.transition(rec, "LAUNCHING")

    def test_set_flags_does_not_change_state(self):
        rec = self._create()
        rec = attempt.transition(rec, "LAUNCHING")
        rec2 = attempt.set_flags(rec, history=True)
        self.assertEqual(rec2.status, "LAUNCHING")
        self.assertTrue(rec2.history)

    def test_maybe_project_noop_unless_all_flags_and_finalized(self):
        rec = self._create()
        rec.status = "FINALIZED"
        self._persist(rec)
        rec = attempt.set_flags(rec, published=True, history=True)   # lifecycle still false
        rec2 = attempt.maybe_project(rec)
        self.assertEqual(rec2.status, "FINALIZED")

    def test_stale_transition_raises(self):
        # Two in-memory copies of the same on-disk record; advance copy A, then try to
        # transition stale copy B -- it must be refused, not silently overwrite A's advance.
        rec = self._create()
        copy_a = attempt.load(rec.path)
        copy_b = attempt.load(rec.path)
        attempt.transition(copy_a, "LAUNCHING")
        with self.assertRaises(attempt.StaleRecord):
            attempt.transition(copy_b, "LAUNCHING")
        self.assertIsInstance(attempt.StaleRecord("x"), attempt.IllegalAttemptTransition)

    def test_stale_set_flags_raises(self):
        rec = self._create()
        copy_a = attempt.load(rec.path)
        copy_b = attempt.load(rec.path)
        attempt.transition(copy_a, "LAUNCHING")
        with self.assertRaises(attempt.StaleRecord):
            attempt.set_flags(copy_b, history=True)

    def test_stale_set_flags_does_not_clobber_earlier_flag_after_reload(self):
        # CAS must key off rev, not status/n: two same-status set_flags calls (one setting
        # `history`, the other `published`) must not silently overwrite each other's flag.
        rec = self._create()
        copy_a = attempt.load(rec.path)
        copy_b = attempt.load(rec.path)
        attempt.set_flags(copy_a, history=True)
        with self.assertRaises(attempt.StaleRecord):
            attempt.set_flags(copy_b, published=True)
        reloaded = attempt.load(rec.path)
        self.assertTrue(reloaded.history)
        self.assertFalse(reloaded.published)

    def test_two_processes_set_flags_on_different_flags_serialize_via_lock(self):
        # Two real processes racing set_flags() on different flags: the per-attempt flock
        # serializes their reload -> compute -> rewrite, and a StaleRecord retry loop lets
        # the loser reload and succeed on its next attempt -- both flags end up set.
        rec = self._create()
        here = pathlib.Path(attempt.__file__).resolve().parent
        adir = str(rec.path)

        def worker_code(flag_name):
            return (
                f"import sys, time, pathlib\n"
                f"sys.path.insert(0, {str(here)!r})\n"
                f"import attempt\n"
                f"adir = pathlib.Path({adir!r})\n"
                f"for _ in range(200):\n"
                f"    r = attempt.load(adir, validate_worktree=False)\n"
                f"    try:\n"
                f"        attempt.set_flags(r, {flag_name}=True)\n"
                f"        break\n"
                f"    except attempt.StaleRecord:\n"
                f"        time.sleep(0.01)\n"
                f"else:\n"
                f"    raise SystemExit(1)\n"
            )

        p1 = subprocess.Popen([sys.executable, "-c", worker_code("history")])
        p2 = subprocess.Popen([sys.executable, "-c", worker_code("published")])
        self.assertEqual(p1.wait(timeout=15), 0)
        self.assertEqual(p2.wait(timeout=15), 0)

        final = attempt.load(rec.path)
        self.assertTrue(final.history)
        self.assertTrue(final.published)


class LoadTests(AttemptTestCase):
    def test_missing_file(self):
        adir = attempt.attempt_dir(self.cfg, "TICK-1", "001", 1)
        attempt.safe_mkdir(adir)
        with self.assertRaises(attempt.UnreadableRecord):
            attempt.load(adir)

    def test_corrupt_json(self):
        adir = attempt.attempt_dir(self.cfg, "TICK-1", "001", 1)
        attempt.safe_mkdir(adir)
        (adir / "attempt.json").write_text("{not json")
        with self.assertRaises(attempt.UnreadableRecord):
            attempt.load(adir)

    def test_symlinked_attempt_json(self):
        adir = attempt.attempt_dir(self.cfg, "TICK-1", "001", 1)
        attempt.safe_mkdir(adir)
        target = adir / "elsewhere.json"
        target.write_text("{}")
        (adir / "attempt.json").symlink_to(target)
        with self.assertRaises(attempt.UnreadableRecord):
            attempt.load(adir)

    def test_legacy_record_missing_worktree(self):
        adir = attempt.attempt_dir(self.cfg, "TICK-1", "001", 1)
        attempt.safe_mkdir(adir)
        (adir / "attempt.json").write_text(json.dumps({"status": "launching", "pgid": 123}))
        with self.assertRaises(attempt.UnreadableRecord):
            attempt.load(adir)

    def test_unknown_key_tolerance(self):
        rec = self._create()
        raw = json.loads((rec.path / "attempt.json").read_text())
        raw["some_future_field"] = "value-from-the-future"
        (rec.path / "attempt.json").write_text(json.dumps(raw))
        loaded = attempt.load(rec.path)
        self.assertEqual(loaded.attempt_id, rec.attempt_id)

    def test_missing_repo_id(self):
        adir = attempt.attempt_dir(self.cfg, "TICK-1", "001", 1)
        attempt.safe_mkdir(adir)
        (adir / "attempt.json").write_text(json.dumps({"status": "launching", "worktree": str(self.wt)}))
        with self.assertRaises(attempt.UnreadableRecord):
            attempt.load(adir)

    def test_non_utf8_bytes_raise_unreadable(self):
        adir = attempt.attempt_dir(self.cfg, "TICK-1", "001", 1)
        attempt.safe_mkdir(adir)
        (adir / "attempt.json").write_bytes(b"\xff\xfe")
        with self.assertRaises(attempt.UnreadableRecord):
            attempt.load(adir)

    def test_load_refuses_nonexistent_worktree(self):
        rec = self._create()
        raw = json.loads((rec.path / "attempt.json").read_text())
        raw["worktree"] = str(pathlib.Path(self.state_tmp.name) / "nowhere-at-all")
        (rec.path / "attempt.json").write_text(json.dumps(raw))
        with self.assertRaises(attempt.UnreadableRecord):
            attempt.load(rec.path)
        # the CAS re-load path (validate_worktree=False) doesn't care about the worktree
        loaded = attempt.load(rec.path, validate_worktree=False)
        self.assertEqual(loaded.status, "CREATED")

    def test_load_refuses_stale_repo_id(self):
        rec = self._create()
        other_repo = tempfile.TemporaryDirectory()
        try:
            other_wt = make_repo(other_repo.name)
            other_repo_id = subprocess.run(
                ["git", "-C", str(other_wt), "rev-parse", "--git-common-dir"],
                capture_output=True, text=True, check=True).stdout.strip()
            other_repo_id = str((other_wt / other_repo_id).resolve()) \
                if not pathlib.Path(other_repo_id).is_absolute() else str(pathlib.Path(other_repo_id).resolve())
            raw = json.loads((rec.path / "attempt.json").read_text())
            raw["repo_id"] = other_repo_id
            (rec.path / "attempt.json").write_text(json.dumps(raw))
            with self.assertRaises(attempt.UnreadableRecord):
                attempt.load(rec.path)
        finally:
            other_repo.cleanup()


class IOHelperTests(AttemptTestCase):
    def test_safe_read_refuses_symlink(self):
        with tempfile.TemporaryDirectory() as d:
            target = pathlib.Path(d) / "real.txt"; target.write_text("hi")
            link = pathlib.Path(d) / "link.txt"; link.symlink_to(target)
            with self.assertRaises(attempt.UnsafePath):
                attempt.safe_read(link)

    def test_safe_read_refuses_missing(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(FileNotFoundError):
                attempt.safe_read(pathlib.Path(d) / "nope.txt")

    def test_safe_write_refuses_existing_symlink(self):
        with tempfile.TemporaryDirectory() as d:
            target = pathlib.Path(d) / "real.txt"; target.write_text("hi")
            link = pathlib.Path(d) / "link.txt"; link.symlink_to(target)
            with self.assertRaises(OSError):
                attempt.safe_write(link, "pwned")
            self.assertEqual(target.read_text(), "hi")

    def test_safe_write_refuses_existing_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "f.txt"; p.write_text("orig")
            with self.assertRaises(OSError):
                attempt.safe_write(p, "new")
            self.assertEqual(p.read_text(), "orig")

    @staticmethod
    def _dead_pid() -> int:
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        os.waitpid(pid, 0)
        return pid

    def test_safe_rewrite_sweeps_dead_pid_tmp(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "f.txt"; p.write_text("orig")
            stale = pathlib.Path(d) / f"f.txt.tmp-{self._dead_pid()}-deadbeef"; stale.write_text("stale")
            attempt.safe_rewrite(p, "new")
            self.assertEqual(p.read_text(), "new")
            self.assertFalse(stale.exists())

    def test_safe_rewrite_spares_live_pid_tmp(self):
        # A temp file embedding a still-alive pid is an in-flight write by another (or this)
        # process, not a crash leftover -- the sweep must not delete it.
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "f.txt"; p.write_text("orig")
            live = pathlib.Path(d) / f"f.txt.tmp-{os.getpid()}-deadbeef"; live.write_text("in flight")
            attempt.safe_rewrite(p, "new")
            self.assertEqual(p.read_text(), "new")
            self.assertTrue(live.exists())
            live.unlink()

    def test_safe_rewrite_spares_other_artifact_tmp(self):
        # Rewriting attempt.json must not sweep a temp belonging to a different artifact
        # whose name happens to start with the same prefix (attempt.json.extra).
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "attempt.json"; p.write_text("orig")
            dead = self._dead_pid()
            other = pathlib.Path(d) / f"attempt.json.extra.tmp-{dead}-deadbeef"; other.write_text("mine")
            attempt.safe_rewrite(p, "new")
            self.assertEqual(p.read_text(), "new")
            self.assertTrue(other.exists())
            other.unlink()

    def test_safe_mkdir_refuses_symlinked_parent(self):
        with tempfile.TemporaryDirectory() as d:
            real_parent = pathlib.Path(d) / "real"; real_parent.mkdir()
            link = pathlib.Path(d) / "link"
            link.symlink_to(real_parent)
            with self.assertRaises(attempt.UnsafePath):
                attempt.safe_mkdir(link / "child")

    def test_safe_mkdir_creates_missing_parents(self):
        with tempfile.TemporaryDirectory() as d:
            target = pathlib.Path(d) / "a" / "b" / "c"
            attempt.safe_mkdir(target)
            self.assertTrue(target.is_dir())


class NextNTests(AttemptTestCase):
    def test_next_n_on_empty_root(self):
        root = pathlib.Path(self.state_tmp.name) / "attempts" / "TICK-1" / "001"
        self.assertEqual(attempt.next_n(root), 1)

    def test_next_n_skips_gaps(self):
        root = pathlib.Path(self.state_tmp.name) / "attempts" / "TICK-1" / "001"
        (root / "1").mkdir(parents=True)
        (root / "3").mkdir(parents=True)
        self.assertEqual(attempt.next_n(root), 4)


if __name__ == "__main__":
    unittest.main()
