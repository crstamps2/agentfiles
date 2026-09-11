import os, pathlib, subprocess, tempfile, unittest
import worktree
from contracts import Task

def git(wt, *a):
    return subprocess.run(["git", "-C", str(wt), *a], capture_output=True, text=True, check=True).stdout

class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.wt = pathlib.Path(self.tmp.name)
        git(self.wt, "init", "-q", "-b", "main")
        git(self.wt, "config", "user.email", "t@t"); git(self.wt, "config", "user.name", "t")
        (self.wt/"app").mkdir(); (self.wt/"app"/"a.rb").write_text("a\n")
        (self.wt/".gitignore").write_text("ignored.txt\n")
        git(self.wt, "add", "-A"); git(self.wt, "commit", "-qm", "init")
    def tearDown(self):
        self.tmp.cleanup()

    def test_snapshot_includes_untracked_and_restore_reverts(self):
        base = worktree.snapshot(self.wt)
        (self.wt/"app"/"a.rb").write_text("changed\n")
        (self.wt/"app"/"new.rb").write_text("new\n")
        (self.wt/"ignored.txt").write_text("keep me\n")
        after = worktree.snapshot(self.wt)
        self.assertNotEqual(base, after)
        # snapshot() now captures ignored writes too (--force), so an ignored file written
        # after base shows up as changed and is removed on restore -- it was never part of base.
        self.assertEqual(sorted(worktree.changed_paths(self.wt, base)), ["app/a.rb", "app/new.rb", "ignored.txt"])
        worktree.restore(self.wt, base)
        self.assertEqual((self.wt/"app"/"a.rb").read_text(), "a\n")
        self.assertFalse((self.wt/"app"/"new.rb").exists())
        self.assertFalse((self.wt/"ignored.txt").exists())  # not in base -> removed by restore
        self.assertTrue(worktree.verify_restored(self.wt, base))
        self.assertEqual(git(self.wt, "status", "--porcelain").strip(), "")

    def test_snapshot_does_not_touch_real_index(self):
        (self.wt/"app"/"new.rb").write_text("new\n")
        worktree.snapshot(self.wt)
        self.assertIn("?? app/new.rb", git(self.wt, "status", "--porcelain"))

    def test_head(self):
        self.assertEqual(worktree.head(self.wt), git(self.wt, "rev-parse", "HEAD").strip())

    def test_rename_of_protected_file_reports_old_path(self):
        """When a protected file is renamed, git diff must report both old and new paths."""
        # Create AGENTS.md and commit it
        (self.wt/"AGENTS.md").write_text("agents list\n")
        git(self.wt, "add", "AGENTS.md")
        git(self.wt, "commit", "-qm", "add AGENTS.md")
        base = worktree.snapshot(self.wt)
        # Enable renames in git config so git would normally hide the old path
        git(self.wt, "config", "diff.renames", "true")
        # Rename the file
        (self.wt/"app").mkdir(exist_ok=True)
        os.rename(str(self.wt/"AGENTS.md"), str(self.wt/"app"/"notes.md"))
        # changed_paths must report both old and new paths (due to --no-renames)
        changed = worktree.changed_paths(self.wt, base)
        self.assertIn("AGENTS.md", changed, "Old path must be reported")
        self.assertIn("app/notes.md", changed, "New path must be reported")


class UnbornHeadTests(unittest.TestCase):
    def test_snapshot_and_restore_in_repo_with_no_commits(self):
        with tempfile.TemporaryDirectory() as d:
            wt = pathlib.Path(d); git(wt, "init", "-q", "-b", "main")
            (wt / "a.txt").write_text("a\n")
            base = worktree.snapshot(wt)
            (wt / "a.txt").write_text("b\n"); (wt / "new.txt").write_text("n\n")
            self.assertEqual(sorted(worktree.changed_paths(wt, base)), ["a.txt", "new.txt"])
            worktree.restore(wt, base)
            self.assertEqual((wt / "a.txt").read_text(), "a\n"); self.assertFalse((wt / "new.txt").exists())


class AllowlistTests(unittest.TestCase):
    PROT = ["bin/", ".github/", "AGENTS.md"]
    TESTS = ["test/**", "**/fixtures/**"]
    def task(self, allowed, may_edit_tests=False):
        return Task(id="001", slug="s", summary="s", allowed_files=allowed, verification_commands=["c"], acceptance=["a"], may_edit_tests=may_edit_tests)

    def test_all_within_allowlist_ok(self):
        v = worktree.check_allowlist(["app/views/components/zui/well/well.rb"], self.task(["app/views/components/zui/well/**"]), self.PROT, self.TESTS)
        self.assertEqual(v, [])
    def test_outside_allowlist_flagged(self):
        v = worktree.check_allowlist(["app/models/user.rb"], self.task(["app/views/**"]), self.PROT, self.TESTS)
        self.assertEqual(len(v), 1); self.assertIn("app/models/user.rb", v[0]); self.assertIn("not in allowed_files", v[0])
    def test_protected_always_flagged_even_if_allowed(self):
        v = worktree.check_allowlist(["bin/rails", "AGENTS.md"], self.task(["**"]), self.PROT, self.TESTS)
        self.assertEqual(len(v), 2); self.assertTrue(all("protected" in x for x in v))
    def test_tests_flagged_without_permission(self):
        v = worktree.check_allowlist(["test/components/well_test.rb"], self.task(["**"]), self.PROT, self.TESTS)
        self.assertEqual(len(v), 1); self.assertIn("may_edit_tests", v[0])
    def test_tests_ok_with_permission(self):
        v = worktree.check_allowlist(["test/components/well_test.rb", "spec/x/fixtures/f.yml"], self.task(["**"], may_edit_tests=True), self.PROT, self.TESTS)
        self.assertEqual(v, [])
    def test_dir_prefix_pattern(self):
        self.assertEqual(worktree.check_allowlist(["app/x/y/z.rb"], self.task(["app/x/"]), self.PROT, self.TESTS), [])
    def test_literal_pattern_does_not_prefix_match(self):
        """Literal allowlist entry 'app/well.rb' must not match 'app/well.rb.bak' or 'app/well.rbx'."""
        # Should reject .bak variant
        v = worktree.check_allowlist(["app/well.rb.bak"], self.task(["app/well.rb"]), self.PROT, self.TESTS)
        self.assertEqual(len(v), 1); self.assertIn("not in allowed_files", v[0])
        # Should accept exact match
        v = worktree.check_allowlist(["app/well.rb"], self.task(["app/well.rb"]), self.PROT, self.TESTS)
        self.assertEqual(v, [])

class IgnoredWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.wt = pathlib.Path(self.tmp.name)
        git(self.wt, "init", "-q", "-b", "main")
        git(self.wt, "config", "user.email", "t@t"); git(self.wt, "config", "user.name", "t")
        (self.wt/"app").mkdir(); (self.wt/"app"/"a.rb").write_text("a\n")
        (self.wt/".gitignore").write_text("tmp/\n")
        git(self.wt, "add", "-A"); git(self.wt, "commit", "-qm", "init")
    def tearDown(self):
        self.tmp.cleanup()

    def test_ignored_write_is_in_changed_paths_and_removed_by_restore(self):
        base = worktree.snapshot(self.wt)
        (self.wt/"tmp").mkdir()
        (self.wt/"tmp"/"x").write_text("scratch\n")
        after = worktree.snapshot(self.wt)
        self.assertNotEqual(base, after)
        self.assertIn("tmp/x", worktree.changed_paths(self.wt, base))
        worktree.restore(self.wt, base)
        self.assertFalse((self.wt/"tmp"/"x").exists())
        self.assertTrue(worktree.verify_restored(self.wt, base))

    def test_tracked_ignored_file_survives_restore(self):
        """A file that is tracked (force-added) despite matching an ignore rule must survive
        a restore to a base tree that already contains it."""
        (self.wt/"tmp").mkdir()
        (self.wt/"tmp"/"keep.txt").write_text("keep\n")
        git(self.wt, "add", "-f", "tmp/keep.txt")
        git(self.wt, "commit", "-qm", "tracked ignored file")
        base = worktree.snapshot(self.wt)
        (self.wt/"tmp"/"scratch").write_text("scratch\n")
        worktree.restore(self.wt, base)
        self.assertTrue((self.wt/"tmp"/"keep.txt").exists())
        self.assertEqual((self.wt/"tmp"/"keep.txt").read_text(), "keep\n")
        self.assertFalse((self.wt/"tmp"/"scratch").exists())
        self.assertTrue(worktree.verify_restored(self.wt, base))


class VerifyRestoredTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.wt = pathlib.Path(self.tmp.name)
        git(self.wt, "init", "-q", "-b", "main")
        git(self.wt, "config", "user.email", "t@t"); git(self.wt, "config", "user.name", "t")
        (self.wt/"a.txt").write_text("a\n")
        git(self.wt, "add", "-A"); git(self.wt, "commit", "-qm", "init")
    def tearDown(self):
        self.tmp.cleanup()

    def test_verify_restored_false_before_true_after(self):
        base = worktree.snapshot(self.wt)
        (self.wt/"a.txt").write_text("changed\n")
        self.assertFalse(worktree.verify_restored(self.wt, base))
        worktree.restore(self.wt, base)
        self.assertTrue(worktree.verify_restored(self.wt, base))


if __name__ == "__main__":
    unittest.main()


class RestoreSymlinkEscapeTests(unittest.TestCase):
    """C4: restore must never delete outside the worktree by traversing a restored base symlink."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.wt = pathlib.Path(self.tmp.name) / "wt"; self.wt.mkdir()
        self.outside = pathlib.Path(self.tmp.name) / "outside"; self.outside.mkdir()
        (self.outside / "x").write_text("keep me\n")
        git(self.wt, "init", "-q", "-b", "main"); git(self.wt, "config", "user.email", "t@t"); git(self.wt, "config", "user.name", "t")
        os.symlink(str(self.outside), self.wt / "d")          # base tree: d -> /outside (a symlink)
        git(self.wt, "add", "-A"); git(self.wt, "commit", "-qm", "base with symlink")
    def tearDown(self):
        self.tmp.cleanup()

    def test_worker_replaces_symlink_with_dir_then_restore_does_not_unlink_outside(self):
        base = worktree.snapshot(self.wt)
        (self.wt / "d").unlink(); (self.wt / "d").mkdir(); (self.wt / "d" / "x").write_text("worker\n")
        worktree.restore(self.wt, base)
        self.assertEqual((self.outside / "x").read_text(), "keep me\n")   # outside untouched
        self.assertTrue(os.path.islink(self.wt / "d"))                      # base symlink restored
        self.assertTrue(worktree.verify_restored(self.wt, base))

    def test_worker_replaces_symlink_with_file_then_restore_is_clean(self):
        base = worktree.snapshot(self.wt)
        (self.wt / "d").unlink(); (self.wt / "d").write_text("file now\n")
        worktree.restore(self.wt, base)
        self.assertTrue(os.path.islink(self.wt / "d")); self.assertTrue(worktree.verify_restored(self.wt, base))

