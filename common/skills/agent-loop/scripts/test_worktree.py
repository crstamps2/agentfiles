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
        self.assertEqual(sorted(worktree.changed_paths(self.wt, base)), ["app/a.rb", "app/new.rb"])
        worktree.restore(self.wt, base)
        self.assertEqual((self.wt/"app"/"a.rb").read_text(), "a\n")
        self.assertFalse((self.wt/"app"/"new.rb").exists())
        self.assertTrue((self.wt/"ignored.txt").exists())   # ignored files survive restore
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

if __name__ == "__main__":
    unittest.main()
