import pathlib
import subprocess
import tempfile
import unittest

import contracts
import guards


def T(cmds, allowed=("app/x.rb",)):
    return contracts.Task(id="001", slug="s", summary="x", allowed_files=list(allowed), verification_commands=list(cmds), acceptance=["y"])


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.wt = pathlib.Path(self.tmp.name)
        g = lambda *a: subprocess.run(["git", "-C", str(self.wt), *a], check=True, capture_output=True, text=True).stdout
        g("init", "-q", "-b", "main"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
        (self.wt / "app").mkdir(); (self.wt / "app" / "base.rb").write_text("class Base\n  def x = class_names('a')\nend\n")
        g("add", "-A"); g("commit", "-qm", "base"); self.g = g
        import worktree; self.base = worktree.snapshot(self.wt)

    def tearDown(self):
        self.tmp.cleanup()


class PrecheckTests(Fixture):
    def test_unrunnable_guard_is_a_violation(self):
        probs = guards.precheck(T(["nonexistent-tool --check app/base.rb"]), self.wt)
        self.assertEqual(len(probs), 1); self.assertIn("not runnable", probs[0])

    def test_runnable_guards_pass_precheck_regardless_of_direction(self):
        self.assertEqual(guards.precheck(T(["grep -q class_names app/base.rb", "! grep -q content_tag app/base.rb", "bin/rails test x"]), self.wt), [])


class AttributionTests(Fixture):
    def test_zip_7875_shape_is_charged_to_the_guard(self):
        # The real ZIP-7875 guard chained a task check with a repo-wide check (`i18n-tasks check-normalized`)
        # that already failed on the BASE tree (config/locales/en.yml not normalized): unrelated to the worker.
        (self.wt / "config").mkdir(); (self.wt / "config" / "en.yml").write_text("b: 1\na: 2\n"); self.g("add", "-A"); self.g("commit", "-qm", "locale")
        import worktree; base = worktree.snapshot(self.wt)
        cmd = "! grep -q content_tag app/base.rb && sort -c config/en.yml"      # sort -c stands in for check-normalized
        self.assertEqual(guards.attribute_rejection(T([cmd]), cmd, self.wt, base), "guard")

    def test_worker_breaking_a_passing_guard_is_charged_to_the_worker(self):
        (self.wt / "app" / "base.rb").write_text("class Base\n  def x = content_tag(:div)\nend\n")
        cmd = "! grep -q content_tag app/base.rb"
        self.assertEqual(guards.attribute_rejection(T([cmd]), cmd, self.wt, self.base), "worker")

    def test_guard_on_the_tasks_own_new_file_is_charged_to_the_worker(self):
        # the task creates app/x.rb; a guard reading it fails on base because the file is absent -> expected
        cmd = "grep -q Stepper app/x.rb"
        self.assertEqual(guards.attribute_rejection(T([cmd]), cmd, self.wt, self.base), "worker")

    def test_read_only_bundle_exec_checks_are_attributed(self):
        for cmd in ("bundle exec i18n-tasks check-normalized en", "bin/agent_run rubocop --cache false a.rb", "bundle exec reek a.rb", "npx figma connect parse --file a.ts"):
            self.assertFalse(guards.is_side_effecting(cmd), cmd)
        for cmd in ("bundle exec ruby linters/generate_zui_todo.rb", "bin/rails test x", "yarn build", "npx figma connect publish --dry-run"):
            self.assertTrue(guards.is_side_effecting(cmd), cmd)

    def test_side_effecting_commands_are_not_attributed(self):
        self.assertEqual(guards.attribute_rejection(T(["bin/rails test t.rb"]), "bin/rails test t.rb", self.wt, self.base), "unknown")


if __name__ == "__main__":
    unittest.main()
