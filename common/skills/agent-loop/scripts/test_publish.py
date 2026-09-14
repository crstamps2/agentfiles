import pathlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import publish


def git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True, text=True).stdout


class GithubWriteAllowlistTests(unittest.TestCase):
    def test_unknown_verb_refused(self):
        with self.assertRaises(publish.PublishError):
            publish.github_write("pr-merge", ["gh", "pr", "merge"], ".")

    def test_forbidden_tokens_refused_even_under_allowed_verb(self):
        for bad in (["git", "push", "--force"], ["git", "push", "-f"], ["gh", "pr", "edit", "--add-reviewer", "x"],
                    ["gh", "pr", "edit", "--reviewer=x"], ["gh", "pr", "merge"]):
            with self.subTest(bad=bad), self.assertRaises(publish.PublishError):
                publish.github_write("pr-edit-body", bad, ".")

    def test_pr_create_without_draft_refused(self):
        with self.assertRaises(publish.PublishError):
            publish.github_write("pr-create-draft", ["gh", "pr", "create", "--title", "x"], ".")

    def test_pr_verbs_carry_skill_envelope(self):
        seen = {}
        def fake_sh(cmd, cwd, env=None, timeout=600):
            seen["env"] = env; return subprocess.CompletedProcess(cmd, 0, "https://github.com/x/y/pull/1\n", "")
        with patch.object(publish, "_sh", fake_sh):
            publish.github_write("pr-create-draft", ["gh", "pr", "create", "--draft"], ".")
        self.assertEqual(seen["env"], {"AGENT_PR_SKILL": "1"})


class GitSideTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.wt = pathlib.Path(self.tmp.name)
        git(self.wt, "init", "-q", "-b", "internal/zip-7873-well"); git(self.wt, "config", "user.email", "t@t"); git(self.wt, "config", "user.name", "t")
        git(self.wt, "config", "core.hooksPath", "/dev/null")      # no lefthook here
        (self.wt / "a.rb").write_text("a\n"); git(self.wt, "add", "-A"); git(self.wt, "commit", "-qm", "init")

    def tearDown(self):
        self.tmp.cleanup()

    def test_guard_branch_accepts_own_ticket_branch_and_refuses_others(self):
        self.assertEqual(publish.guard_branch(self.wt, "ZIP-7873"), "internal/zip-7873-well")
        with self.assertRaises(publish.PublishError):
            publish.guard_branch(self.wt, "ZIP-9999")
        git(self.wt, "checkout", "-qb", "main")
        with self.assertRaises(publish.PublishError):
            publish.guard_branch(self.wt, "ZIP-7873")

    def test_commit_paths_commits_exactly_the_given_paths(self):
        (self.wt / "b.rb").write_text("b\n"); (self.wt / "stray.rb").write_text("no\n")
        sha = publish.commit_paths(self.wt, ["b.rb"], "add b")
        self.assertIsNotNone(sha)
        self.assertEqual(git(self.wt, "show", "--name-only", "--format=", sha).split(), ["b.rb"])
        self.assertIn("stray.rb", git(self.wt, "status", "--porcelain"))

    def test_commit_paths_returns_none_when_nothing_staged(self):
        self.assertIsNone(publish.commit_paths(self.wt, ["a.rb"], "noop"))
        self.assertIsNone(publish.commit_paths(self.wt, [], "noop"))

    def test_push_skips_when_not_ahead(self):
        with patch.object(publish, "ahead_of_remote", return_value=0), patch.object(publish, "github_write") as gw:
            self.assertFalse(publish.push(self.wt, "internal/zip-7873-well")); gw.assert_not_called()

    def test_push_pushes_when_no_remote_branch_or_ahead(self):
        for ahead in (None, 2):
            with patch.object(publish, "ahead_of_remote", return_value=ahead), patch.object(publish, "github_write") as gw:
                self.assertTrue(publish.push(self.wt, "internal/zip-7873-well"))
                gw.assert_called_once_with("push", ["git", "push", "-u", "origin", "HEAD"], self.wt)


class PrBodyTests(unittest.TestCase):
    def test_title_format_per_skill(self):
        self.assertEqual(publish.pr_title("ZIP-7873", "ZUI Well  container. "), "INTERNAL: ZUI Well container ZIP-7873")

    def test_body_has_every_template_section_and_footer(self):
        b = publish.pr_body("ZIP-7873", "Well", "why", ["a.rb"], ["tests"], {"intent": "i"})
        for sec in ("🎁 Summary", "🖼 Screenshots", "☎️ Related links", "✋ Deployment Dependencies",
                    "🗺 Where should a review start?", "📝 Documentation", "✅ Quality Assurances",
                    "🤖 AI Conversation Summary", "Token Usage"):
            self.assertIn(sec, b)
        self.assertIn("https://zipline.atlassian.net/browse/ZIP-7873", b)
        self.assertNotIn("retailzipline.atlassian.net", b)
        self.assertTrue(b.rstrip().endswith(publish.FOOTER))

    def test_ensure_draft_pr_is_idempotent(self):
        with patch.object(publish, "existing_pr", return_value={"number": 7, "isDraft": True}), patch.object(publish, "github_write") as gw:
            self.assertEqual(publish.ensure_draft_pr(".", "b", "t", "body")["number"], 7); gw.assert_not_called()


if __name__ == "__main__":
    unittest.main()
