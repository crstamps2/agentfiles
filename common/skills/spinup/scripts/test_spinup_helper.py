"""Unit tests for spinup_helper. Run from the scripts/ dir:
    python3 -m unittest test_spinup_helper -v
"""

import contextlib
import io
import json
import pathlib
import sys
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent))
import spinup_helper  # noqa: E402
import spinup_helper as sh  # noqa: E402


class TestSpinupHelper(unittest.TestCase):
    def test_module_imports(self):
        self.assertTrue(hasattr(spinup_helper, "main"))

    def test_derive_branch_story(self):
        self.assertEqual(
            spinup_helper.derive_branch("PROJ-1234", "Story", "Add login button"),
            "feature/proj-1234-add-login-button",
        )

    def test_derive_branch_bug(self):
        self.assertEqual(
            spinup_helper.derive_branch("PROJ-99", "Bug", "Fix avatar crash"),
            "bugfix/proj-99-fix-avatar-crash",
        )

    def test_derive_branch_unknown_type_falls_back_to_internal(self):
        self.assertEqual(
            spinup_helper.derive_branch("PROJ-5", "Spike", "Investigate caching"),
            "internal/proj-5-investigate-caching",
        )

    def test_derive_branch_strips_special_chars(self):
        self.assertEqual(
            spinup_helper.derive_branch("PROJ-7", "Story", "Fix [BUG]: don't crash!"),
            "feature/proj-7-fix-bug-dont-crash",
        )

    def test_derive_branch_collapses_repeated_dashes(self):
        self.assertEqual(
            spinup_helper.derive_branch("PROJ-7", "Story", "a -- b // c"),
            "feature/proj-7-a-b-c",
        )

    def test_derive_branch_caps_slug_at_40_chars(self):
        long_title = "a" * 80
        result = spinup_helper.derive_branch("PROJ-1", "Story", long_title)
        # prefix/key-slug -- the slug portion alone is capped at 40
        self.assertTrue(result.startswith("feature/proj-1-"))
        slug = result.split("proj-1-", 1)[1]
        self.assertLessEqual(len(slug), 40)

    def test_derive_branch_ticket_key_lowercased(self):
        self.assertEqual(
            spinup_helper.derive_branch("PROJ-1234", "Story", "Foo"),
            "feature/proj-1234-foo",
        )

    def test_derive_branch_empty_title_uses_only_key(self):
        self.assertEqual(
            spinup_helper.derive_branch("PROJ-1", "Story", ""),
            "feature/proj-1",
        )

    def test_derive_branch_truncates_at_word_boundary_not_mid_word(self):
        # Title where naive 40-char truncation would land mid-word.
        # naive 40-char slug: "time-window-for-assessment-doesnt-show-o" (cuts "on" mid-word)
        # desired: trim back to last full word -> "time-window-for-assessment-doesnt-show"
        result = spinup_helper.derive_branch(
            "PROJ-2689",
            "Task",
            "Time window for assessment doesn't show on rendered page",
        )
        slug = result.split("proj-2689-", 1)[1]
        self.assertNotEqual(slug, "time-window-for-assessment-doesnt-show-o")
        self.assertEqual(slug, "time-window-for-assessment-doesnt-show")

    def test_workspace_name_from_key_lowercases(self):
        self.assertEqual(spinup_helper._workspace_name_from_key("PROJ-1234"), "proj-1234")


