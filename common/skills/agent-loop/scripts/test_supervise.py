import os
import pathlib
import plistlib
import tempfile
import unittest
from unittest.mock import patch

import supervise


class PlistTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); root = pathlib.Path(self.tmp.name)
        self.p_agents = patch.object(supervise, "AGENTS_DIR", root / "LaunchAgents"); self.p_agents.start()
        self.p_launcher = patch.object(supervise, "ensure_launcher", return_value="/Users/cody/.local/bin/agent-loop-python"); self.p_launcher.start()
        self.state_root = root / "state"

    def tearDown(self):
        self.p_agents.stop(); self.p_launcher.stop(); self.tmp.cleanup()

    def test_plist_uses_signed_launcher_mise_first_path_and_keepalive_on_failure_only(self):
        p = supervise.write_plist("ZIP-7872", "/wt/zip-7872", self.state_root)
        d = plistlib.loads(p.read_bytes())
        self.assertEqual(d["Label"], "com.cody.agent-loop.zip-7872")
        self.assertEqual(d["ProgramArguments"][0], "/Users/cody/.local/bin/agent-loop-python")        # stable TCC identity, not homebrew python
        self.assertIn("--supervised", d["ProgramArguments"]); self.assertIn("ZIP-7872", d["ProgramArguments"])
        path = d["EnvironmentVariables"]["PATH"].split(":")
        self.assertTrue(path[0].endswith("mise/shims"), path[0])                                      # system Ruby 2.6 broke bin/wt prepare (cb32b32)
        self.assertEqual(d["KeepAlive"], {"SuccessfulExit": False})                                   # exit 0 (gate reached) unloads; non-zero restarts
        self.assertTrue(d["RunAtLoad"]); self.assertGreaterEqual(d["ThrottleInterval"], 60)
        self.assertTrue(d["StandardErrorPath"].endswith("zip-7872.err.log"))

    def test_a_runner_stopping_its_own_agent_still_removes_the_plist(self):
        # bootout of our own label kills the calling runner; the plist must already be gone, or the
        # supervisor reloads at the next login (ZIP-4281/4282 left stale plists at Human Gate 1).
        supervise.AGENTS_DIR.mkdir(parents=True, exist_ok=True)
        plist = supervise.plist_path("ZIP-1"); plist.write_text("x")
        def killed(*args):
            raise SystemExit(143)
        with patch.object(supervise, "_launchctl", side_effect=killed), self.assertRaises(SystemExit):
            supervise.stop("ZIP-1")
        self.assertFalse(plist.exists())

    def test_ensure_running_does_not_bootout_a_running_or_loaded_supervisor(self):
        with patch.object(supervise, "status", return_value="state=running, pid=123"), patch.object(supervise, "_launchctl", side_effect=AssertionError("must not restart live work")):
            self.assertFalse(supervise.ensure_running("ZIP-1", "/wt/a", self.state_root))
        with patch.object(supervise, "status", return_value="state=waiting"), patch.object(supervise, "_launchctl", side_effect=AssertionError("KeepAlive owns the loaded job")):
            self.assertFalse(supervise.ensure_running("ZIP-1", "/wt/a", self.state_root))

    def test_ensure_running_starts_missing_supervisor_once_and_respects_takeover(self):
        with patch.object(supervise, "status", return_value="not loaded"), patch.object(supervise, "start", side_effect=lambda key, wt, root: supervise.write_plist(key, wt, root)):
            self.assertTrue(supervise.ensure_running("ZIP-1", "/wt/a", self.state_root))
        self.assertTrue(supervise.plist_path("ZIP-1").exists())
        tdir = self.state_root / "tickets" / "ZIP-2"; tdir.mkdir(parents=True); (tdir / "HUMAN").touch()
        with patch.object(supervise, "status", return_value="not loaded"), patch.object(supervise, "start", side_effect=AssertionError("human owns this ticket")):
            self.assertFalse(supervise.ensure_running("ZIP-2", "/wt/b", self.state_root))

    def test_shadow_periodic_reconciler_always_uses_dry_run(self):
        with patch.object(supervise, "_launchctl", return_value=type("Result", (), {"returncode": 0})()):
            supervise.start_periodic(self.state_root, "cohort-reconcile", 60, extra_args=["--dry-run"])
        p = supervise.AGENTS_DIR / "com.cody.agent-loop.cohort-reconcile.plist"
        payload = plistlib.loads(p.read_bytes())
        self.assertEqual(payload["ProgramArguments"][-2:], ["cohort-reconcile", "--dry-run"])

    def test_periodic_cohort_reconciler_is_not_reported_as_a_ticket(self):
        supervise.write_plist("ZIP-1", "/wt/a", self.state_root)
        with patch.object(supervise, "_launchctl", return_value=type("Result", (), {"returncode": 0})()):
            supervise.start_periodic(self.state_root, "cohort-reconcile", 60)
            supervise.start_periodic(self.state_root, "repair", 300)
        self.assertEqual(supervise.active(), ["ZIP-1"])
        p = supervise.AGENTS_DIR / "com.cody.agent-loop.cohort-reconcile.plist"
        payload = plistlib.loads(p.read_bytes())
        self.assertEqual(payload["StartInterval"], 60)
        self.assertEqual(payload["ProgramArguments"][-1], "cohort-reconcile")

    def test_every_launch_agent_can_find_pi(self):
        # pi installs itself to ~/.pi/agent/bin (2026-10-16); launchd has no login shell to add it.
        with patch.object(supervise, "_launchctl", return_value=type("Result", (), {"returncode": 0})()):
            paths = [plistlib.loads(supervise.write_plist("ZIP-1", "/wt/a", self.state_root).read_bytes()),
                     plistlib.loads((supervise.AGENTS_DIR / (supervise.start_periodic(self.state_root, "repair", 300) + ".plist")).read_bytes()),
                     plistlib.loads((supervise.AGENTS_DIR / (supervise.start_drip(self.state_root) + ".plist")).read_bytes())]
        for plist in paths:
            entries = plist["EnvironmentVariables"]["PATH"].split(":")
            self.assertIn(os.path.expanduser("~/.pi/agent/bin"), entries)
            self.assertIn(os.path.expanduser("~/.local/bin"), entries)

    def test_label_and_active_roundtrip(self):
        supervise.write_plist("ZIP-1", "/wt/a", self.state_root); supervise.write_plist("ZIP-2", "/wt/b", self.state_root)
        self.assertEqual(supervise.active(), ["ZIP-1", "ZIP-2"])
        self.assertEqual(supervise.plist_path("ZIP-1").name, "com.cody.agent-loop.zip-1.plist")


class LauncherTests(unittest.TestCase):
    def test_launcher_source_execs_homebrew_python_and_forwards_signals(self):
        src = (pathlib.Path(__file__).resolve().parent.parent / "launcher" / "agent-loop-python.c").read_text()
        self.assertIn('"/opt/homebrew/bin/python3"', src); self.assertIn("SIGTERM", src); self.assertIn("waitpid", src)

    @unittest.skipUnless(pathlib.Path("~/.local/bin/agent-loop-python").expanduser().exists(), "launcher not built here")
    def test_built_launcher_has_stable_identifier_and_propagates_exit(self):
        import subprocess
        out = subprocess.run(["codesign", "-dv", str(pathlib.Path("~/.local/bin/agent-loop-python").expanduser())], capture_output=True, text=True).stderr
        self.assertIn("Identifier=com.cody.agent-loop.python", out)
        self.assertEqual(subprocess.run([str(pathlib.Path("~/.local/bin/agent-loop-python").expanduser()), "-c", "import sys; sys.exit(7)"]).returncode, 7)


if __name__ == "__main__":
    unittest.main()
