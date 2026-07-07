import io
import contextlib
import json
import os
import pathlib
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch, MagicMock, call
import cmux_chain as cc
import spinup_helper as sh


def _run(stdout="", returncode=0, stderr=""):
    m = MagicMock(); m.stdout = stdout; m.returncode = returncode; m.stderr = stderr
    return m


class WrapperTests(unittest.TestCase):
    @patch("cmux_chain.subprocess.run")
    def test_cmux_new_workspace_parses_ref(self, run):
        run.return_value = _run(stdout="OK workspace:2\n")
        ref = cc.cmux_new_workspace("/wt", "echo hi", "proj-1")
        self.assertEqual(ref, "workspace:2")
        argv = run.call_args[0][0]
        self.assertEqual(argv[:3], ["cmux", "new-workspace", "--cwd"])
        self.assertIn("--command", argv); self.assertIn("echo hi", argv)
        self.assertIn("--name", argv); self.assertIn("proj-1", argv)

    @patch("cmux_chain.subprocess.run")
    def test_cmux_new_surface_parses_ref(self, run):
        run.return_value = _run(stdout="OK surface:5 workspace:2\n")
        ref = cc.cmux_new_surface("workspace:2", surface_type="terminal")
        self.assertEqual(ref, "surface:5")
        argv = run.call_args[0][0]
        self.assertEqual(argv, ["cmux", "new-surface", "--workspace", "workspace:2", "--type", "terminal"])

    @patch("cmux_chain.subprocess.run")
    def test_cmux_new_surface_browser_includes_url(self, run):
        run.return_value = _run(stdout="OK surface:6 workspace:2\n")
        cc.cmux_new_surface("workspace:2", surface_type="browser", url="https://admin.proj-1.test")
        argv = run.call_args[0][0]
        self.assertIn("--type", argv); self.assertIn("browser", argv)
        self.assertIn("--url", argv); self.assertIn("https://admin.proj-1.test", argv)

    @patch("cmux_chain.subprocess.run")
    def test_send_and_send_key(self, run):
        run.return_value = _run(stdout="OK surface:5 workspace:2")
        cc.cmux_send("surface:5", "bin/dev")
        self.assertEqual(run.call_args[0][0], ["cmux", "send", "--surface", "surface:5", "bin/dev"])
        cc.cmux_send_key("surface:5", "enter")
        self.assertEqual(run.call_args[0][0], ["cmux", "send-key", "--surface", "surface:5", "enter"])

    @patch("cmux_chain.subprocess.run")
    def test_notify_argv(self, run):
        run.return_value = _run()
        cc.cmux_notify("PROJ-1 ready", "plan running", workspace="workspace:2")
        argv = run.call_args[0][0]
        self.assertEqual(argv[:2], ["cmux", "notify"])
        self.assertIn("--title", argv); self.assertIn("PROJ-1 ready", argv)
        self.assertIn("--body", argv); self.assertIn("plan running", argv)
        self.assertIn("--workspace", argv); self.assertIn("workspace:2", argv)

    @patch("cmux_chain.subprocess.run")
    def test_cmux_failure_raises(self, run):
        run.return_value = _run(returncode=1, stderr="broken pipe")
        with self.assertRaises(cc.CmuxError):
            cc.cmux_new_workspace("/wt", "echo hi", "proj-1")

    @patch("cmux_chain.subprocess.run")
    def test_git_worktree_add_new_branch(self, run):
        run.return_value = _run()
        cc.git_worktree_add("/wt/proj-1", "feature/proj-1-x", new_branch=True)
        argv = run.call_args[0][0]
        self.assertEqual(argv[:4], ["git", "-C", str(cc.MAIN_REPO), "worktree"])
        self.assertIn("add", argv); self.assertIn("-b", argv)
        self.assertIn("feature/proj-1-x", argv); self.assertIn("/wt/proj-1", argv)

    @patch("cmux_chain.subprocess.run")
    def test_git_worktree_add_existing_branch(self, run):
        run.return_value = _run()
        cc.git_worktree_add("/wt/pr-9", "internal/foo", new_branch=False)
        argv = run.call_args[0][0]
        self.assertNotIn("-b", argv)
        self.assertIn("internal/foo", argv)


class WorktreeTests(unittest.TestCase):
    def test_names(self):
        self.assertEqual(cc.worktree_name_for_ticket("PROJ-5487"), "proj-5487")
        self.assertEqual(cc.worktree_name_for_pr(45647), "pr-45647")

    def test_worktree_path(self):
        self.assertEqual(cc.worktree_path("proj-5487"),
                         str(cc.WORKTREE_BASE / "proj-5487"))

    @patch("cmux_chain._ensure_base_dir")
    @patch("cmux_chain.git_worktree_add")
    @patch("cmux_chain.os.path.isdir", return_value=False)
    def test_ensure_worktree_creates_new_branch(self, isdir, add, mkd):
        p = cc.ensure_worktree("proj-1", "feature/proj-1-x", new_branch=True)
        self.assertEqual(p, str(cc.WORKTREE_BASE / "proj-1"))
        add.assert_called_once_with(str(cc.WORKTREE_BASE / "proj-1"), "feature/proj-1-x", True)

    @patch("cmux_chain._ensure_base_dir")
    @patch("cmux_chain.git_worktree_add")
    @patch("cmux_chain.os.path.isdir", return_value=True)
    def test_ensure_worktree_idempotent_when_path_exists(self, isdir, add, mkd):
        p = cc.ensure_worktree("proj-1", "feature/proj-1-x", new_branch=True)
        add.assert_not_called()  # already there -> reuse
        self.assertEqual(p, str(cc.WORKTREE_BASE / "proj-1"))


class SetupTabTests(unittest.TestCase):
    @patch("cmux_chain.cmux_new_workspace", return_value="workspace:3")
    def test_open_setup_tab_builds_command(self, neww):
        ws = cc.open_setup_tab("/wt/proj-1", "proj-1")
        self.assertEqual(ws, "workspace:3")
        cmd = neww.call_args[0][1]  # the --command string
        self.assertIn("WORKTREE_NAME=proj-1", cmd)
        self.assertIn(f"WORKTREE_ROOT_PATH={cc.MAIN_REPO}", cmd)
        self.assertIn("bin/worktree-setup", cmd)
        self.assertIn(".cmux-setup-status", cmd)
        self.assertNotIn("CONDUCTOR_", cmd)

    def test_wait_for_setup_success(self):
        import tempfile, os
        d = tempfile.mkdtemp()
        with open(os.path.join(d, ".cmux-setup-status"), "w") as f:
            f.write("EXIT:0\n")
        self.assertTrue(cc.wait_for_setup(d, timeout_s=2, poll_s=0.1))

    def test_wait_for_setup_failure(self):
        import tempfile, os
        d = tempfile.mkdtemp()
        with open(os.path.join(d, ".cmux-setup-status"), "w") as f:
            f.write("EXIT:17\n")
        self.assertFalse(cc.wait_for_setup(d, timeout_s=2, poll_s=0.1))

    def test_wait_for_setup_timeout(self):
        import tempfile
        self.assertFalse(cc.wait_for_setup(tempfile.mkdtemp(), timeout_s=0.3, poll_s=0.1))


class DevServerTests(unittest.TestCase):
    @patch("cmux_chain.cmux_send_key")
    @patch("cmux_chain.cmux_send")
    @patch("cmux_chain.cmux_new_surface", return_value="surface:7")
    @patch("cmux_chain.os.path.exists", return_value=False)
    def test_open_dev_server_tab(self, exists, news, send, sendkey):
        cc.open_dev_server_tab("workspace:3", "/wt/proj-1")
        news.assert_called_once_with("workspace:3", surface_type="terminal")
        send.assert_called_once_with("surface:7", "bin/dev")
        sendkey.assert_called_once_with("surface:7", "enter")

    @patch("cmux_chain.cmux_send_key")
    @patch("cmux_chain.cmux_send")
    @patch("cmux_chain.cmux_new_surface", return_value="surface:7")
    @patch("cmux_chain.os.remove")
    @patch("cmux_chain.dev_server_up", return_value=False)
    @patch("cmux_chain.os.path.exists", return_value=True)
    def test_removes_stale_overmind_sock_before_start(self, exists, up, rm, news, send, sendkey):
        # Sock exists but no live overmind owns it -> dead leftover; must be removed
        # or `bin/dev`'s overmind refuses to start.
        cc.open_dev_server_tab("workspace:3", "/wt/proj-1")
        rm.assert_called_once_with(os.path.join("/wt/proj-1", ".overmind.sock"))

    @patch("cmux_chain.cmux_send_key")
    @patch("cmux_chain.cmux_send")
    @patch("cmux_chain.cmux_new_surface", return_value="surface:7")
    @patch("cmux_chain.os.remove")
    @patch("cmux_chain.dev_server_up", return_value=True)
    @patch("cmux_chain.os.path.exists", return_value=True)
    def test_keeps_live_overmind_sock(self, exists, up, rm, news, send, sendkey):
        # A live overmind owns the sock -> never remove it.
        cc.open_dev_server_tab("workspace:3", "/wt/proj-1")
        rm.assert_not_called()


class DevServerUpTests(unittest.TestCase):
    @patch("cmux_chain.subprocess.run")
    @patch("cmux_chain.os.path.exists", return_value=True)
    def test_up_when_overmind_web_running(self, exists, run):
        run.return_value = _run(stdout="PROCESS PID STATUS\nweb 1764 running\nworker 1765 running\n")
        self.assertTrue(cc.dev_server_up("/wt"))

    @patch("cmux_chain.os.path.exists", return_value=False)
    def test_down_when_no_overmind_sock(self, exists):
        self.assertFalse(cc.dev_server_up("/wt"))

    @patch("cmux_chain.subprocess.run")
    @patch("cmux_chain.os.path.exists", return_value=True)
    def test_down_when_web_not_running(self, exists, run):
        run.return_value = _run(stdout="PROCESS PID STATUS\nweb 1764 dead\n")
        self.assertFalse(cc.dev_server_up("/wt"))

    @patch("cmux_chain.dev_server_up", side_effect=[False, True])
    def test_wait_for_dev_server_succeeds(self, up):
        self.assertTrue(cc.wait_for_dev_server("/wt", timeout_s=5, poll_s=0.1))

    @patch("cmux_chain.dev_server_up", return_value=False)
    def test_wait_for_dev_server_times_out(self, up):
        self.assertFalse(cc.wait_for_dev_server("/wt", timeout_s=0.3, poll_s=0.1))