class TestCreateAtlasWorkspace(unittest.TestCase):
    def _fake_run(self, returncode=0, stdout="", stderr=""):
        return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)

    def test_create_atlas_workspace_invokes_cli_with_expected_args(self):
        fake_output = json.dumps({
            "id": "abc-123",
            "name": "proj-1234",
            "worktree_path": "$HOME/.atlas/workspaces/your-app/proj-1234",
        })
        with mock.patch.object(spinup_helper.subprocess, "run", return_value=self._fake_run(stdout=fake_output)) as m:
            result = spinup_helper.create_atlas_workspace("feature/proj-1234-foo", "PROJ-1234")
        cmd = m.call_args[0][0]
        self.assertEqual(cmd[0], "atlas-cli")
        self.assertIn("--json", cmd)
        self.assertIn("workspaces", cmd)
        self.assertIn("new", cmd)
        # --repo is pinned to the atlas_managed UUID, not the name
        self.assertIn(spinup_helper.ATLAS_REPO_ID, cmd)
        self.assertIn("--branch", cmd)
        self.assertIn("feature/proj-1234-foo", cmd)
        self.assertIn("--use-existing", cmd)
        # Positional NAME is the kebab-cased ticket key, last arg
        self.assertEqual(cmd[-1], "proj-1234")
        self.assertEqual(result["id"], "abc-123")

    def test_create_atlas_workspace_raises_on_nonzero_exit(self):
        with mock.patch.object(spinup_helper.subprocess, "run", return_value=self._fake_run(returncode=1, stderr="boom")):
            with self.assertRaises(spinup_helper.AtlasError) as ctx:
                spinup_helper.create_atlas_workspace("feature/proj-1-foo", "PROJ-1")
        self.assertIn("boom", str(ctx.exception))

    def test_create_atlas_workspace_raises_when_cli_missing(self):
        with mock.patch.object(spinup_helper.subprocess, "run", side_effect=FileNotFoundError):
            with self.assertRaises(spinup_helper.AtlasError) as ctx:
                spinup_helper.create_atlas_workspace("feature/proj-1-foo", "PROJ-1")
        self.assertIn("atlas-cli", str(ctx.exception))

    def test_create_atlas_workspace_raises_on_invalid_json(self):
        with mock.patch.object(spinup_helper.subprocess, "run", return_value=self._fake_run(stdout="not json")):
            with self.assertRaises(spinup_helper.AtlasError):
                spinup_helper.create_atlas_workspace("feature/proj-1-foo", "PROJ-1")


class TestStateOps(unittest.TestCase):
    def setUp(self):
        # Each test gets a fresh tempdir; CACHE_PATH is patched into it.
        self._tmp = tempfile.TemporaryDirectory()
        self._tmp_path = Path(self._tmp.name)
        self._cache_path = self._tmp_path / "state.json"
        self._patcher = mock.patch.object(spinup_helper, "CACHE_PATH", self._cache_path)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._tmp.cleanup()

    def test_load_state_missing_file_returns_empty(self):
        # CACHE_PATH points to a path that doesn't exist yet.
        self.assertEqual(spinup_helper.load_state(), {})

    def test_load_state_corrupted_file_returns_empty(self):
        self._cache_path.write_text("not json{{{")
        self.assertEqual(spinup_helper.load_state(), {})

    def test_save_then_load_roundtrip(self):
        state = {
            "PROJ-1": {"first_surfaced": "2026-04-28T00:00:00Z", "spunup_at": None}
        }
        spinup_helper.save_state(state)
        self.assertEqual(spinup_helper.load_state(), state)

    def test_mark_surfaced_creates_new_entry(self):
        spinup_helper.mark_surfaced("PROJ-42")
        state = spinup_helper.load_state()
        self.assertIn("PROJ-42", state)
        self.assertIsNone(state["PROJ-42"]["spunup_at"])
        self.assertTrue(state["PROJ-42"]["first_surfaced"])

    def test_mark_surfaced_idempotent(self):
        spinup_helper.mark_surfaced("PROJ-42")
        first = spinup_helper.load_state()["PROJ-42"]["first_surfaced"]
        spinup_helper.mark_surfaced("PROJ-42")
        second = spinup_helper.load_state()["PROJ-42"]["first_surfaced"]
        self.assertEqual(first, second)  # timestamp not overwritten

    def test_mark_spunup_sets_timestamp(self):
        spinup_helper.mark_surfaced("PROJ-7")
        spinup_helper.mark_spunup("PROJ-7")
        state = spinup_helper.load_state()
        self.assertIsNotNone(state["PROJ-7"]["spunup_at"])

    def test_pending_keys_excludes_already_spunup(self):
        spinup_helper.mark_surfaced("PROJ-1")
        spinup_helper.mark_surfaced("PROJ-2")
        spinup_helper.mark_spunup("PROJ-1")
        self.assertEqual(spinup_helper.pending_keys(), {"PROJ-2"})


