import os, pathlib, subprocess, sys, tempfile, time, unittest
import procs

class RunStageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.d = pathlib.Path(self.tmp.name)
    def tearDown(self):
        self.tmp.cleanup()

    def test_success_captures_output_and_elapsed(self):
        r = procs.run_stage([sys.executable, "-c", "print('hi'); import sys; print('err', file=sys.stderr)"],
                            cwd=self.d, timeout_s=10, env=None,
                            stdout_path=self.d/"out", stderr_path=self.d/"err")
        self.assertEqual(r.returncode, 0); self.assertFalse(r.timed_out)
        self.assertEqual((self.d/"out").read_text().strip(), "hi")
        self.assertEqual((self.d/"err").read_text().strip(), "err")
        self.assertGreater(r.elapsed_s, 0)

    def test_timeout_kills_grandchildren_too(self):
        # child spawns a grandchild `sleep 60` that would outlive a naive kill
        timeout_s = 1.0
        code = "import subprocess,time; p=subprocess.Popen(['sleep','60']); open('gpid','w').write(str(p.pid)); time.sleep(60)"
        r = procs.run_stage([sys.executable, "-c", code], cwd=self.d, timeout_s=timeout_s, env=None,
                            stdout_path=self.d/"out", stderr_path=self.d/"err")
        self.assertTrue(r.timed_out)
        self.assertLess(r.elapsed_s, timeout_s + 2.0)
        self.assertIs(r.terminated, True)
        gpid = int((self.d/"gpid").read_text())
        time.sleep(0.2)
        with self.assertRaises(ProcessLookupError):
            os.kill(gpid, 0)
        self.assertFalse(procs.group_alive(r.pgid))

    def test_kill_group_reaps_leader_promptly(self):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             start_new_session=True)
        pgid = os.getpgid(p.pid)
        t0 = time.monotonic()
        ok = procs.kill_group(pgid, grace_s=5.0, reap=p)
        elapsed = time.monotonic() - t0
        self.assertTrue(ok)
        self.assertLess(elapsed, 1.0)

    def test_success_sets_terminated_true(self):
        r = procs.run_stage([sys.executable, "-c", "print('hi')"],
                            cwd=self.d, timeout_s=10, env=None,
                            stdout_path=self.d/"out", stderr_path=self.d/"err")
        self.assertIs(r.terminated, True)

    def test_env_is_passed_and_cwd_honored(self):
        r = procs.run_stage([sys.executable, "-c", "import os; print(os.environ['AL_X']); print(os.getcwd())"],
                            cwd=self.d, timeout_s=5, env={**os.environ, "AL_X": "42"},
                            stdout_path=self.d/"out", stderr_path=self.d/"err")
        out = (self.d/"out").read_text().splitlines()
        self.assertEqual(out[0], "42"); self.assertEqual(pathlib.Path(out[1]).resolve(), self.d.resolve())

if __name__ == "__main__":
    unittest.main()