class AgentTabTests(unittest.TestCase):
    @patch("cmux_chain.read_surface")
    @patch("cmux_chain._claude_transcripts")
    @patch("cmux_chain.time")
    @patch("cmux_chain.cmux_send_key")
    @patch("cmux_chain.cmux_send")
    @patch("cmux_chain.cmux_new_surface", return_value="surface:9")
    def test_open_agent_tab_submits_on_first_enter(self, news, send, sendkey, mock_time, transcripts, read_surf):
        """Case A: TUI shows ❯ immediately; prompt appears in pane; transcript appears on first backstop check."""
        existing = {pathlib.Path("/fake/old.jsonl")}
        new_transcript = {pathlib.Path("/fake/old.jsonl"), pathlib.Path("/fake/new.jsonl")}

        prompt = "writing-plans for PROJ-1, ..."
        # read_surface calls:
        #   1. wait_for_claude_ready poll -> returns ❯ (ready immediately)
        #   2. wait_for_text_visible poll -> returns the prompt prefix (visible immediately)
        read_surf.side_effect = ["❯ some text", f"❯ {prompt}"]

        # _claude_transcripts calls:
        #   1. before snapshot
        #   2. first backstop loop check -> new transcript appeared
        transcripts.side_effect = [existing, new_transcript]

        # monotonic sequence: each poller deadline + each backstop check
        # wait_for_claude_ready: deadline set (0.0), then check inside loop (0.1) -> found
        # wait_for_text_visible: deadline set (0.2), then check inside loop (0.3) -> found
        # backstop loop: deadline set (0.4), first check (0.5) -> transcript appeared, break
        mock_time.monotonic.side_effect = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5]
        mock_time.sleep = MagicMock()

        cc.open_agent_tab("workspace:3", prompt, "/wt/proj-1")

        news.assert_called_once_with("workspace:3", surface_type="terminal")
        # send("claude") happens BEFORE read_surface returns ❯
        self.assertEqual(send.call_args_list[0][0], ("surface:9", "claude"))
        # send(prompt) happens AFTER ❯ was seen (read_surface[0] returned ❯)
        self.assertEqual(send.call_args_list[1][0], ("surface:9", prompt))
        # The send(prompt) must come after the first read_surface call saw ❯
        claude_send_idx = send.call_args_list.index(call("surface:9", "claude"))
        prompt_send_idx = send.call_args_list.index(call("surface:9", prompt))
        self.assertLess(claude_send_idx, prompt_send_idx)
        # exactly 2 send-key enter calls: after 'claude' (launch) and after the prompt (submit); no retry
        self.assertEqual(sendkey.call_count, 2)
        self.assertEqual(sendkey.call_args_list[0][0], ("surface:9", "enter"))
        self.assertEqual(sendkey.call_args_list[1][0], ("surface:9", "enter"))

    @patch("cmux_chain.read_surface")
    @patch("cmux_chain._claude_transcripts")
    @patch("cmux_chain.time")
    @patch("cmux_chain.cmux_send_key")
    @patch("cmux_chain.cmux_send")
    @patch("cmux_chain.cmux_new_surface", return_value="surface:9")
    def test_open_agent_tab_retries_enter_when_needed(self, news, send, sendkey, mock_time, transcripts, read_surf):
        """Case B: transcript empty on first backstop check -> retry Enter; appears on second check."""
        existing = {pathlib.Path("/fake/old.jsonl")}
        new_transcript = {pathlib.Path("/fake/old.jsonl"), pathlib.Path("/fake/new.jsonl")}

        prompt = "writing-plans for PROJ-1, ..."
        # read_surface: ❯ ready immediately, prompt visible immediately
        read_surf.side_effect = ["❯ some text", f"❯ {prompt}"]

        # _claude_transcripts: before=existing, first backstop check=existing (no new), second=new_transcript
        transcripts.side_effect = [existing, existing, new_transcript]

        # monotonic: ready poller (deadline+check), text poller (deadline+check),
        #            backstop (deadline, first check within, second check within)
        mock_time.monotonic.side_effect = [0.0, 0.1, 0.2, 0.3, 0.4, 1.0, 3.0]
        mock_time.sleep = MagicMock()

        cc.open_agent_tab("workspace:3", prompt, "/wt/proj-1")

        news.assert_called_once_with("workspace:3", surface_type="terminal")
        self.assertEqual(send.call_args_list[0][0], ("surface:9", "claude"))
        self.assertEqual(send.call_args_list[1][0], ("surface:9", prompt))
        # 3 send-key enter calls: after 'claude', initial submit after prompt, + 1 retry
        self.assertEqual(sendkey.call_count, 3)
        self.assertEqual(sendkey.call_args_list[0][0], ("surface:9", "enter"))
        self.assertEqual(sendkey.call_args_list[1][0], ("surface:9", "enter"))
        self.assertEqual(sendkey.call_args_list[2][0], ("surface:9", "enter"))


class BrowserTabTests(unittest.TestCase):
    @patch("cmux_chain.cmux_new_surface", return_value="surface:10")
    def test_open_browser_tab(self, news):
        s = cc.open_browser_tab("workspace:3", "proj-1")
        self.assertEqual(s, "surface:10")
        news.assert_called_once_with("workspace:3", surface_type="browser",
                                     url="https://admin.proj-1.test")


class RefBrowserPaneTests(unittest.TestCase):
    @patch("cmux_chain._cmux")
    def test_open_ref_browser_pane_calls_new_pane(self, cmux):
        cc.open_ref_browser_pane("workspace:3", "https://your-jira-instance/browse/PROJ-1")
        cmux.assert_called_once_with([
            "new-pane", "--workspace", "workspace:3",
            "--type", "browser",
            "--url", "https://your-jira-instance/browse/PROJ-1",
        ])

    @patch("cmux_chain._cmux", side_effect=cc.CmuxError("fail"))
    def test_open_ref_browser_pane_swallows_cmux_error(self, cmux):
        # Must not raise even when _cmux fails
        cc.open_ref_browser_pane("workspace:3", "https://your-jira-instance/browse/PROJ-1")