# pytest-style top-level function (works when pytest is available)
def test_module_imports():
    assert hasattr(spinup_helper, "main")


# ---------------------------------------------------------------------------
# Task 1: Work-prompt builders
# ---------------------------------------------------------------------------

class WorkPromptTests(unittest.TestCase):
    def test_jira_work_prompt(self):
        self.assertEqual(
            sh.jira_work_prompt("PROJ-5467"),
            "writing-plans for PROJ-5467, make sure to test any assumptions or "
            "reproduce the issue and take screenshots on admin.proj-5467.test",
        )

    def test_pr_work_prompt(self):
        self.assertEqual(
            sh.pr_work_prompt(45647),
            "lens-review for PR #45647, QA on admin.pr-45647.test",
        )


# ---------------------------------------------------------------------------
# Task 2: atlas-cli term/claude wrappers
# ---------------------------------------------------------------------------

class AtlasWrapperTests(unittest.TestCase):
    def _ok(self, stdout="", returncode=0, stderr=""):
        m = MagicMock()
        m.stdout = stdout
        m.returncode = returncode
        m.stderr = stderr
        return m

    @patch("spinup_helper.subprocess.run")
    def test_term_new_returns_id(self, run):
        run.return_value = self._ok(stdout='{"id": "tid-123"}')
        self.assertEqual(sh.term_new("proj-5467"), "tid-123")
        args = run.call_args[0][0]
        self.assertEqual(args, ["atlas-cli", "--json", "term", "new", "proj-5467"])

    @patch("spinup_helper.subprocess.run")
    def test_term_exec_builds_argv_and_returns_stdout(self, run):
        run.return_value = self._ok(stdout="DONE")
        out = sh.term_exec("tid-123", "bin/conductor-setup")
        self.assertEqual(out, "DONE")
        self.assertEqual(
            run.call_args[0][0],
            ["atlas-cli", "term", "exec", "tid-123", "bin/conductor-setup"],
        )

    @patch("spinup_helper.subprocess.run")
    def test_term_send_appends_data(self, run):
        run.return_value = self._ok()
        sh.term_send("tid-123", "bin/conductor-server\n")
        self.assertEqual(
            run.call_args[0][0],
            ["atlas-cli", "term", "send", "tid-123", "bin/conductor-server\n"],
        )

    @patch("spinup_helper.subprocess.run")
    def test_claude_new_returns_session_id(self, run):
        run.return_value = self._ok(stdout='{"id": "sess-9"}')
        self.assertEqual(sh.claude_new("proj-5467", "do the thing"), "sess-9")
        self.assertEqual(
            run.call_args[0][0],
            ["atlas-cli", "--json", "claude", "new", "proj-5467", "--prompt", "do the thing"],
        )

    @patch("spinup_helper.subprocess.run")
    def test_term_exec_raises_on_failure(self, run):
        run.return_value = self._ok(returncode=1, stderr="boom")
        with self.assertRaises(sh.AtlasError):
            sh.term_exec("tid-123", "bad")


# ---------------------------------------------------------------------------
# Task 3: Shared spin-up chain + named-workspace creation
# ---------------------------------------------------------------------------

