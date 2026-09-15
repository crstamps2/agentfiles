import json
import pathlib
import subprocess
import tempfile
import unittest

import contracts
import parallel


def T(id_, files, after=(), visual=False):
    return contracts.Task(id=id_, slug=f"s{id_}", summary="x", allowed_files=list(files), verification_commands=["true"],
                          acceptance=["y"], visual=visual, after=list(after))


class IndependenceTests(unittest.TestCase):
    def test_disjoint_files_are_independent(self):
        self.assertTrue(parallel.independent(T("001", ["app/a/**"]), T("002", ["app/b/**"])))

    def test_overlapping_globs_are_dependent(self):
        self.assertFalse(parallel.independent(T("001", ["app/views/components/zui/well/**"]), T("002", ["app/views/components/zui/well/well.scss"])))
        self.assertFalse(parallel.independent(T("001", ["app/**"]), T("002", ["app/x.rb"])))
        self.assertFalse(parallel.independent(T("001", ["a.rb"]), T("002", ["a.rb"])))

    def test_explicit_after_forces_dependency(self):
        self.assertFalse(parallel.independent(T("001", ["a.rb"]), T("002", ["b.rb"], after=["001"])))


class BatchTests(unittest.TestCase):
    def test_ready_batch_respects_after_limit_and_pairwise_independence(self):
        tasks = [T("001", ["a/**"]), T("002", ["b/**"]), T("003", ["a/x.rb"]), T("004", ["c/**"], after=["001"]), T("005", ["d/**"])]
        b = parallel.ready_batch(tasks, done_ids=set(), in_flight=set(), limit=3)
        self.assertEqual([t.id for t in b], ["001", "002", "005"])          # 003 overlaps 001; 004 waits for 001
        b = parallel.ready_batch(tasks, done_ids={"001"}, in_flight={"002", "005"}, limit=3)
        self.assertEqual([t.id for t in b], ["003"])                        # one free slot; 003 now independent of running set
        b = parallel.ready_batch(tasks, done_ids={"001", "002", "003", "005"}, in_flight=set(), limit=3)
        self.assertEqual([t.id for t in b], ["004"])

    def test_in_flight_set_covers_finished_but_unlanded_tasks(self):
        """Regression: a task that finished but had not landed yet was re-dispatched (double land)."""
        tasks = [T("001", ["a/**"]), T("002", ["b/**"])]
        self.assertEqual([t.id for t in parallel.ready_batch(tasks, done_ids=set(), in_flight={"001", "002"}, limit=2)], [])

    def test_limit_one_is_serial_manifest_order(self):
        tasks = [T("001", ["a/**"]), T("002", ["b/**"])]
        self.assertEqual([t.id for t in parallel.ready_batch(tasks, set(), set(), 1)], ["001"])


class WorktreeAndLandingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = pathlib.Path(self.tmp.name)
        self.wt = self.root / "ticket"; self.wt.mkdir()
        self.g("init", "-q", "-b", "internal/zip-1"); self.g("config", "user.email", "t@t"); self.g("config", "user.name", "t")
        (self.wt / "app").mkdir(); (self.wt / "app" / "a.rb").write_text("a\n"); self.g("add", "-A"); self.g("commit", "-qm", "base")

    def tearDown(self):
        self.tmp.cleanup()

    def g(self, *a, cwd=None):
        return subprocess.run(["git", "-C", str(cwd or self.wt), *a], check=True, capture_output=True, text=True).stdout

    def test_task_worktree_is_detached_at_head_and_isolated(self):
        twt = parallel.ensure_task_worktree(self.wt, "ZIP-1", "001")
        self.assertTrue((twt / "app" / "a.rb").exists())
        (twt / "app" / "b.rb").write_text("b\n")
        self.assertFalse((self.wt / "app" / "b.rb").exists())                # edits do not leak to the ticket worktree
        twt2 = parallel.ensure_task_worktree(self.wt, "ZIP-1", "001")        # re-created fresh
        self.assertFalse((twt2 / "app" / "b.rb").exists())
        parallel.remove_task_worktree(self.wt, "ZIP-1", "001"); self.assertFalse(twt.exists())

    def test_land_applies_only_source_paths_from_the_attempt_diff(self):
        twt = parallel.ensure_task_worktree(self.wt, "ZIP-1", "002")
        (twt / "app" / "b.rb").write_text("b\n"); (twt / "tmp").mkdir(); (twt / "tmp" / "cache.bin").write_text("x")
        self.g("add", "-A", "--force", cwd=twt)
        patch = self.g("diff", "--cached", "--binary", cwd=twt)
        adir = self.root / "attempts" / "ZIP-1" / "002" / "1"; adir.mkdir(parents=True)
        (adir / "diff.patch").write_text(patch)
        (adir / "attempt.json").write_text(json.dumps({"changed_paths": ["app/b.rb", "tmp/cache.bin"], "worktree": str(twt), "outcome": "accepted"}))
        cfg = type("Cfg", (), {"harness_artifact_globs": ["tmp/**"], "state_root": self.root})()
        paths = parallel.land(cfg, "ZIP-1", self.wt, T("002", ["app/b.rb"]), adir)
        self.assertEqual(paths, ["app/b.rb"])
        self.assertEqual((self.wt / "app" / "b.rb").read_text(), "b\n")
        self.assertFalse((self.wt / "tmp" / "cache.bin").exists())
        self.assertIn("A  app/b.rb", self.g("status", "--porcelain"))        # staged, ready for publish's commit


if __name__ == "__main__":
    unittest.main()