class PortDetectionTests(unittest.TestCase):
    def test_read_worktree_port_present(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, ".env"), "w") as f:
                f.write("SOME_VAR=hello\nPORT=3060\nOTHER=x\n")
            self.assertEqual(cc.read_worktree_port(d), 3060)

    def test_read_worktree_port_missing_env_file(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(cc.read_worktree_port(d))

    def test_read_worktree_port_no_port_line(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, ".env"), "w") as f:
                f.write("RAILS_ENV=development\n")
            self.assertIsNone(cc.read_worktree_port(d))

    def test_port_in_use_true_when_listener_active(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            self.assertTrue(cc.port_in_use(port))
        finally:
            srv.close()

    def test_port_in_use_false_after_socket_closed(self):
        # Bind to a port, record it, close it -- then verify it's free
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.assertFalse(cc.port_in_use(port))


class FindFreePortTests(unittest.TestCase):
    @patch("cmux_chain.port_in_use", side_effect=[True, True, False])
    def test_returns_first_free_port(self, piu):
        result = cc.find_free_port(3060)
        self.assertEqual(result, 3062)  # 3060 in use, 3061 in use, 3062 free

    @patch("cmux_chain.port_in_use", return_value=True)
    def test_raises_when_no_free_port_found(self, piu):
        with self.assertRaises(cc.CmuxError):
            cc.find_free_port(3060, attempts=5)
        # Should have checked exactly 5 ports (3060..3064)
        self.assertEqual(piu.call_count, 5)

    @patch("cmux_chain.port_in_use", return_value=False)
    def test_returns_start_when_immediately_free(self, piu):
        result = cc.find_free_port(3060)
        self.assertEqual(result, 3060)
        piu.assert_called_once_with(3060)


class RerunSetupWithPortTests(unittest.TestCase):
    @patch("cmux_chain.subprocess.run")
    def test_returns_true_on_zero_exit(self, run):
        run.return_value = _run(returncode=0)
        result = cc.rerun_setup_with_port("/wt/proj-1", "proj-1", 3070)
        self.assertTrue(result)
        call = run.call_args
        self.assertEqual(call[0][0], ["bin/worktree-setup"])
        self.assertEqual(call[1]["cwd"], "/wt/proj-1")
        env = call[1]["env"]
        self.assertEqual(env["WORKTREE_PORT"], "3070")
        self.assertEqual(env["WORKTREE_NAME"], "proj-1")
        self.assertEqual(env["WORKTREE_ROOT_PATH"], str(cc.MAIN_REPO))
        self.assertNotIn("CONDUCTOR_WORKSPACE_NAME", env)
        self.assertNotIn("CONDUCTOR_ROOT_PATH", env)

    @patch("cmux_chain.subprocess.run")
    def test_returns_false_on_nonzero_exit(self, run):
        run.return_value = _run(returncode=1, stderr="error")
        result = cc.rerun_setup_with_port("/wt/proj-1", "proj-1", 3070)
        self.assertFalse(result)

    @patch("cmux_chain.subprocess.run")
    def test_removes_existing_env_file_before_setup(self, run):
        """generate_env skips an existing .env; verify we delete it so it's regenerated."""
        run.return_value = _run(returncode=0)
        with tempfile.TemporaryDirectory() as d:
            env_path = os.path.join(d, ".env")
            with open(env_path, "w") as f:
                f.write("PORT=3060\n")
            result = cc.rerun_setup_with_port(d, "proj-x", 3061)
            self.assertTrue(result)
            self.assertFalse(os.path.exists(env_path),
                             ".env should be removed before worktree-setup runs")
            call = run.call_args
            self.assertEqual(call[0][0], ["bin/worktree-setup"])
            self.assertEqual(call[1]["env"]["WORKTREE_PORT"], "3061")

    @patch("cmux_chain.subprocess.run")
    def test_no_crash_when_env_file_absent(self, run):
        """If .env doesn't exist, rerun_setup_with_port must not raise."""
        run.return_value = _run(returncode=0)
        with tempfile.TemporaryDirectory() as d:
            # No .env created — should succeed silently
            result = cc.rerun_setup_with_port(d, "proj-x", 3061)
            self.assertTrue(result)
            call = run.call_args
            self.assertEqual(call[1]["env"]["WORKTREE_PORT"], "3061")


class ChainTests(unittest.TestCase):
    @patch("cmux_chain.open_ref_browser_pane")
    @patch("cmux_chain.cmux_notify")
    @patch("cmux_chain.open_browser_tab")
    @patch("cmux_chain.open_agent_tab")
    @patch("cmux_chain.wait_for_dev_server", return_value=True)
    @patch("cmux_chain.open_dev_server_tab")
    @patch("cmux_chain.port_in_use", return_value=False)
    @patch("cmux_chain.read_worktree_port", return_value=3060)
    @patch("cmux_chain.wait_for_setup", return_value=True)
    @patch("cmux_chain.open_setup_tab", return_value="workspace:3")
    @patch("cmux_chain.ensure_worktree", return_value="/wt/proj-1")
    def test_chain_happy_path_runs_all_tabs_in_order(
        self, ew, setup, wsetup, rwp, piu, dev, wserve, agent, browser, notify, ref_pane
    ):
        rc = cc.run_chain("proj-1", "feature/proj-1-x", new_branch=True, prompt="P")
        self.assertEqual(rc["status"], "ok")
        setup.assert_called_once(); dev.assert_called_once()
        agent.assert_called_once_with("workspace:3", "P", "/wt/proj-1")
        browser.assert_called_once_with("workspace:3", "proj-1")
        notify.assert_called()  # ready notification
        ref_pane.assert_not_called()  # no ref_url passed

    @patch("cmux_chain.open_ref_browser_pane")
    @patch("cmux_chain.cmux_notify")
    @patch("cmux_chain.open_browser_tab")
    @patch("cmux_chain.open_agent_tab")
    @patch("cmux_chain.wait_for_dev_server", return_value=True)
    @patch("cmux_chain.open_dev_server_tab")
    @patch("cmux_chain.port_in_use", return_value=False)
    @patch("cmux_chain.read_worktree_port", return_value=3060)
    @patch("cmux_chain.wait_for_setup", return_value=True)
    @patch("cmux_chain.open_setup_tab", return_value="workspace:3")
    @patch("cmux_chain.ensure_worktree", return_value="/wt/proj-1")
    def test_chain_ref_url_opens_ref_pane(
        self, ew, setup, wsetup, rwp, piu, dev, wserve, agent, browser, notify, ref_pane
    ):
        rc = cc.run_chain("proj-1", "feature/proj-1-x", new_branch=True, prompt="P",
                          ref_url="https://x/PROJ-1")
        self.assertEqual(rc["status"], "ok")
        ref_pane.assert_called_once_with("workspace:3", "https://x/PROJ-1")

    @patch("cmux_chain.open_ref_browser_pane")
    @patch("cmux_chain.cmux_notify")
    @patch("cmux_chain.open_browser_tab")
    @patch("cmux_chain.open_agent_tab")
    @patch("cmux_chain.wait_for_dev_server", return_value=False)  # serving times out
    @patch("cmux_chain.open_dev_server_tab")
    @patch("cmux_chain.port_in_use", return_value=False)
    @patch("cmux_chain.read_worktree_port", return_value=3060)
    @patch("cmux_chain.wait_for_setup", return_value=True)
    @patch("cmux_chain.open_setup_tab", return_value="workspace:3")
    @patch("cmux_chain.ensure_worktree", return_value="/wt/proj-1")
    def test_chain_ref_url_opens_ref_pane_even_on_serving_timeout(
        self, ew, setup, wsetup, rwp, piu, dev, wserve, agent, browser, notify, ref_pane
    ):
        rc = cc.run_chain("proj-1", "feature/proj-1-x", new_branch=True, prompt="P",
                          ref_url="https://x/PROJ-1")
        self.assertEqual(rc["status"], "serving-timeout")
        browser.assert_not_called()          # test-app browser skipped (serving gated)
        ref_pane.assert_called_once_with("workspace:3", "https://x/PROJ-1")  # ref pane unconditional

    @patch("cmux_chain.open_ref_browser_pane")
    @patch("cmux_chain.cmux_notify")
    @patch("cmux_chain.open_dev_server_tab")
    @patch("cmux_chain.wait_for_setup", return_value=False)   # setup FAILS
    @patch("cmux_chain.open_setup_tab", return_value="workspace:3")
    @patch("cmux_chain.ensure_worktree", return_value="/wt/proj-1")
    def test_chain_stops_on_setup_failure(self, ew, setup, wsetup, dev, notify, ref_pane):
        rc = cc.run_chain("proj-1", "feature/proj-1-x", new_branch=True, prompt="P")
        self.assertEqual(rc["status"], "setup-failed")
        dev.assert_not_called()                  # gated: no dev server
        notify.assert_called()                   # failure notification
        ref_pane.assert_not_called()             # chain stopped before ref pane

    @patch("cmux_chain.open_ref_browser_pane")
    @patch("cmux_chain.cmux_notify")
    @patch("cmux_chain.open_browser_tab")
    @patch("cmux_chain.open_agent_tab")
    @patch("cmux_chain.wait_for_dev_server", return_value=False)  # never serves
    @patch("cmux_chain.open_dev_server_tab")
    @patch("cmux_chain.port_in_use", return_value=False)
    @patch("cmux_chain.read_worktree_port", return_value=3060)
    @patch("cmux_chain.wait_for_setup", return_value=True)
    @patch("cmux_chain.open_setup_tab", return_value="workspace:3")
    @patch("cmux_chain.ensure_worktree", return_value="/wt/proj-1")
    def test_chain_opens_agent_even_if_serving_times_out(
        self, ew, setup, wsetup, rwp, piu, dev, wserve, agent, browser, notify, ref_pane
    ):
        rc = cc.run_chain("proj-1", "feature/proj-1-x", new_branch=True, prompt="P")
        # per design failure table: still open the agent; skip the browser
        agent.assert_called_once()
        browser.assert_not_called()
        self.assertEqual(rc["status"], "serving-timeout")
        ref_pane.assert_not_called()  # no ref_url passed

    @patch("cmux_chain.open_ref_browser_pane")
    @patch("cmux_chain.rerun_setup_with_port", return_value=True)
    @patch("cmux_chain.find_free_port", return_value=3070)
    @patch("cmux_chain.dev_server_up", return_value=False)
    @patch("cmux_chain.port_in_use", return_value=True)
    @patch("cmux_chain.read_worktree_port", return_value=3060)
    @patch("cmux_chain.cmux_notify")
    @patch("cmux_chain.open_browser_tab")
    @patch("cmux_chain.open_agent_tab")
    @patch("cmux_chain.wait_for_dev_server", return_value=True)
    @patch("cmux_chain.open_dev_server_tab")
    @patch("cmux_chain.wait_for_setup", return_value=True)
    @patch("cmux_chain.open_setup_tab", return_value="workspace:3")
    @patch("cmux_chain.ensure_worktree", return_value="/wt/proj-1")
    def test_chain_port_conflict_reassigns_and_continues(
        self, ew, setup, wsetup, dev, wserve, agent, browser, notify, rwp, piu, dsu, ffp, rsp, ref_pane
    ):
        rc = cc.run_chain("proj-1", "feature/proj-1-x", new_branch=True, prompt="P")
        # Reassign succeeded -> chain continues to ok
        self.assertEqual(rc["status"], "ok")
        # find_free_port called with port+1
        ffp.assert_called_once_with(3061)
        # rerun_setup_with_port called with worktree, name, new port
        rsp.assert_called_once_with("/wt/proj-1", "proj-1", 3070)
        # Notify mentions reassignment
        notify_calls = notify.call_args_list
        reassign_notify = notify_calls[0]
        self.assertIn("3070", reassign_notify[0][0])  # title: "moved to port 3070"
        self.assertIn("3060", reassign_notify[0][1])  # body: "port 3060 was in use"
        self.assertIn("3070", reassign_notify[0][1])  # body: "reconfigured to 3070"
        # Dev server, agent, and browser all run
        dev.assert_called_once()
        agent.assert_called_once_with("workspace:3", "P", "/wt/proj-1")
        browser.assert_called_once_with("workspace:3", "proj-1")
        ref_pane.assert_not_called()  # no ref_url passed

    @patch("cmux_chain.open_ref_browser_pane")
    @patch("cmux_chain.rerun_setup_with_port", return_value=True)
    @patch("cmux_chain.find_free_port", return_value=3070)
    @patch("cmux_chain.dev_server_up", return_value=False)
    @patch("cmux_chain.port_in_use", return_value=True)
    @patch("cmux_chain.read_worktree_port", return_value=3060)
    @patch("cmux_chain.cmux_notify")
    @patch("cmux_chain.open_browser_tab")
    @patch("cmux_chain.open_agent_tab")
    @patch("cmux_chain.wait_for_dev_server", return_value=False)  # serving times out
    @patch("cmux_chain.open_dev_server_tab")
    @patch("cmux_chain.wait_for_setup", return_value=True)
    @patch("cmux_chain.open_setup_tab", return_value="workspace:3")
    @patch("cmux_chain.ensure_worktree", return_value="/wt/proj-1")
    def test_chain_port_conflict_reassign_then_serving_timeout(
        self, ew, setup, wsetup, dev, wserve, agent, browser, notify, rwp, piu, dsu, ffp, rsp, ref_pane
    ):
        rc = cc.run_chain("proj-1", "feature/proj-1-x", new_branch=True, prompt="P")
        # Reassigned but serving timed out -> serving-timeout (agent still runs, browser skipped)
        self.assertEqual(rc["status"], "serving-timeout")
        dev.assert_called_once()
        agent.assert_called_once()
        browser.assert_not_called()
        ref_pane.assert_not_called()  # no ref_url passed

    @patch("cmux_chain.open_ref_browser_pane")
    @patch("cmux_chain.kill_worktree_port_orphans", return_value=1)
    @patch("cmux_chain.rerun_setup_with_port")
    @patch("cmux_chain.find_free_port")
    @patch("cmux_chain.dev_server_up", return_value=False)
    @patch("cmux_chain.port_in_use", side_effect=[True, False])  # in use, then freed
    @patch("cmux_chain.read_worktree_port", return_value=3060)
    @patch("cmux_chain.cmux_notify")
    @patch("cmux_chain.open_browser_tab")
    @patch("cmux_chain.open_agent_tab")
    @patch("cmux_chain.wait_for_dev_server", return_value=True)
    @patch("cmux_chain.open_dev_server_tab")
    @patch("cmux_chain.wait_for_setup", return_value=True)
    @patch("cmux_chain.open_setup_tab", return_value="workspace:3")
    @patch("cmux_chain.ensure_worktree", return_value="/wt/proj-1")
    @patch("cmux_chain.time.sleep")
    def test_chain_orphan_reclaim_reuses_port_no_reassign(
        self, slp, ew, setup, wsetup, dev, wserve, agent, browser, notify, rwp, piu, dsu, ffp, rsp, kill, ref_pane
    ):
        rc = cc.run_chain("proj-1", "feature/proj-1-x", new_branch=True, prompt="P")
        # Orphan killed, port freed -> no reassign needed
        self.assertEqual(rc["status"], "ok")
        kill.assert_called_once_with("/wt/proj-1", 3060)
        rsp.assert_not_called()   # reassign NOT called
        ffp.assert_not_called()   # find_free_port NOT called
        dev.assert_called_once()
        agent.assert_called_once_with("workspace:3", "P", "/wt/proj-1")
        browser.assert_called_once_with("workspace:3", "proj-1")
        ref_pane.assert_not_called()  # no ref_url passed

    @patch("cmux_chain.open_ref_browser_pane")
    @patch("cmux_chain.rerun_setup_with_port", return_value=False)  # re-setup fails
    @patch("cmux_chain.find_free_port", return_value=3070)
    @patch("cmux_chain.dev_server_up", return_value=False)
    @patch("cmux_chain.port_in_use", return_value=True)
    @patch("cmux_chain.read_worktree_port", return_value=3060)
    @patch("cmux_chain.cmux_notify")
    @patch("cmux_chain.open_browser_tab")
    @patch("cmux_chain.open_agent_tab")
    @patch("cmux_chain.open_dev_server_tab")
    @patch("cmux_chain.wait_for_setup", return_value=True)
    @patch("cmux_chain.open_setup_tab", return_value="workspace:3")
    @patch("cmux_chain.ensure_worktree", return_value="/wt/proj-1")
    def test_chain_port_conflict_reassign_fails(
        self, ew, setup, wsetup, dev, agent, browser, notify, rwp, piu, dsu, ffp, rsp, ref_pane
    ):
        rc = cc.run_chain("proj-1", "feature/proj-1-x", new_branch=True, prompt="P")
        self.assertEqual(rc["status"], "port-reassign-failed")
        self.assertEqual(rc["port"], 3060)
        self.assertIn("workspace", rc)
        self.assertIn("worktree", rc)
        # Notify fired with failure message
        notify.assert_called_once()
        notify_title, notify_body = notify.call_args[0][:2]
        self.assertIn("reassign failed", notify_title)
        # Dev server, agent, and browser must NOT have been opened
        dev.assert_not_called()
        agent.assert_not_called()
        browser.assert_not_called()
        ref_pane.assert_not_called()  # chain stopped before ref pane


class CmuxPollTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._c, self._p = sh.CACHE_PATH, sh.PR_CACHE_PATH
        sh.CACHE_PATH = pathlib.Path(self.tmp.name) / "spinup-surfaced.json"
        sh.PR_CACHE_PATH = pathlib.Path(self.tmp.name) / "pr-surfaced.json"
        # Redirect PR notify state to tmp dir too
        self._pns = cc.PR_NOTIFY_STATE_PATH
        cc.PR_NOTIFY_STATE_PATH = pathlib.Path(self.tmp.name) / "cmux-pr-notify.json"

    def tearDown(self):
        sh.CACHE_PATH, sh.PR_CACHE_PATH = self._c, self._p
        cc.PR_NOTIFY_STATE_PATH = self._pns
        self.tmp.cleanup()

    @patch("cmux_chain.within_work_hours", return_value=True)
    @patch("cmux_chain.cmux_notify")
    @patch("spinup_helper.list_review_requests", return_value=[])
    @patch("spinup_helper.mark_spunup")
    @patch("cmux_chain.run_chain", return_value={"status": "ok", "workspace": "workspace:3", "worktree": "/wt"})
    @patch("spinup_helper.transition_to_in_progress", return_value=True)
    @patch("spinup_helper.list_assigned_eligible",
           return_value=[{"key": "PROJ-9", "title": "T", "issue_type": "Bug", "status": "Triage"}])
    def test_poll_autospins_new_jira_via_cmux(self, elig, trans, chain, mark, prs, notify, wh):
        self.assertEqual(cc.cmd_cmux_poll(type("A", (), {})()), 0)
        called = chain.call_args
        self.assertEqual(called.kwargs.get("name") or called[0][0], "proj-9")
        self.assertTrue((called.kwargs.get("branch") or called[0][1]).startswith("bugfix/proj-9"))
        self.assertTrue(called.kwargs.get("new_branch"))
        mark.assert_called_once_with("PROJ-9")
        notify.assert_called()  # ready notification

    @patch("cmux_chain.within_work_hours", return_value=True)
    @patch("cmux_chain.cmux_notify")
    @patch("spinup_helper.list_review_requests", return_value=[])
    @patch("spinup_helper.mark_spunup")
    @patch("cmux_chain.run_chain", return_value={"status": "port-reassign-failed", "workspace": "workspace:3", "worktree": "/wt"})
    @patch("spinup_helper.transition_to_in_progress", return_value=True)
    @patch("spinup_helper.list_assigned_eligible",
           return_value=[{"key": "PROJ-9", "title": "T", "issue_type": "Bug", "status": "Triage"}])
    def test_poll_does_not_mark_spunup_on_run_chain_failure(self, elig, trans, chain, mark, prs, notify, wh):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cc.cmd_cmux_poll(type("A", (), {})()), 0)
        result = json.loads(buf.getvalue())
        mark.assert_not_called()
        self.assertEqual(result["jira_spun_up"], [])

    @patch("cmux_chain.find_notification_id_by_title", return_value="uuid-abc")
    @patch("cmux_chain.main_workspace_ref", return_value=None)
    @patch("cmux_chain.cmux_notify")
    @patch("cmux_chain.run_chain")
    @patch("spinup_helper.list_assigned_eligible", return_value=[])
    @patch("spinup_helper.list_review_requests",
           return_value=[{"number": 45647, "title": "x", "author": "alice", "url": "u", "created_at": ""}])
    def test_poll_surfaces_prs_once_via_cmux_notify(self, prs, elig, chain, notify, ws_ref, find_id):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cc.cmd_cmux_poll(type("A", (), {})())      # surfaces #45647
        self.assertIn(45647, sh.surfaced_pr_numbers())
        notify.assert_called()
        notify.reset_mock()
        find_id.reset_mock()
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            cc.cmd_cmux_poll(type("A", (), {})())      # pending set unchanged -> no re-post
        # second cycle: pending set [45647] == state [45647] -> no new notify
        notify.assert_not_called()