class SpinupChainTests(unittest.TestCase):
    @patch("spinup_helper.claude_new")
    @patch("spinup_helper.term_send")
    @patch("spinup_helper.term_exec", return_value="setup-done")
    @patch("spinup_helper.term_new", side_effect=["setup-tid"])
    @patch("spinup_helper.open_atlas_workspace")
    @patch(
        "spinup_helper.create_atlas_workspace_named",
        return_value={"id": "ws-1", "name": "proj-5467", "worktree_path": "/wt"},
    )
    def test_chain_runs_setup_only(
        self, create, open_ws, t_new, t_exec, t_send, c_new
    ):
        result = sh.run_spinup_chain("proj-5467", "internal/proj-5467-x")
        # term_new called exactly once (setup tab only)
        self.assertEqual(t_new.call_count, 1)
        # setup blocks via term_exec on the setup terminal
        t_exec.assert_called_once_with("setup-tid", "bin/conductor-setup")
        # dev server is NOT started; puma-dev lazy-boots on demand
        t_send.assert_not_called()
        # claude_new is NOT called -- user opens the work tab themselves
        c_new.assert_not_called()
        self.assertEqual(result["workspace_id"], "ws-1")
        self.assertNotIn("work_session_id", result)

    @patch("spinup_helper.subprocess.run")
    def test_create_named_passes_explicit_name(self, run):
        m = MagicMock()
        m.returncode = 0
        m.stdout = '{"id":"ws","name":"pr-1"}'
        m.stderr = ""
        run.return_value = m
        sh.create_atlas_workspace_named("internal/foo", "pr-1")
        args = run.call_args[0][0]
        self.assertIn("pr-1", args)
        self.assertIn("--use-existing", args)
        self.assertIn("internal/foo", args)


# ---------------------------------------------------------------------------
# Task 4: Route cmd_spinup through the chain
# ---------------------------------------------------------------------------

class CmdSpinupTests(unittest.TestCase):
    @patch("spinup_helper.mark_spunup")
    @patch(
        "spinup_helper.run_spinup_chain",
        return_value={
            "workspace_id": "ws",
            "workspace_name": "proj-5467",
            "worktree_path": "/wt",
            "setup_terminal_id": "tid",
        },
    )
    @patch("spinup_helper.transition_to_in_progress", return_value=True)
    @patch(
        "spinup_helper.fetch_ticket",
        return_value={
            "key": "PROJ-5467",
            "title": "Render placeholder",
            "issue_type": "Task",
            "status": "To Do",
        },
    )
    def test_cmd_spinup_uses_chain_and_emits_suggested_prompt(self, fetch, trans, chain, mark):
        args = type("A", (), {"ticket": "PROJ-5467"})()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = sh.cmd_spinup(args)
        self.assertEqual(rc, 0)
        # chain called with workspace name + branch only (no prompt arg)
        called = chain.call_args
        self.assertEqual(called[0][0], "proj-5467")                       # workspace name
        self.assertTrue(called[0][1].startswith("internal/proj-5467"))    # branch
        self.assertEqual(len(called[0]), 2)                              # exactly 2 positional args
        mark.assert_called_once_with("PROJ-5467")
        # output JSON has suggested_prompt, not work_session_id
        out = json.loads(buf.getvalue())
        self.assertIn("suggested_prompt", out)
        self.assertIn("writing-plans for PROJ-5467", out["suggested_prompt"])
        self.assertNotIn("work_session_id", out)


# ---------------------------------------------------------------------------
# Task 5: PR detection (GitHub)
# ---------------------------------------------------------------------------

