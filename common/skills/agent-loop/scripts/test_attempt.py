import dataclasses
import json
import os
import pathlib
import subprocess
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
                rec = attempt.transition(rec, "FENCING")
                rec = attempt.transition(rec, "ORPHANED")
                rec2 = attempt.transition(rec, "INTERRUPTED")
                self.assertEqual(rec2.status, "INTERRUPTED")
                rec = attempt.transition(rec, "FENCING")   # ORPHANED -> FENCING (re-fence) also legal
                self.assertEqual(rec.status, "FENCING")

    def test_classified_can_finalize_directly_on_recovery(self):
        rec = self._create()
        rec.status = "CLASSIFIED"
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
        rec = attempt.set_flags(rec, published=True, history=True)   # lifecycle still false
        rec2 = attempt.maybe_project(rec)
        self.assertEqual(rec2.status, "FINALIZED")


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


class IOHelperTests(AttemptTestCase):
    def test_safe_read_refuses_symlink(self):
        with tempfile.TemporaryDirectory() as d:
            target = pathlib.Path(d) / "real.txt"; target.write_text("hi")
            link = pathlib.Path(d) / "link.txt"; link.symlink_to(target)
            with self.assertRaises(attempt.UnsafePath):
                attempt.safe_read(link)

    def test_safe_read_refuses_missing(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(attempt.UnsafePath):
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

    def test_safe_rewrite_tolerates_stale_tmp(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "f.txt"; p.write_text("orig")
            stale = pathlib.Path(d) / "f.txt.99999.deadbeef.tmp"; stale.write_text("stale")
            attempt.safe_rewrite(p, "new")
            self.assertEqual(p.read_text(), "new")
            self.assertFalse(stale.exists())

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