class CmuxListNotificationsTests(unittest.TestCase):
    def test_parses_realistic_pipe_format(self):
        """cmux_list_notifications correctly extracts UUID from the pipe format."""
        sample = (
            "0:notif-uuid-001|workspace:1|surface:2|unread|cmux: 2 PR reviews pending"
            "||#45647 (alice) -- /spinup #45647|2026-06-05T10:00:00Z|workspace\n"
            "1:notif-uuid-002|workspace:1|surface:2|read|cmux: 1 PR review pending"
            "||#45000 (alice) -- /spinup #45000|2026-06-04T09:00:00Z|workspace\n"
        )
        with patch("cmux_chain._cmux", return_value=sample):
            result = cc.cmux_list_notifications()
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["id"], "notif-uuid-001")
        self.assertFalse(result[0]["read"])
        self.assertEqual(result[0]["title"], "cmux: 2 PR reviews pending")
        self.assertEqual(result[1]["id"], "notif-uuid-002")
        self.assertTrue(result[1]["read"])
        self.assertEqual(result[1]["title"], "cmux: 1 PR review pending")

    def test_returns_empty_on_cmux_error(self):
        with patch("cmux_chain._cmux", side_effect=cc.CmuxError("fail")):
            result = cc.cmux_list_notifications()
        self.assertEqual(result, [])

    def test_skips_malformed_lines(self):
        sample = "not-a-valid-line\n0:uuid|ws|surf|unread|Title||body|ts|scope\n"
        with patch("cmux_chain._cmux", return_value=sample):
            result = cc.cmux_list_notifications()
        # The malformed line is skipped; the valid one is parsed
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "uuid")


class PrNotifyStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._pns = cc.PR_NOTIFY_STATE_PATH
        cc.PR_NOTIFY_STATE_PATH = pathlib.Path(self.tmp.name) / "cmux-pr-notify.json"

    def tearDown(self):
        cc.PR_NOTIFY_STATE_PATH = self._pns
        self.tmp.cleanup()

    def test_load_default_when_absent(self):
        state = cc.load_pr_notify_state()
        self.assertEqual(state, {"numbers": [], "notif_id": None})

    def test_roundtrip_save_and_load(self):
        cc.save_pr_notify_state({"numbers": [100, 200], "notif_id": "uuid-xyz"})
        state = cc.load_pr_notify_state()
        self.assertEqual(state["numbers"], [100, 200])
        self.assertEqual(state["notif_id"], "uuid-xyz")

    def test_load_corrupted_returns_default(self):
        cc.PR_NOTIFY_STATE_PATH.write_text("not json")
        state = cc.load_pr_notify_state()
        self.assertEqual(state, {"numbers": [], "notif_id": None})


class PrNotifyBehaviorTests(unittest.TestCase):
    """Tests for the always-current PR notification logic in cmd_cmux_poll."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._c, self._p = sh.CACHE_PATH, sh.PR_CACHE_PATH
        sh.CACHE_PATH = pathlib.Path(self.tmp.name) / "spinup-surfaced.json"
        sh.PR_CACHE_PATH = pathlib.Path(self.tmp.name) / "pr-surfaced.json"
        self._pns = cc.PR_NOTIFY_STATE_PATH
        cc.PR_NOTIFY_STATE_PATH = pathlib.Path(self.tmp.name) / "cmux-pr-notify.json"

    def tearDown(self):
        sh.CACHE_PATH, sh.PR_CACHE_PATH = self._c, self._p
        cc.PR_NOTIFY_STATE_PATH = self._pns
        self.tmp.cleanup()

    def _run_poll(self, prs, jira=None):
        """Helper: run cmd_cmux_poll with given PRs and optional jira results, capture stdout."""
        jira = jira or []
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cc.cmd_cmux_poll(type("A", (), {})())
        return json.loads(buf.getvalue())

    @patch("cmux_chain.find_notification_id_by_title", return_value="uuid-new-001")
    @patch("cmux_chain.main_workspace_ref", return_value=None)
    @patch("cmux_chain.cmux_notify")
    @patch("cmux_chain.run_chain")
    @patch("spinup_helper.list_assigned_eligible", return_value=[])
    @patch("spinup_helper.list_review_requests",
           return_value=[
               {"number": 100, "title": "PR 100", "author": "alice", "url": "u1", "created_at": ""},
               {"number": 200, "title": "PR 200", "author": "bob", "url": "u2", "created_at": ""},
           ])
    def test_new_prs_post_one_notification_with_correct_content(
            self, prs, elig, chain, notify, ws_ref, find_id):
        """New PRs (state empty) -> posts one notification; title+body correct; state saved."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cc.cmd_cmux_poll(type("A", (), {})())

        # Exactly one cmux_notify call for PRs (no jira spun up)
        notify.assert_called_once()
        call_args = notify.call_args
        title = call_args[0][0]
        body = call_args[0][1]
        self.assertEqual(title, "cmux: 2 PR reviews pending")
        self.assertIn("#100 alice", body)
        self.assertIn("#200 bob", body)
        self.assertIn("-- /spinup #N", body)
        self.assertNotIn("\n", body)  # cmux truncates multi-line bodies

        # State saved with numbers and notif_id
        state = cc.load_pr_notify_state()
        self.assertEqual(state["numbers"], [100, 200])
        self.assertEqual(state["notif_id"], "uuid-new-001")

    @patch("cmux_chain.find_notification_id_by_title", return_value="uuid-001")
    @patch("cmux_chain.main_workspace_ref", return_value=None)
    @patch("cmux_chain.cmux_notify")
    @patch("cmux_chain.run_chain")
    @patch("spinup_helper.list_assigned_eligible", return_value=[])
    @patch("spinup_helper.list_review_requests",
           return_value=[
               {"number": 100, "title": "PR 100", "author": "alice", "url": "u1", "created_at": ""},
           ])
    def test_singular_title_when_one_pending(self, prs, elig, chain, notify, ws_ref, find_id):
        """1 pending PR -> title uses singular 'review pending'."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cc.cmd_cmux_poll(type("A", (), {})())
        title = notify.call_args[0][0]
        self.assertEqual(title, "cmux: 1 PR review pending")

    @patch("cmux_chain.find_notification_id_by_title", return_value="uuid-001")
    @patch("cmux_chain.main_workspace_ref", return_value=None)
    @patch("cmux_chain.cmux_notify")
    @patch("cmux_chain.cmux_dismiss_notification")
    @patch("cmux_chain.run_chain")
    @patch("spinup_helper.list_assigned_eligible", return_value=[])
    @patch("spinup_helper.list_review_requests",
           return_value=[
               {"number": 100, "title": "PR 100", "author": "alice", "url": "u1", "created_at": ""},
           ])
    def test_unchanged_pending_set_posts_nothing(
            self, prs, elig, chain, dismiss, notify, ws_ref, find_id):
        """Two consecutive polls with the same pending set -> second poll posts/dismisses NOTHING."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cc.cmd_cmux_poll(type("A", (), {})())   # first poll: sets state to [100]
        notify.reset_mock()
        dismiss.reset_mock()

        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            cc.cmd_cmux_poll(type("A", (), {})())   # second poll: [100] == [100] -> no-op

        notify.assert_not_called()
        dismiss.assert_not_called()

    @patch("cmux_chain.find_notification_id_by_title", return_value="uuid-smaller")
    @patch("cmux_chain.main_workspace_ref", return_value=None)
    @patch("cmux_chain.cmux_notify")
    @patch("cmux_chain.cmux_dismiss_notification")
    @patch("cmux_chain.run_chain")
    @patch("spinup_helper.list_assigned_eligible", return_value=[])
    def test_acted_pr_drops_out_of_pending_dismiss_and_repost(
            self, elig, chain, dismiss, notify, ws_ref, find_id):
        """PR 100 becomes 'review' -> drops from pending -> old notif dismissed + new smaller set posted."""
        # Seed: both 100 and 200 are open/pending
        sh.mark_pr_surfaced(100, {"author": "alice", "title": "", "url": ""})
        sh.mark_pr_surfaced(200, {"author": "bob", "title": "", "url": ""})
        # Save notify state indicating old notif for [100, 200]
        cc.save_pr_notify_state({"numbers": [100, 200], "notif_id": "uuid-old"})

        # Now PR 100 has been acted on (decision=review)
        sh.record_pr_decision(100, "review")

        # Only PR 200 is still open
        with patch("spinup_helper.list_review_requests",
                   return_value=[
                       {"number": 200, "title": "PR 200", "author": "bob", "url": "u2", "created_at": ""},
                   ]):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cc.cmd_cmux_poll(type("A", (), {})())

        # Old notification dismissed
        dismiss.assert_called_once_with("uuid-old")
        # New notification posted with only PR 200
        notify.assert_called_once()
        title = notify.call_args[0][0]
        body = notify.call_args[0][1]
        self.assertEqual(title, "cmux: 1 PR review pending")
        self.assertIn("#200", body)
        self.assertNotIn("#100", body)
        # State updated
        state = cc.load_pr_notify_state()
        self.assertEqual(state["numbers"], [200])

    @patch("cmux_chain.main_workspace_ref", return_value=None)
    @patch("cmux_chain.cmux_notify")
    @patch("cmux_chain.cmux_dismiss_notification")
    @patch("cmux_chain.run_chain")
    @patch("spinup_helper.list_assigned_eligible", return_value=[])
    def test_all_prs_acted_dismisses_and_posts_nothing(
            self, elig, chain, dismiss, notify, ws_ref):
        """All PRs acted -> dismiss old, post nothing, state numbers == []."""
        # Seed both PRs as surfaced+acted
        sh.mark_pr_surfaced(100, {"author": "alice", "title": "", "url": ""})
        sh.mark_pr_surfaced(200, {"author": "bob", "title": "", "url": ""})
        sh.record_pr_decision(100, "review")
        sh.record_pr_decision(200, "skip")
        cc.save_pr_notify_state({"numbers": [100, 200], "notif_id": "uuid-old"})

        with patch("spinup_helper.list_review_requests", return_value=[]):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cc.cmd_cmux_poll(type("A", (), {})())

        dismiss.assert_called_once_with("uuid-old")
        notify.assert_not_called()
        state = cc.load_pr_notify_state()
        self.assertEqual(state["numbers"], [])
        self.assertIsNone(state["notif_id"])

    @patch("cmux_chain.find_notification_id_by_title", return_value="uuid-001")
    @patch("cmux_chain.main_workspace_ref", return_value=None)
    @patch("cmux_chain.cmux_notify")
    @patch("cmux_chain.run_chain")
    @patch("spinup_helper.list_assigned_eligible", return_value=[])
    def test_pending_excludes_review_and_skip_decisions(
            self, elig, chain, notify, ws_ref, find_id):
        """PRs with decision 'review' or 'skip' are excluded from pending."""
        sh.mark_pr_surfaced(10, {"author": "a", "title": "", "url": ""})
        sh.mark_pr_surfaced(20, {"author": "b", "title": "", "url": ""})
        sh.mark_pr_surfaced(30, {"author": "c", "title": "", "url": ""})
        sh.record_pr_decision(10, "review")
        sh.record_pr_decision(20, "skip")
        # PR 30 remains pending

        with patch("spinup_helper.list_review_requests",
                   return_value=[
                       {"number": 10, "title": "", "author": "a", "url": "", "created_at": ""},
                       {"number": 20, "title": "", "author": "b", "url": "", "created_at": ""},
                       {"number": 30, "title": "", "author": "c", "url": "", "created_at": ""},
                   ]):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cc.cmd_cmux_poll(type("A", (), {})())

        notify.assert_called_once()
        title = notify.call_args[0][0]
        body = notify.call_args[0][1]
        self.assertEqual(title, "cmux: 1 PR review pending")
        self.assertIn("#30", body)
        self.assertNotIn("#10", body)
        self.assertNotIn("#20", body)
        state = cc.load_pr_notify_state()
        self.assertEqual(state["numbers"], [30])

    @patch("cmux_chain.within_work_hours", return_value=True)
    @patch("cmux_chain.find_notification_id_by_title", return_value="uuid-jira-test")
    @patch("cmux_chain.main_workspace_ref", return_value=None)
    @patch("cmux_chain.cmux_notify")
    @patch("spinup_helper.list_review_requests", return_value=[])
    @patch("spinup_helper.mark_spunup")
    @patch("cmux_chain.run_chain", return_value={"status": "ok", "workspace": "workspace:3", "worktree": "/wt"})
    @patch("spinup_helper.transition_to_in_progress", return_value=True)
    @patch("spinup_helper.list_assigned_eligible",
           return_value=[{"key": "PROJ-99", "title": "T", "issue_type": "Story", "status": "Triage"}])
    def test_jira_spunup_notification_fires_without_pr_lines(
            self, elig, trans, chain, mark, prs, notify, ws_ref, find_id, wh):
        """Jira spin-up notification fires (Jira-only) and contains no PR lines."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cc.cmd_cmux_poll(type("A", (), {})())

        # At least one notify call should be the Jira spin-up one
        jira_calls = [c for c in notify.call_args_list if "spun up" in c[0][0]]
        self.assertTrue(len(jira_calls) >= 1, "Expected a Jira spun-up notification")
        jira_title, jira_body = jira_calls[0][0][0], jira_calls[0][0][1]
        self.assertIn("spun up", jira_title)
        # Body must not contain PR lines
        self.assertNotIn("/spinup #", jira_body)

    @patch("cmux_chain.within_work_hours", return_value=True)
    @patch("cmux_chain.cmux_notify")
    @patch("spinup_helper.list_review_requests", return_value=[])
    @patch("spinup_helper.mark_spunup")
    @patch("cmux_chain.run_chain", return_value={"status": "ok", "workspace": "workspace:3", "worktree": "/wt"})
    @patch("spinup_helper.transition_to_in_progress", return_value=True)
    @patch("spinup_helper.list_assigned_eligible", return_value=[
        {"key": "PROJ-10", "title": "T1", "issue_type": "Bug", "status": "Triage"},
        {"key": "PROJ-11", "title": "T2", "issue_type": "Story", "status": "Triage"},
    ])
    def test_poll_spins_up_at_most_one_ticket_per_cycle(self, elig, trans, chain, mark, prs, notify, wh):
        """Fix B: with 2+ eligible tickets, a single poll cycle calls run_chain exactly once."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cc.cmd_cmux_poll(type("A", (), {})())
        self.assertEqual(chain.call_count, 1)


class SeedBacklogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._c, self._p = sh.CACHE_PATH, sh.PR_CACHE_PATH
        sh.CACHE_PATH = pathlib.Path(self.tmp.name) / "s.json"
        sh.PR_CACHE_PATH = pathlib.Path(self.tmp.name) / "p.json"

    def tearDown(self):
        sh.CACHE_PATH, sh.PR_CACHE_PATH = self._c, self._p
        self.tmp.cleanup()

    @patch("spinup_helper.list_review_requests",
           return_value=[{"number": 7, "title": "t", "author": "a", "url": "u", "created_at": ""}])
    @patch("spinup_helper.list_assigned_eligible",
           return_value=[{"key": "PROJ-1", "title": "t", "issue_type": "Bug", "status": "Triage"}])
    def test_seed_marks_everything_handled(self, elig, prs):
        self.assertEqual(cc.cmd_seed_backlog(type("A", (), {})()), 0)
        self.assertIn("PROJ-1", {k for k, v in sh.load_state().items() if v.get("spunup_at")})
        self.assertIn(7, sh.surfaced_pr_numbers())


class OrphanCleanupTests(unittest.TestCase):
    @patch("cmux_chain.subprocess.run")
    def test_proc_cwd_parses_lsof(self, run):
        run.return_value = _run(stdout="COMMAND PID USER FD TYPE DEVICE SIZE NODE NAME\np 1 u cwd DIR 1,2 64 9 /wt\n")
        self.assertEqual(cc._proc_cwd("1"), "/wt")

    @patch("cmux_chain._proc_cwd")
    @patch("cmux_chain.subprocess.run")
    def test_kill_worktree_port_orphans_kills_matching_cwd(self, run, cwd):
        # lsof -t returns two pids; both cwd == worktree -> both killed
        run.side_effect = [_run(stdout="111\n222\n"), _run(), _run()]
        cwd.return_value = "/wt"
        n = cc.kill_worktree_port_orphans("/wt", 3062)
        self.assertEqual(n, 2)
        self.assertEqual(run.call_count, 3)  # 1 lsof + 2 kill calls

    @patch("cmux_chain._proc_cwd", return_value="/other")
    @patch("cmux_chain.subprocess.run")
    def test_kill_worktree_port_orphans_skips_other_cwd(self, run, cwd):
        run.side_effect = [_run(stdout="111\n"), _run()]
        self.assertEqual(cc.kill_worktree_port_orphans("/wt", 3062), 0)


class CmdTests(unittest.TestCase):
    @patch("cmux_chain.branch_exists", return_value=False)
    @patch("cmux_chain.run_chain", return_value={"status": "ok", "workspace": "workspace:3", "worktree": "/wt/proj-5487"})
    @patch("spinup_helper.transition_to_in_progress", return_value=True)
    @patch("spinup_helper.fetch_ticket", return_value={
        "key": "PROJ-5487", "title": "Breadcrumb nav", "issue_type": "Bug", "status": "Triage"})
    def test_cmd_spinup_uses_shared_helpers_and_jira_prompt(self, fetch, trans, chain, be):
        rc = cc.cmd_spinup(type("A", (), {"ticket": "PROJ-5487"})())
        self.assertEqual(rc, 0)
        # branch derived via shared helper (bugfix/ prefix for a Bug)
        called = chain.call_args
        self.assertEqual(called.kwargs.get("name") or called[0][0], "proj-5487")
        self.assertTrue((called.kwargs.get("branch") or called[0][1]).startswith("bugfix/proj-5487"))
        self.assertIn("writing-plans for PROJ-5487", (called.kwargs.get("prompt") or called[0][3]))
        # ref_url must point to the Jira ticket
        ref_url = called.kwargs.get("ref_url")
        self.assertIsNotNone(ref_url)
        self.assertTrue(ref_url.endswith("/PROJ-5487"), f"ref_url should end with /PROJ-5487, got {ref_url!r}")
        self.assertIn("your-jira-instance", ref_url)

    @patch("cmux_chain.run_chain", return_value={"status": "ok", "workspace": "workspace:4", "worktree": "/wt/pr-9"})
    @patch("cmux_chain.git_fetch")
    @patch("spinup_helper.resolve_pr_head_branch", return_value="internal/foo")
    def test_cmd_spinup_pr_resolves_head_and_lens_review(self, resolve, fetch, chain):
        rc = cc.cmd_spinup_pr(type("A", (), {"number": 9})())
        self.assertEqual(rc, 0)
        fetch.assert_called_once_with("internal/foo")
        called = chain.call_args
        self.assertEqual(called.kwargs.get("name") or called[0][0], "pr-9")
        # existing remote branch -- run_chain is called with kwargs, so check kwargs directly
        nb = called.kwargs["new_branch"] if "new_branch" in called.kwargs else called[0][2]
        self.assertFalse(nb)
        self.assertIn("lens-review for PR #9", (called.kwargs.get("prompt") or called[0][3]))
        # ref_url must point to the GitHub PR
        ref_url = called.kwargs.get("ref_url")
        self.assertIsNotNone(ref_url)
        self.assertEqual(ref_url, f"https://github.com/{cc.GH_REPO}/pull/9")

    @patch("spinup_helper.mark_spunup")
    @patch("cmux_chain.branch_exists", return_value=False)
    @patch("cmux_chain.run_chain", return_value={"status": "ok", "workspace": "workspace:3", "worktree": "/wt/proj-5487"})
    @patch("spinup_helper.transition_to_in_progress", return_value=True)
    @patch("spinup_helper.fetch_ticket", return_value={
        "key": "PROJ-5487", "title": "Breadcrumb nav", "issue_type": "Bug", "status": "Triage"})
    def test_cmd_spinup_marks_spunup_on_success(self, fetch, trans, chain, be, mark):
        """Fix C: cmd_spinup calls mark_spunup when run_chain status is 'ok'."""
        rc = cc.cmd_spinup(type("A", (), {"ticket": "PROJ-5487"})())
        self.assertEqual(rc, 0)
        mark.assert_called_once_with("PROJ-5487")

    @patch("spinup_helper.mark_spunup")
    @patch("cmux_chain.branch_exists", return_value=False)
    @patch("cmux_chain.run_chain", return_value={"status": "serving-timeout", "workspace": "workspace:3", "worktree": "/wt/proj-5487"})
    @patch("spinup_helper.transition_to_in_progress", return_value=True)
    @patch("spinup_helper.fetch_ticket", return_value={
        "key": "PROJ-5487", "title": "Breadcrumb nav", "issue_type": "Bug", "status": "Triage"})
    def test_cmd_spinup_marks_spunup_on_serving_timeout(self, fetch, trans, chain, be, mark):
        """Fix C: cmd_spinup calls mark_spunup when run_chain status is 'serving-timeout'."""
        rc = cc.cmd_spinup(type("A", (), {"ticket": "PROJ-5487"})())
        self.assertEqual(rc, 0)
        mark.assert_called_once_with("PROJ-5487")

    @patch("spinup_helper.mark_spunup")
    @patch("cmux_chain.branch_exists", return_value=False)
    @patch("cmux_chain.run_chain", return_value={"status": "setup-failed", "workspace": "workspace:3", "worktree": "/wt/proj-5487"})
    @patch("spinup_helper.transition_to_in_progress", return_value=True)
    @patch("spinup_helper.fetch_ticket", return_value={
        "key": "PROJ-5487", "title": "Breadcrumb nav", "issue_type": "Bug", "status": "Triage"})
    def test_cmd_spinup_does_not_mark_spunup_on_failure(self, fetch, trans, chain, be, mark):
        """Fix C: cmd_spinup must NOT call mark_spunup when run_chain status indicates failure."""
        rc = cc.cmd_spinup(type("A", (), {"ticket": "PROJ-5487"})())
        self.assertEqual(rc, 0)
        mark.assert_not_called()


class GroupListTests(unittest.TestCase):
    def test_parses_group_list_response(self):
        payload = json.dumps({"groups": [
            {"id": "g-001", "name": "In Progress", "custom_color": "#F59E0B",
             "icon_symbol": "hammer.fill", "is_pinned": True,
             "member_workspace_ids": ["ws-uuid-1"], "member_workspace_refs": ["workspace:2"]},
            {"id": "g-002", "name": "Pull Request Reviews", "custom_color": "#3B82F6",
             "icon_symbol": "arrow.triangle.pull", "is_pinned": True,
             "member_workspace_ids": [], "member_workspace_refs": []},
        ]})
        with patch("cmux_chain._cmux", return_value=payload):
            result = cc.cmux_group_list()
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["id"], "g-001")
        self.assertEqual(result[0]["name"], "In Progress")
        self.assertEqual(result[1]["id"], "g-002")
        self.assertEqual(result[1]["name"], "Pull Request Reviews")

    def test_returns_empty_on_cmux_error(self):
        with patch("cmux_chain._cmux", side_effect=cc.CmuxError("fail")):
            result = cc.cmux_group_list()
        self.assertEqual(result, [])

    def test_returns_empty_on_invalid_json(self):
        with patch("cmux_chain._cmux", return_value="not-json"):
            result = cc.cmux_group_list()
        self.assertEqual(result, [])


class GroupEnsureTests(unittest.TestCase):
    def test_existing_group_returns_id_without_create(self):
        groups = [{"id": "g-existing", "name": "In Progress"}]
        with patch("cmux_chain.cmux_group_list", return_value=groups) as gl, \
             patch("cmux_chain._cmux") as cmux:
            result = cc.cmux_group_ensure("In Progress", "#F59E0B", "hammer.fill")
        self.assertEqual(result, "g-existing")
        cmux.assert_not_called()

    def test_missing_group_creates_then_themes_and_pins(self):
        # First call returns no groups (missing); second call (re-list) returns new group
        create_resp = json.dumps({"group": {"id": "g-new", "name": "In Progress"}})
        color_resp = json.dumps({})
        icon_resp = json.dumps({})
        pin_resp = json.dumps({})
        with patch("cmux_chain.cmux_group_list", return_value=[]) as gl, \
             patch("cmux_chain._cmux", side_effect=[create_resp, color_resp, icon_resp, pin_resp]) as cmux:
            result = cc.cmux_group_ensure("In Progress", "#F59E0B", "hammer.fill")
        self.assertEqual(result, "g-new")
        # calls is a list of argv lists, e.g. ['rpc', 'workspace.group.create', '{"name": ...}']
        # Use a helper: check if any element in the argv list contains the method name as substring.
        def has_method(argv, method):
            return any(method in elem for elem in argv)

        calls = [c[0][0] for c in cmux.call_args_list]
        # create called with workspace.group.create
        self.assertTrue(any(has_method(argv, "workspace.group.create") for argv in calls))
        # set_color called with group_id and hex -- check the JSON payload (last element)
        color_argv = next(argv for argv in calls if has_method(argv, "workspace.group.set_color"))
        color_payload = color_argv[-1]
        self.assertIn("g-new", color_payload)
        self.assertIn("#F59E0B", color_payload)
        # set_icon called with group_id and symbol
        icon_argv = next(argv for argv in calls if has_method(argv, "workspace.group.set_icon"))
        icon_payload = icon_argv[-1]
        self.assertIn("g-new", icon_payload)
        self.assertIn("hammer.fill", icon_payload)
        # pin called with group_id
        pin_argv = next(argv for argv in calls if has_method(argv, "workspace.group.pin"))
        pin_payload = pin_argv[-1]
        self.assertIn("g-new", pin_payload)

    def test_ensure_returns_id_when_create_response_has_group_key(self):
        create_resp = json.dumps({"group": {"id": "g-777", "name": "X"}})
        with patch("cmux_chain.cmux_group_list", return_value=[]), \
             patch("cmux_chain._cmux", side_effect=[create_resp, "{}", "{}", "{}"]):
            gid = cc.cmux_group_ensure("X", "#000000", "star")
        self.assertEqual(gid, "g-777")