class PRDetectionTests(unittest.TestCase):
    @patch("spinup_helper.subprocess.run")
    def test_list_review_requests_maps_fields(self, run):
        payload = json.dumps([
            {
                "number": 45647,
                "title": "BUGFIX: x",
                "author": {"login": "alice"},
                "url": "https://github.com/your-org/your-app/pull/45647",
                "createdAt": "2026-06-02T12:56:33Z",
            },
        ])
        m = MagicMock()
        m.returncode = 0
        m.stdout = payload
        m.stderr = ""
        run.return_value = m
        prs = sh.list_review_requests()
        self.assertEqual(prs[0]["number"], 45647)
        self.assertEqual(prs[0]["author"], "alice")
        self.assertEqual(prs[0]["url"].rsplit("/", 1)[-1], "45647")
        argv = run.call_args[0][0]
        self.assertIn("--review-requested=@me", argv)
        self.assertIn("--repo", argv)
        self.assertIn(sh.GH_REPO, argv)

    @patch("spinup_helper.subprocess.run")
    def test_resolve_pr_head_branch(self, run):
        m = MagicMock()
        m.returncode = 0
        m.stdout = '{"headRefName":"internal/foo"}'
        m.stderr = ""
        run.return_value = m
        self.assertEqual(sh.resolve_pr_head_branch(45647), "internal/foo")

    @patch("spinup_helper.subprocess.run")
    def test_list_review_requests_raises_on_failure(self, run):
        m = MagicMock()
        m.returncode = 1
        m.stdout = ""
        m.stderr = "gh boom"
        run.return_value = m
        with self.assertRaises(sh.GitHubError):
            sh.list_review_requests()

    @patch("spinup_helper.subprocess.run")
    def test_resolve_pr_head_branch_raises_on_non_json(self, run):
        m = MagicMock()
        m.returncode = 0
        m.stdout = "<html>rate limited</html>"
        m.stderr = ""
        run.return_value = m
        with self.assertRaises(sh.GitHubError):
            sh.resolve_pr_head_branch(1)


# ---------------------------------------------------------------------------
# Task 6: PR state & decision logging
# ---------------------------------------------------------------------------

class PRStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = sh.PR_CACHE_PATH
        sh.PR_CACHE_PATH = pathlib.Path(self.tmp.name) / "pr-surfaced.json"

    def tearDown(self):
        sh.PR_CACHE_PATH = self._orig
        self.tmp.cleanup()

    def test_mark_pr_surfaced_is_idempotent(self):
        meta = {"number": 45647, "author": "alice", "title": "x", "url": "u"}
        sh.mark_pr_surfaced(45647, meta)
        first = sh.load_pr_state()["45647"]["first_surfaced"]
        sh.mark_pr_surfaced(45647, meta)  # second call must not overwrite
        self.assertEqual(sh.load_pr_state()["45647"]["first_surfaced"], first)
        self.assertEqual(sh.load_pr_state()["45647"]["decision"], "pending")

    def test_surfaced_pr_numbers(self):
        sh.mark_pr_surfaced(1, {"number": 1, "author": "a", "title": "t", "url": "u"})
        sh.mark_pr_surfaced(2, {"number": 2, "author": "b", "title": "t", "url": "u"})
        self.assertEqual(sh.surfaced_pr_numbers(), {1, 2})

    def test_record_pr_decision(self):
        sh.mark_pr_surfaced(45647, {"number": 45647, "author": "alice", "title": "x", "url": "u"})
        sh.record_pr_decision(45647, "review")
        entry = sh.load_pr_state()["45647"]
        self.assertEqual(entry["decision"], "review")
        self.assertIsNotNone(entry["decided_at"])


# ---------------------------------------------------------------------------
# Task 7: spinup-pr subcommand
# ---------------------------------------------------------------------------

class CmdSpinupPRTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = sh.PR_CACHE_PATH
        sh.PR_CACHE_PATH = pathlib.Path(self.tmp.name) / "pr-surfaced.json"

    def tearDown(self):
        sh.PR_CACHE_PATH = self._orig
        self.tmp.cleanup()

    @patch("spinup_helper.record_pr_decision")
    @patch("spinup_helper.run_spinup_chain")
    def test_skip_logs_decision_no_chain(self, chain, record):
        args = type("A", (), {"number": 45647, "skip": True})()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sh.cmd_spinup_pr(args), 0)
        chain.assert_not_called()
        record.assert_called_once_with(45647, "skip")

    @patch("spinup_helper.record_pr_decision")
    @patch(
        "spinup_helper.run_spinup_chain",
        return_value={
            "workspace_id": "ws",
            "workspace_name": "pr-45647",
            "worktree_path": "/wt",
            "setup_terminal_id": "tid",
        },
    )
    @patch("spinup_helper.fetch_branch")
    @patch("spinup_helper.resolve_pr_head_branch", return_value="internal/foo")
    def test_review_resolves_branch_fetches_and_emits_suggested_prompt(
        self, resolve, fetch, chain, record
    ):
        args = type("A", (), {"number": 45647, "skip": False})()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(sh.cmd_spinup_pr(args), 0)
        fetch.assert_called_once_with("internal/foo")
        called = chain.call_args[0]
        self.assertEqual(called[0], "pr-45647")            # workspace name
        self.assertEqual(called[1], "internal/foo")        # head branch
        self.assertEqual(len(called), 2)                   # exactly 2 positional args (no prompt)
        record.assert_called_once_with(45647, "review")
        # output JSON has suggested_prompt, not work_session_id
        out = json.loads(buf.getvalue())
        self.assertIn("suggested_prompt", out)
        self.assertIn("lens-review for PR #45647", out["suggested_prompt"])
        self.assertNotIn("work_session_id", out)


