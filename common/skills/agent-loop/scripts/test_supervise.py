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