class GroupAddTests(unittest.TestCase):
    # workspace.group.add accepts a ref OR a UUID for workspace_id and resolves refs
    # itself, so cmux_group_add passes whatever it is given straight through (single call).

    def test_passes_ref_through_directly(self):
        with patch("cmux_chain._cmux", return_value=json.dumps({})) as cmux:
            cc.cmux_group_add("g-001", "workspace:2")
        self.assertEqual(cmux.call_count, 1)
        argv = cmux.call_args[0][0]
        self.assertIn("workspace.group.add", argv)
        add_json = json.loads(argv[-1])
        self.assertEqual(add_json["group_id"], "g-001")
        self.assertEqual(add_json["workspace_id"], "workspace:2")

    def test_passes_uuid_through_directly(self):
        with patch("cmux_chain._cmux", return_value=json.dumps({})) as cmux:
            cc.cmux_group_add("g-001", "plain-uuid-here")
        self.assertEqual(cmux.call_count, 1)
        add_json = json.loads(cmux.call_args[0][0][-1])
        self.assertEqual(add_json["workspace_id"], "plain-uuid-here")


class AssignToGroupTests(unittest.TestCase):
    def test_calls_ensure_and_add(self):
        group_spec = {"name": "In Progress", "hex": "#F59E0B", "symbol": "hammer.fill"}
        with patch("cmux_chain.cmux_group_ensure", return_value="g-001") as ensure, \
             patch("cmux_chain.cmux_group_add") as add:
            cc.assign_to_group("workspace:2", group_spec)
        ensure.assert_called_once_with("In Progress", "#F59E0B", "hammer.fill")
        add.assert_called_once_with("g-001", "workspace:2")

    def test_grouping_failure_warns_to_stderr_but_does_not_raise(self):
        group_spec = {"name": "In Progress", "hex": "#F59E0B", "symbol": "hammer.fill"}
        with patch("cmux_chain.cmux_group_ensure", side_effect=Exception("rpc fail")):
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                # Must not raise
                cc.assign_to_group("workspace:2", group_spec)
        self.assertIn("rpc fail", buf.getvalue())

    def test_add_failure_warns_to_stderr_but_does_not_raise(self):
        group_spec = {"name": "In Progress", "hex": "#F59E0B", "symbol": "hammer.fill"}
        with patch("cmux_chain.cmux_group_ensure", return_value="g-001"), \
             patch("cmux_chain.cmux_group_add", side_effect=cc.CmuxError("add fail")):
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                cc.assign_to_group("workspace:2", group_spec)
        self.assertIn("add fail", buf.getvalue())


class RunChainGroupTests(unittest.TestCase):
    """run_chain with group= arg -> calls assign_to_group after workspace creation."""

    def _make_patches(self, serving=True, setup=True):
        return [
            patch("cmux_chain.open_ref_browser_pane"),
            patch("cmux_chain.cmux_notify"),
            patch("cmux_chain.open_browser_tab"),
            patch("cmux_chain.open_agent_tab"),
            patch("cmux_chain.wait_for_dev_server", return_value=serving),
            patch("cmux_chain.open_dev_server_tab"),
            patch("cmux_chain.port_in_use", return_value=False),
            patch("cmux_chain.read_worktree_port", return_value=3060),
            patch("cmux_chain.wait_for_setup", return_value=setup),
            patch("cmux_chain.open_setup_tab", return_value="workspace:3"),
            patch("cmux_chain.ensure_worktree", return_value="/wt/proj-1"),
            patch("cmux_chain.assign_to_group"),
        ]

    def test_run_chain_with_group_calls_assign(self):
        patches = self._make_patches()
        managers = [p.start() for p in patches]
        assign = managers[-1]
        try:
            rc = cc.run_chain("proj-1", "feature/proj-1-x", True, "P",
                              group=cc.GROUP_IN_PROGRESS)
            self.assertEqual(rc["status"], "ok")
            assign.assert_called_once_with("workspace:3", cc.GROUP_IN_PROGRESS)
        finally:
            for p in patches:
                p.stop()

    def test_run_chain_without_group_skips_assign(self):
        patches = self._make_patches()
        managers = [p.start() for p in patches]
        assign = managers[-1]
        try:
            rc = cc.run_chain("proj-1", "feature/proj-1-x", True, "P")
            self.assertEqual(rc["status"], "ok")
            assign.assert_not_called()
        finally:
            for p in patches:
                p.stop()

    def test_run_chain_setup_failure_skips_assign(self):
        patches = self._make_patches(setup=False)
        managers = [p.start() for p in patches]
        assign = managers[-1]
        try:
            rc = cc.run_chain("proj-1", "feature/proj-1-x", True, "P",
                              group=cc.GROUP_IN_PROGRESS)
            self.assertEqual(rc["status"], "setup-failed")
            assign.assert_not_called()
        finally:
            for p in patches:
                p.stop()


class IntegrationGroupRoutingTests(unittest.TestCase):
    """Confirm ticket spin-ups use GROUP_IN_PROGRESS and PR spin-up uses GROUP_PR_REVIEWS."""

    @patch("cmux_chain.run_chain", return_value={"status": "ok", "workspace": "workspace:3", "worktree": "/wt"})
    @patch("spinup_helper.transition_to_in_progress", return_value=True)
    @patch("spinup_helper.fetch_ticket", return_value={
        "key": "PROJ-5487", "title": "Breadcrumb nav", "issue_type": "Bug", "status": "Triage"})
    def test_cmd_spinup_passes_in_progress_group(self, fetch, trans, chain):
        cc.cmd_spinup(type("A", (), {"ticket": "PROJ-5487"})())
        called = chain.call_args
        group = called.kwargs.get("group")
        self.assertIsNotNone(group, "run_chain must be called with group=")
        self.assertEqual(group["name"], "In Progress")

    @patch("cmux_chain.run_chain", return_value={"status": "ok", "workspace": "workspace:4", "worktree": "/wt"})
    @patch("cmux_chain.git_fetch")
    @patch("spinup_helper.resolve_pr_head_branch", return_value="internal/foo")
    def test_cmd_spinup_pr_passes_pr_reviews_group(self, resolve, fetch, chain):
        cc.cmd_spinup_pr(type("A", (), {"number": 9})())
        called = chain.call_args
        group = called.kwargs.get("group")
        self.assertIsNotNone(group, "run_chain must be called with group=")
        self.assertEqual(group["name"], "Pull Request Reviews")

    @patch("cmux_chain.within_work_hours", return_value=True)
    @patch("cmux_chain.cmux_notify")
    @patch("spinup_helper.list_review_requests", return_value=[])
    @patch("spinup_helper.mark_spunup")
    @patch("cmux_chain.run_chain", return_value={"status": "ok", "workspace": "workspace:3", "worktree": "/wt"})
    @patch("spinup_helper.transition_to_in_progress", return_value=True)
    @patch("spinup_helper.list_assigned_eligible",
           return_value=[{"key": "PROJ-42", "title": "T", "issue_type": "Story", "status": "Triage"}])
    def test_cmd_cmux_poll_passes_in_progress_group(self, elig, trans, chain, mark, prs, notify, wh):
        import tempfile, pathlib
        tmp = tempfile.mkdtemp()
        _c, _p = sh.CACHE_PATH, sh.PR_CACHE_PATH
        sh.CACHE_PATH = pathlib.Path(tmp) / "s.json"
        sh.PR_CACHE_PATH = pathlib.Path(tmp) / "p.json"
        _pns = cc.PR_NOTIFY_STATE_PATH
        cc.PR_NOTIFY_STATE_PATH = pathlib.Path(tmp) / "pr-notify.json"
        try:
            cc.cmd_cmux_poll(type("A", (), {})())
            called = chain.call_args
            group = called.kwargs.get("group")
            self.assertIsNotNone(group, "auto-poll run_chain must be called with group=")
            self.assertEqual(group["name"], "In Progress")
        finally:
            sh.CACHE_PATH, sh.PR_CACHE_PATH = _c, _p
            cc.PR_NOTIFY_STATE_PATH = _pns


class GroupingFailureBestEffortTests(unittest.TestCase):
    """A grouping failure must not abort the spin-up."""

    @patch("cmux_chain.run_chain")
    @patch("spinup_helper.transition_to_in_progress", return_value=True)
    @patch("spinup_helper.fetch_ticket", return_value={
        "key": "PROJ-5487", "title": "X", "issue_type": "Bug", "status": "Triage"})
    def test_cmd_spinup_succeeds_even_when_grouping_raises(self, fetch, trans, chain):
        # run_chain succeeds, but assign_to_group (called inside run_chain) raises
        # We test by having run_chain itself succeed but inject failure via assign_to_group
        chain.return_value = {"status": "ok", "workspace": "workspace:3", "worktree": "/wt"}
        with patch("cmux_chain.assign_to_group", side_effect=Exception("rpc boom")):
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                # cmd_spinup calls run_chain (mocked), so we need to test run_chain directly
                # with a failing assign_to_group
                pass
        # The real best-effort check: run_chain with group set, assign_to_group raises
        with patch("cmux_chain.open_ref_browser_pane"), \
             patch("cmux_chain.cmux_notify"), \
             patch("cmux_chain.open_browser_tab"), \
             patch("cmux_chain.open_agent_tab"), \
             patch("cmux_chain.wait_for_dev_server", return_value=True), \
             patch("cmux_chain.open_dev_server_tab"), \
             patch("cmux_chain.port_in_use", return_value=False), \
             patch("cmux_chain.read_worktree_port", return_value=3060), \
             patch("cmux_chain.wait_for_setup", return_value=True), \
             patch("cmux_chain.open_setup_tab", return_value="workspace:3"), \
             patch("cmux_chain.ensure_worktree", return_value="/wt/proj-1"), \
             patch("cmux_chain.assign_to_group", side_effect=Exception("rpc boom")):
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                result = cc.run_chain("proj-1", "feature/proj-1-x", True, "P",
                                      group=cc.GROUP_IN_PROGRESS)
        self.assertEqual(result["status"], "ok")


class WorktreePathGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name) / "your-app-worktrees"
        self.base.mkdir(parents=True)
        self._old_base, self._old_main = cc.WORKTREE_BASE, cc.MAIN_REPO
        cc.WORKTREE_BASE = self.base
        cc.MAIN_REPO = pathlib.Path(self.tmp.name) / "your-app"
        cc.MAIN_REPO.mkdir()

    def tearDown(self):
        cc.WORKTREE_BASE, cc.MAIN_REPO = self._old_base, self._old_main
        self.tmp.cleanup()

    def test_valid_worktree_returns_path(self):
        (self.base / "proj-1").mkdir()
        self.assertEqual(cc.worktree_path_guard("proj-1"), (self.base / "proj-1").resolve())

    def test_refuses_missing(self):
        with self.assertRaises(cc.TeardownRefused):
            cc.worktree_path_guard("does-not-exist")

    def test_refuses_path_separators_and_traversal(self):
        for bad in ("", ".", "..", "a/b", "../proj-1"):
            with self.assertRaises(cc.TeardownRefused):
                cc.worktree_path_guard(bad)

    def test_refuses_prime_checkout_even_if_symlinked_in(self):
        # a name that resolves to MAIN_REPO must be refused
        link = self.base / "prime"
        try:
            link.symlink_to(cc.MAIN_REPO)
        except OSError:
            self.skipTest("symlink not permitted")
        with self.assertRaises(cc.TeardownRefused):
            cc.worktree_path_guard("prime")