# ---------------------------------------------------------------------------
# Task 8: notify_macos
# ---------------------------------------------------------------------------

class NotifyMacosTests(unittest.TestCase):
    @patch("spinup_helper.shutil.which", return_value="/opt/homebrew/bin/terminal-notifier")
    @patch("spinup_helper.subprocess.run")
    def test_prefers_terminal_notifier_when_available(self, run, which):
        run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        sh.notify_macos("Atlas: 1 PR review", "Hello world")
        self.assertTrue(run.called)
        args = run.call_args[0][0]
        self.assertEqual(args[0], "terminal-notifier")
        self.assertIn("-title", args)
        self.assertIn("Atlas: 1 PR review", args)
        self.assertIn("-message", args)
        self.assertIn("Hello world", args)

    @patch("spinup_helper.shutil.which", return_value=None)
    @patch("spinup_helper.subprocess.run")
    def test_falls_back_to_osascript_when_terminal_notifier_absent(self, run, which):
        run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        sh.notify_macos("Atlas: 1 PR review", "Hello world")
        self.assertTrue(run.called)
        args = run.call_args[0][0]
        self.assertEqual(args[0], "osascript")
        self.assertEqual(args[1], "-e")
        script = args[2]
        self.assertIn("display notification", script)
        self.assertIn("Hello world", script)
        self.assertIn("Atlas: 1 PR review", script)

    @patch("spinup_helper.shutil.which", return_value="/opt/homebrew/bin/terminal-notifier")
    @patch("spinup_helper.subprocess.run", side_effect=OSError("no terminal-notifier"))
    def test_does_not_raise_on_subprocess_error_terminal_notifier(self, run, which):
        # Must not raise even when subprocess.run raises (terminal-notifier path).
        sh.notify_macos("title", "message")

    @patch("spinup_helper.shutil.which", return_value=None)
    @patch("spinup_helper.subprocess.run", side_effect=OSError("no osascript"))
    def test_does_not_raise_on_subprocess_error_osascript(self, run, which):
        # Must not raise even when subprocess.run raises (osascript fallback path).
        sh.notify_macos("title", "message")


# ---------------------------------------------------------------------------
# Task 9: poll subcommand
# ---------------------------------------------------------------------------

class CmdPollTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._cache, self._pr = sh.CACHE_PATH, sh.PR_CACHE_PATH
        sh.CACHE_PATH = pathlib.Path(self.tmp.name) / "spinup-surfaced.json"
        sh.PR_CACHE_PATH = pathlib.Path(self.tmp.name) / "pr-surfaced.json"

    def tearDown(self):
        sh.CACHE_PATH, sh.PR_CACHE_PATH = self._cache, self._pr
        self.tmp.cleanup()

    @patch("spinup_helper.notify_macos")
    @patch("spinup_helper.list_review_requests", return_value=[])
    @patch("spinup_helper.mark_spunup")
    @patch(
        "spinup_helper.run_spinup_chain",
        return_value={
            "workspace_id": "ws",
            "workspace_name": "proj-1",
            "worktree_path": "/wt",
            "setup_terminal_id": "tid",
        },
    )
    @patch("spinup_helper.transition_to_in_progress", return_value=True)
    @patch(
        "spinup_helper.list_assigned_eligible",
        return_value=[{"key": "PROJ-1", "title": "T", "issue_type": "Bug", "status": "To Do"}],
    )
    def test_poll_autospins_new_jira(self, elig, trans, chain, mark, prs, notify):
        with contextlib.redirect_stdout(io.StringIO()):
            rc = sh.cmd_poll(type("A", (), {})())
        self.assertEqual(rc, 0)
        chain.assert_called_once()
        mark.assert_called_once_with("PROJ-1")
        # chain called with 2-arg signature (no prompt)
        called = chain.call_args[0]
        self.assertEqual(called[0], "proj-1")
        self.assertEqual(len(called), 2)

    @patch("spinup_helper.notify_macos")
    @patch("spinup_helper.list_review_requests", return_value=[])
    @patch("spinup_helper.mark_spunup")
    @patch(
        "spinup_helper.run_spinup_chain",
        return_value={
            "workspace_id": "ws",
            "workspace_name": "proj-1",
            "worktree_path": "/wt",
            "setup_terminal_id": "tid",
        },
    )
    @patch("spinup_helper.transition_to_in_progress", return_value=True)
    @patch(
        "spinup_helper.list_assigned_eligible",
        return_value=[{"key": "PROJ-1", "title": "T", "issue_type": "Bug", "status": "To Do"}],
    )
    def test_poll_notifies_when_jira_spun_up(self, elig, trans, chain, mark, prs, notify):
        with contextlib.redirect_stdout(io.StringIO()):
            sh.cmd_poll(type("A", (), {})())
        notify.assert_called_once()
        title, message = notify.call_args[0]
        self.assertIn("ready", title)
        self.assertIn("PROJ-1", message)
        self.assertIn("ready to plan", message)

    @patch("spinup_helper.notify_macos")
    @patch("spinup_helper.run_spinup_chain")
    @patch("spinup_helper.list_assigned_eligible", return_value=[])
    @patch(
        "spinup_helper.list_review_requests",
        return_value=[
            {"number": 45647, "title": "x", "author": "alice", "url": "u", "created_at": ""},
        ],
    )
    def test_poll_surfaces_new_prs_once(self, prs, elig, chain, notify):
        with contextlib.redirect_stdout(io.StringIO()):
            sh.cmd_poll(type("A", (), {})())          # first cycle surfaces it
        self.assertIn(45647, sh.surfaced_pr_numbers())
        # second cycle must NOT re-surface (dedup)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sh.cmd_poll(type("A", (), {})())
        self.assertEqual(json.loads(buf.getvalue())["prs_to_surface"], [])

    @patch("spinup_helper.notify_macos")
    @patch("spinup_helper.list_assigned_eligible", return_value=[])
    @patch(
        "spinup_helper.list_review_requests",
        return_value=[
            {"number": 45647, "title": "x", "author": "alice", "url": "u", "created_at": ""},
            {"number": 45648, "title": "y", "author": "bob", "url": "u2", "created_at": ""},
        ],
    )
    def test_poll_notifies_once_for_new_prs(self, prs, elig, notify):
        with contextlib.redirect_stdout(io.StringIO()):
            sh.cmd_poll(type("A", (), {})())
        notify.assert_called_once()
        title, message = notify.call_args[0]
        self.assertIn("PR", title)
        self.assertIn("#45647", message)
        self.assertIn("alice", message)
        self.assertIn("#45648", message)
        self.assertIn("bob", message)
        self.assertIn("/spinup #45647", message)

    @patch("spinup_helper.notify_macos")
    @patch("spinup_helper.list_assigned_eligible", return_value=[])
    @patch("spinup_helper.list_review_requests", return_value=[])
    def test_poll_does_not_notify_when_nothing_new(self, prs, elig, notify):
        with contextlib.redirect_stdout(io.StringIO()):
            sh.cmd_poll(type("A", (), {})())
        notify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