class TeardownHelperTests(unittest.TestCase):
    def test_worktree_archive_invokes_script_with_env(self):
        with patch("cmux_chain.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            cc.worktree_archive("proj-7")
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["bin/worktree-teardown"])
        self.assertEqual(kwargs["cwd"], str(cc.MAIN_REPO))
        self.assertEqual(kwargs["env"]["WORKTREE_NAME"], "proj-7")
        self.assertEqual(kwargs["env"]["WORKTREE_ROOT_PATH"], str(cc.MAIN_REPO))
        self.assertNotIn("CONDUCTOR_WORKSPACE_NAME", kwargs["env"])
        self.assertNotIn("CONDUCTOR_ROOT_PATH", kwargs["env"])

    def test_worktree_archive_raises_on_failure(self):
        with patch("cmux_chain.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 1, "", "boom")):
            with self.assertRaises(cc.CmuxError):
                cc.worktree_archive("proj-7")

    def test_git_worktree_remove_force_argv(self):
        with patch("cmux_chain.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            cc.git_worktree_remove_force(pathlib.Path("/wt/proj-7"))
        self.assertEqual(run.call_args[0][0],
                         ["git", "-C", str(cc.MAIN_REPO), "worktree", "remove", "--force", "/wt/proj-7"])

    def test_close_workspace_is_best_effort(self):
        with patch("cmux_chain._cmux", side_effect=cc.CmuxError("nope")):
            cc.cmux_close_workspace("workspace:5")  # must not raise


class WorkspaceResolveTests(unittest.TestCase):
    LIST_PLAIN = "  workspace:1 ~ Main\n* workspace:3 proj-5661 [selected]\n  workspace:9 proj-5864\n"
    LIST_BOTH = ("  workspace:1 E4294D9E-... ~ Main\n"
                 "* workspace:9 8EA2BF87-6886-41F9-9554-BC394A196EC5 proj-5864 [selected]\n")

    def test_ref_for_name(self):
        with patch("cmux_chain._cmux", return_value=self.LIST_PLAIN):
            self.assertEqual(cc.workspace_ref_for_name("proj-5864"), "workspace:9")
            self.assertEqual(cc.workspace_ref_for_name("proj-5661"), "workspace:3")
            self.assertIsNone(cc.workspace_ref_for_name("proj-0000"))

    def test_name_for_ref(self):
        with patch("cmux_chain._cmux", return_value=self.LIST_PLAIN):
            self.assertEqual(cc.workspace_name_for_ref("workspace:9"), "proj-5864")
            self.assertEqual(cc.workspace_name_for_ref("workspace:3"), "proj-5661")

    def test_uuid_for_ref_ignores_star_and_indent(self):
        with patch("cmux_chain._cmux", return_value=self.LIST_BOTH):
            self.assertEqual(cc.workspace_uuid_for_ref("workspace:9"),
                             "8EA2BF87-6886-41F9-9554-BC394A196EC5")


class WorkspaceMapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = cc.WORKSPACE_MAP_PATH
        cc.WORKSPACE_MAP_PATH = pathlib.Path(self.tmp.name) / "map.json"

    def tearDown(self):
        cc.WORKSPACE_MAP_PATH = self._old
        self.tmp.cleanup()

    def test_record_and_load(self):
        with patch("cmux_chain.workspace_uuid_for_ref", return_value="uuid-9"):
            cc.record_workspace_map("workspace:9", "proj-5864")
        self.assertEqual(cc.load_workspace_map(), {"uuid-9": "proj-5864"})

    def test_prune_removes_name(self):
        cc.save_workspace_map({"uuid-9": "proj-5864", "uuid-3": "proj-5661"})
        cc.prune_workspace_map("proj-5864")
        self.assertEqual(cc.load_workspace_map(), {"uuid-3": "proj-5661"})


class TeardownWorktreeTests(unittest.TestCase):
    def test_happy_path_runs_close_archive_remove_in_order(self):
        calls = []
        with patch("cmux_chain.worktree_path_guard", return_value=pathlib.Path("/wt/proj-7")), \
             patch("cmux_chain.worktree_archive", side_effect=lambda n: calls.append(("archive", n))), \
             patch("cmux_chain.git_worktree_remove_force", side_effect=lambda p: calls.append(("remove", str(p)))), \
             patch("cmux_chain.cmux_close_workspace", side_effect=lambda r: calls.append(("close", r))), \
             patch("cmux_chain.prune_workspace_map"), \
             patch("cmux_chain.cmux_notify") as notify:
            cc.teardown_worktree("proj-7", workspace_ref="workspace:9")
        self.assertEqual([c[0] for c in calls], ["close", "archive", "remove"])
        self.assertIn("cleaned up", notify.call_args[0][0])

    def test_close_workspace_false_skips_close(self):
        with patch("cmux_chain.worktree_path_guard", return_value=pathlib.Path("/wt/proj-7")), \
             patch("cmux_chain.worktree_archive"), \
             patch("cmux_chain.git_worktree_remove_force"), \
             patch("cmux_chain.cmux_close_workspace") as close, \
             patch("cmux_chain.prune_workspace_map"), \
             patch("cmux_chain.cmux_notify"):
            cc.teardown_worktree("proj-7", close_workspace=False)
        close.assert_not_called()

    def test_archive_failure_still_removes_and_warns(self):
        with patch("cmux_chain.worktree_path_guard", return_value=pathlib.Path("/wt/proj-7")), \
             patch("cmux_chain.worktree_archive", side_effect=cc.CmuxError("db boom")), \
             patch("cmux_chain.git_worktree_remove_force") as remove, \
             patch("cmux_chain.cmux_close_workspace"), \
             patch("cmux_chain.prune_workspace_map"), \
             patch("cmux_chain.cmux_notify") as notify:
            cc.teardown_worktree("proj-7", close_workspace=False)
        remove.assert_called_once()
        self.assertIn("issues", notify.call_args[0][0])

    def test_guard_refusal_propagates_and_does_nothing(self):
        with patch("cmux_chain.worktree_path_guard", side_effect=cc.TeardownRefused("nope")), \
             patch("cmux_chain.worktree_archive") as arch:
            with self.assertRaises(cc.TeardownRefused):
                cc.teardown_worktree("proj-app")
        arch.assert_not_called()


class SpindownCommandTests(unittest.TestCase):
    def test_named_calls_teardown(self):
        with patch("cmux_chain.teardown_worktree") as td:
            rc = cc.cmd_spindown(type("A", (), {"name": "proj-7"})())
        td.assert_called_once_with("proj-7", close_workspace=True)
        self.assertEqual(rc, 0)

    def test_no_arg_resolves_focused_worktree(self):
        with patch("cmux_chain.focused_worktree_name", return_value="proj-9"), \
             patch("cmux_chain.teardown_worktree") as td:
            cc.cmd_spindown(type("A", (), {"name": None})())
        td.assert_called_once_with("proj-9", close_workspace=True)

    def test_refusal_is_reported_not_raised(self):
        with patch("cmux_chain.teardown_worktree", side_effect=cc.TeardownRefused("prime")), \
             patch("cmux_chain.cmux_notify") as notify:
            rc = cc.cmd_spindown(type("A", (), {"name": "proj-app"})())
        self.assertEqual(rc, 1)
        self.assertIn("refused", notify.call_args[0][0].lower())

    def test_focused_non_worktree_refuses(self):
        with patch("cmux_chain.focused_worktree_name", return_value=None), \
             patch("cmux_chain.teardown_worktree") as td:
            rc = cc.cmd_spindown(type("A", (), {"name": None})())
        td.assert_not_called()
        self.assertEqual(rc, 1)


class RunChainMapTests(unittest.TestCase):
    """Task 7: run_chain records the workspace map after workspace creation."""

    def _make_patches(self, serving=True, setup=True):
        return [
            patch("cmux_chain.open_ref_browser_pane"),
            patch("cmux_chain.cmux_notify"),
            patch("cmux_chain.open_browser_tab"),
            patch("cmux_chain.open_agent_tab"),
            patch("cmux_chain.wait_for_dev_server", return_value=serving),
            patch("cmux_chain.open_dev_server_tab"),
            patch("cmux_chain.port_in_use", return_value=False),
            patch("cmux_chain.read_worktree_port", return_value=3060),
            patch("cmux_chain.wait_for_setup", return_value=setup),
            patch("cmux_chain.open_setup_tab", return_value="workspace:3"),
            patch("cmux_chain.ensure_worktree", return_value="/wt/proj-7"),
            patch("cmux_chain.record_workspace_map"),
        ]

    def test_run_chain_records_workspace_map(self):
        patches = self._make_patches()
        managers = [p.start() for p in patches]
        rec = managers[-1]
        try:
            cc.run_chain("proj-7", "feature/proj-7", new_branch=True, prompt="p")
            self.assertTrue(rec.called)
            called_name = rec.call_args[0][1]
            self.assertEqual(called_name, "proj-7")
        finally:
            for p in patches:
                p.stop()

    def test_run_chain_map_error_does_not_abort_chain(self):
        patches = self._make_patches()
        managers = [p.start() for p in patches]
        rec = managers[-1]
        rec.side_effect = Exception("uuid-lookup failed")
        try:
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                result = cc.run_chain("proj-7", "feature/proj-7", new_branch=True, prompt="p")
            self.assertEqual(result["status"], "ok")
            self.assertIn("warning", buf.getvalue())
        finally:
            for p in patches:
                p.stop()


class CloseEventTests(unittest.TestCase):
    """Task 8: resolve_closed_worktree + handle_close_event."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name) / "your-app-worktrees"
        (self.base / "proj-7").mkdir(parents=True)
        self._old_base = cc.WORKTREE_BASE
        cc.WORKTREE_BASE = self.base

    def tearDown(self):
        cc.WORKTREE_BASE = self._old_base
        self.tmp.cleanup()

    def test_resolve_by_cwd(self):
        ev = {"payload": {"cwd": str(self.base / "proj-7"), "workspace_id": "uuid-x"}}
        self.assertEqual(cc.resolve_closed_worktree(ev), "proj-7")

    def test_resolve_by_payload_name(self):
        ev = {"data": {"title": "proj-7", "workspace_id": "uuid-x"}}
        self.assertEqual(cc.resolve_closed_worktree(ev), "proj-7")

    def test_resolve_by_map_when_no_name(self):
        with patch("cmux_chain.load_workspace_map", return_value={"uuid-x": "proj-7"}):
            ev = {"data": {"workspace_id": "uuid-x"}}
            self.assertEqual(cc.resolve_closed_worktree(ev), "proj-7")

    def test_resolve_unknown_returns_none(self):
        with patch("cmux_chain.load_workspace_map", return_value={}):
            self.assertIsNone(cc.resolve_closed_worktree({"data": {"workspace_id": "nope"}}))

    def test_handle_close_tears_down_known_worktree(self):
        with patch("cmux_chain.resolve_closed_worktree", return_value="proj-7"), \
             patch("cmux_chain.teardown_worktree") as td:
            cc.handle_close_event({"data": {"workspace_id": "uuid-x"}})
        td.assert_called_once_with("proj-7", close_workspace=False)

    def test_handle_close_ignores_unresolved(self):
        with patch("cmux_chain.resolve_closed_worktree", return_value=None), \
             patch("cmux_chain.teardown_worktree") as td:
            cc.handle_close_event({"data": {}})
        td.assert_not_called()

    def test_handle_close_ignores_out_of_scope(self):
        with patch("cmux_chain.resolve_closed_worktree", return_value="proj-app"), \
             patch("cmux_chain.worktree_path_guard", side_effect=cc.TeardownRefused("prime")), \
             patch("cmux_chain.teardown_worktree") as td:
            cc.handle_close_event({"data": {}})
        td.assert_not_called()


class CloseListenTests(unittest.TestCase):
    """Task 9: cmd_close_listen streams workspace.closed events."""

    def test_dispatches_close_frames_and_skips_ack(self):
        frames = [
            '{"type":"ack"}',
            '{"name":"workspace.closed","data":{"workspace_id":"uuid-x"}}',
            '{"type":"heartbeat"}',
            '',
        ]

        class FakeProc:
            def __init__(self):
                self.stdout = iter(f + "\n" for f in frames)

        with patch("cmux_chain.subprocess.Popen", return_value=FakeProc()), \
             patch("cmux_chain.handle_close_event") as h:
            cc.cmd_close_listen(type("A", (), {})())
        self.assertEqual(h.call_count, 1)
        self.assertEqual(h.call_args[0][0]["name"], "workspace.closed")

    def test_survives_handler_exception_and_continues(self):
        frames = [
            '{"name":"workspace.closed","data":{"workspace_id":"a"}}',
            '{"name":"workspace.closed","data":{"workspace_id":"b"}}',
        ]

        class FakeProc:
            def __init__(self):
                self.stdout = iter(f + "\n" for f in frames)

        with patch("cmux_chain.subprocess.Popen", return_value=FakeProc()), \
             patch("cmux_chain.handle_close_event",
                   side_effect=[RuntimeError("boom"), None]) as h:
            rc = cc.cmd_close_listen(type("A", (), {})())
        self.assertEqual(rc, 0)
        self.assertEqual(h.call_count, 2)   # second frame processed after the first raised

    def test_popen_argv_has_reconnect_and_name_but_not_cursor_file(self):
        """Fix A: cmd_close_listen must stream from 'now' only -- no --cursor-file."""
        class FakeProc:
            def __init__(self):
                self.stdout = iter([])

        with patch("cmux_chain.subprocess.Popen", return_value=FakeProc()) as popen, \
             patch("cmux_chain.handle_close_event"):
            cc.cmd_close_listen(type("A", (), {})())
        argv = popen.call_args[0][0]
        self.assertIn("--reconnect", argv)
        self.assertIn("--name", argv)
        self.assertIn("workspace.closed", argv)
        self.assertNotIn("--cursor-file", argv)


class ArchiveSweepTests(unittest.TestCase):
    """Task 10: sweep_archive_group tears down proj-*/pr-* members of the Archive group."""

    GROUPS = [{
        "name": "Archive", "id": "g-arch",
        "anchor_workspace_ref": "workspace:20",
        "member_workspace_refs": ["workspace:20", "workspace:9", "workspace:6"],
    }]

    def test_sweep_tears_down_worktree_members_only(self):
        names = {"workspace:9": "proj-5864", "workspace:6": "pr-45647", "workspace:20": "Archive"}
        with patch("cmux_chain.cmux_group_list", return_value=self.GROUPS), \
             patch("cmux_chain.workspace_name_for_ref", side_effect=lambda r: names[r]), \
             patch("cmux_chain.os.path.isdir", return_value=True), \
             patch("cmux_chain.teardown_worktree") as td:
            cc.sweep_archive_group()
        torn = sorted(c[0][0] for c in td.call_args_list)
        self.assertEqual(torn, ["pr-45647", "proj-5864"])   # anchor (workspace:20) skipped

    def test_sweep_noop_when_no_archive_group(self):
        with patch("cmux_chain.cmux_group_list", return_value=[]), \
             patch("cmux_chain.teardown_worktree") as td:
            cc.sweep_archive_group()
        td.assert_not_called()

    def test_sweep_swallows_teardown_refusal_and_continues(self):
        names = {"workspace:9": "proj-5864", "workspace:6": "pr-45647", "workspace:20": "Archive"}
        with patch("cmux_chain.cmux_group_list", return_value=self.GROUPS), \
             patch("cmux_chain.workspace_name_for_ref", side_effect=lambda r: names[r]), \
             patch("cmux_chain.os.path.isdir", return_value=True), \
             patch("cmux_chain.teardown_worktree",
                   side_effect=[cc.TeardownRefused("nope"), None]) as td:
            cc.sweep_archive_group()   # must not raise
        self.assertEqual(td.call_count, 2)   # second member attempted after the refusal


# ---------------------------------------------------------------------------
# Change 1 tests: within_work_hours + cmd_cmux_poll work-hours gate
# ---------------------------------------------------------------------------

class WithinWorkHoursTests(unittest.TestCase):
    def _mock_now(self, isoweekday, hour):
        """Return a mock datetime.datetime with the given isoweekday() and hour."""
        dt = MagicMock()
        dt.isoweekday.return_value = isoweekday
        dt.hour = hour
        return dt

    def test_weekday_midday_returns_true(self):
        dt = self._mock_now(isoweekday=2, hour=10)  # Tuesday 10:00
        with patch("cmux_chain.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = dt
            self.assertTrue(cc.within_work_hours())

    def test_weekday_after_hours_returns_false(self):
        dt = self._mock_now(isoweekday=3, hour=22)  # Wednesday 22:00
        with patch("cmux_chain.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = dt
            self.assertFalse(cc.within_work_hours())

    def test_sunday_returns_false(self):
        dt = self._mock_now(isoweekday=7, hour=10)  # Sunday 10:00
        with patch("cmux_chain.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = dt
            self.assertFalse(cc.within_work_hours())

    def test_saturday_returns_false(self):
        dt = self._mock_now(isoweekday=6, hour=12)  # Saturday 12:00
        with patch("cmux_chain.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = dt
            self.assertFalse(cc.within_work_hours())

    def test_friday_hour_16_returns_true(self):
        dt = self._mock_now(isoweekday=5, hour=16)  # Friday 16:00 (boundary)
        with patch("cmux_chain.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = dt
            self.assertTrue(cc.within_work_hours())

    def test_weekday_hour_8_returns_true(self):
        dt = self._mock_now(isoweekday=1, hour=8)   # Monday 08:00 (boundary)
        with patch("cmux_chain.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = dt
            self.assertTrue(cc.within_work_hours())


class CmuxPollWorkHoursGateTests(unittest.TestCase):
    """cmd_cmux_poll: Jira spin-up is gated on within_work_hours; PR+Archive run unconditionally."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._c, self._p = sh.CACHE_PATH, sh.PR_CACHE_PATH
        sh.CACHE_PATH = pathlib.Path(self.tmp.name) / "spinup-surfaced.json"
        sh.PR_CACHE_PATH = pathlib.Path(self.tmp.name) / "pr-surfaced.json"
        self._pns = cc.PR_NOTIFY_STATE_PATH
        cc.PR_NOTIFY_STATE_PATH = pathlib.Path(self.tmp.name) / "cmux-pr-notify.json"

    def tearDown(self):
        sh.CACHE_PATH, sh.PR_CACHE_PATH = self._c, self._p
        cc.PR_NOTIFY_STATE_PATH = self._pns
        self.tmp.cleanup()

    @patch("cmux_chain.sweep_archive_group")
    @patch("cmux_chain.cmux_notify")
    @patch("spinup_helper.list_review_requests", return_value=[])
    @patch("cmux_chain.run_chain")
    @patch("spinup_helper.list_assigned_eligible",
           return_value=[{"key": "PROJ-9", "title": "T", "issue_type": "Bug", "status": "Triage"}])
    @patch("cmux_chain.within_work_hours", return_value=False)
    def test_off_hours_skips_jira_spin_but_calls_sweep(
            self, wh, elig, chain, prs, notify, sweep):
        """Off-hours: Jira spin-up (run_chain for tickets) is NOT called; sweep_archive_group IS called."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cc.cmd_cmux_poll(type("A", (), {})())
        self.assertEqual(rc, 0)
        chain.assert_not_called()
        sweep.assert_called_once()


# ---------------------------------------------------------------------------
# Change 2 tests: teardown order + git_worktree_remove_force backstop
# ---------------------------------------------------------------------------

class TeardownOrderTests(unittest.TestCase):
    """teardown_worktree must close workspace FIRST, then archive, then remove."""

    def test_close_then_archive_then_remove(self):
        calls = []
        with patch("cmux_chain.worktree_path_guard", return_value=pathlib.Path("/wt/proj-7")), \
             patch("cmux_chain.cmux_close_workspace", side_effect=lambda r: calls.append(("close", r))), \
             patch("cmux_chain.worktree_archive", side_effect=lambda n: calls.append(("archive", n))), \
             patch("cmux_chain.git_worktree_remove_force", side_effect=lambda p: calls.append(("remove", str(p)))), \
             patch("cmux_chain.prune_workspace_map"), \
             patch("cmux_chain.cmux_notify"):
            cc.teardown_worktree("proj-7", workspace_ref="workspace:9")
        self.assertEqual([c[0] for c in calls], ["close", "archive", "remove"])

    def test_close_workspace_false_skips_close_step(self):
        calls = []
        with patch("cmux_chain.worktree_path_guard", return_value=pathlib.Path("/wt/proj-7")), \
             patch("cmux_chain.cmux_close_workspace", side_effect=lambda r: calls.append(("close", r))), \
             patch("cmux_chain.worktree_archive", side_effect=lambda n: calls.append(("archive", n))), \
             patch("cmux_chain.git_worktree_remove_force", side_effect=lambda p: calls.append(("remove", str(p)))), \
             patch("cmux_chain.prune_workspace_map"), \
             patch("cmux_chain.cmux_notify"):
            cc.teardown_worktree("proj-7", close_workspace=False)
        # close must not appear at all
        self.assertNotIn("close", [c[0] for c in calls])
        self.assertEqual([c[0] for c in calls], ["archive", "remove"])


class GitWorktreeRemoveForceBackstopTests(unittest.TestCase):
    """git_worktree_remove_force: rmtree + prune when dir still exists after git remove."""

    @patch("cmux_chain.subprocess.run")
    @patch("cmux_chain.os.path.isdir", return_value=True)
    @patch("cmux_chain.shutil.rmtree")
    def test_rmtree_and_prune_called_when_dir_still_exists(self, rmtree, isdir, run):
        run.return_value = _run(returncode=0)
        cc.git_worktree_remove_force(pathlib.Path("/wt/proj-7"))
        rmtree.assert_called_once_with("/wt/proj-7", ignore_errors=True)
        # prune must be called as a second subprocess.run
        prune_calls = [c for c in run.call_args_list
                       if "prune" in (c[0][0] if c[0] else c[1].get("args", []))]
        self.assertTrue(len(prune_calls) >= 1, "git worktree prune should be called as backstop")

    @patch("cmux_chain.subprocess.run")
    @patch("cmux_chain.os.path.isdir", return_value=False)
    @patch("cmux_chain.shutil.rmtree")
    def test_no_rmtree_when_dir_already_gone(self, rmtree, isdir, run):
        run.return_value = _run(returncode=0)
        cc.git_worktree_remove_force(pathlib.Path("/wt/proj-7"))
        rmtree.assert_not_called()


# ---------------------------------------------------------------------------
# Change 3 tests: open_agent_tab idle path + jira_agent_prompt
# ---------------------------------------------------------------------------

class OpenAgentTabIdleTests(unittest.TestCase):
    """open_agent_tab with falsy prompt starts claude idle (no prompt send/submit)."""

    @patch("cmux_chain.read_surface", return_value="❯ ")
    @patch("cmux_chain._claude_transcripts", return_value=set())
    @patch("cmux_chain.time")
    @patch("cmux_chain.cmux_send_key")
    @patch("cmux_chain.cmux_send")
    @patch("cmux_chain.cmux_new_surface", return_value="surface:9")
    def test_none_prompt_starts_idle_no_second_send(
            self, news, send, sendkey, mock_time, transcripts, read_surf):
        """prompt=None -> sends 'claude' + enter, waits for ready, then returns; no prompt submitted."""
        mock_time.monotonic.side_effect = [0.0, 0.1]  # deadline + one check for wait_for_claude_ready
        mock_time.sleep = MagicMock()
        cc.open_agent_tab("workspace:3", None, "/wt/proj-1")
        # Only one cmux_send call: 'claude'
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args[0], ("surface:9", "claude"))
        # Only one send-key: enter after 'claude' launch
        self.assertEqual(sendkey.call_count, 1)
        self.assertEqual(sendkey.call_args[0], ("surface:9", "enter"))

    @patch("cmux_chain.read_surface", return_value="❯ ")
    @patch("cmux_chain._claude_transcripts", return_value=set())
    @patch("cmux_chain.time")
    @patch("cmux_chain.cmux_send_key")
    @patch("cmux_chain.cmux_send")
    @patch("cmux_chain.cmux_new_surface", return_value="surface:9")
    def test_empty_string_prompt_also_starts_idle(
            self, news, send, sendkey, mock_time, transcripts, read_surf):
        """prompt="" -> same idle behavior as None."""
        mock_time.monotonic.side_effect = [0.0, 0.1]
        mock_time.sleep = MagicMock()
        cc.open_agent_tab("workspace:3", "", "/wt/proj-1")
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args[0], ("surface:9", "claude"))
        self.assertEqual(sendkey.call_count, 1)


class JiraAgentPromptTests(unittest.TestCase):
    """jira_agent_prompt returns None (idle) ONLY when the branch exists AND has commits
    (real work); a new or empty-at-base branch gets the writing-plans prompt."""

    def test_returns_none_when_branch_exists_with_commits(self):
        with patch("cmux_chain.branch_exists", return_value=True), \
             patch("cmux_chain.branch_has_commits", return_value=True):
            result = cc.jira_agent_prompt("PROJ-99", "feature/proj-99-foo")
        self.assertIsNone(result)

    def test_returns_prompt_when_branch_exists_but_no_commits(self):
        # Recovery case: branch left by a failed spin-up, sitting at base HEAD (no work).
        with patch("cmux_chain.branch_exists", return_value=True), \
             patch("cmux_chain.branch_has_commits", return_value=False):
            result = cc.jira_agent_prompt("PROJ-99", "feature/proj-99-foo")
        self.assertIsNotNone(result)
        self.assertIn("PROJ-99", result)

    def test_returns_writing_plans_prompt_when_branch_new(self):
        with patch("cmux_chain.branch_exists", return_value=False):
            result = cc.jira_agent_prompt("PROJ-99", "feature/proj-99-foo")
        self.assertIsNotNone(result)
        self.assertIn("PROJ-99", result)


if __name__ == "__main__":
    unittest.main()
