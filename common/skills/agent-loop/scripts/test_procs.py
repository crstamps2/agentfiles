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

    # ----- Fix round 1: Finding 1, launch gate journals pgid before exec --------------

    def test_child_does_not_execute_before_on_start(self):
        times = {}
        def on_start(pgid):
            times["pgid"] = pgid
            time.sleep(0.5)
            times["done"] = time.time()
        r = procs.run_stage(
            [sys.executable, "-c",
             "import time,pathlib; pathlib.Path('t').write_text(str(time.time()))"],
            cwd=self.d, timeout_s=10, env=None,
            stdout_path=self.d/"out", stderr_path=self.d/"err", on_start=on_start)
        self.assertEqual(r.returncode, 0)
        ts = float((self.d / "t").read_text())
        self.assertGreaterEqual(ts, times["done"])

    def test_on_start_exception_prevents_execution(self):
        captured = {}
        def on_start(pgid):
            captured["pgid"] = pgid
            raise RuntimeError("boom")
        with self.assertRaises(RuntimeError):
            procs.run_stage(
                [sys.executable, "-c",
                 "import pathlib; pathlib.Path('sentinel').write_text('ran')"],
                cwd=self.d, timeout_s=10, env=None,
                stdout_path=self.d/"out", stderr_path=self.d/"err", on_start=on_start)
        self.assertFalse((self.d / "sentinel").exists())
        time.sleep(0.2)
        self.assertFalse(procs.group_alive(captured["pgid"]))


    def test_planted_stdout_symlink_raises_and_does_not_clobber(self):
        outside = self.d / "outside.txt"; outside.write_text("keep")
        target = self.d / "linked_out"
        os.symlink(str(outside), target)
        sentinel = self.d / "sentinel"
        with self.assertRaises(procs.LogPathExists):
            procs.run_stage(
                [sys.executable, "-c",
                 f"import pathlib; pathlib.Path({str(sentinel)!r}).write_text('ran')"],
                cwd=self.d, timeout_s=10, env=None,
                stdout_path=target, stderr_path=self.d / "err2")
        self.assertEqual(outside.read_text(), "keep")
        self.assertFalse(sentinel.exists())

    def test_stderr_open_failure_cleans_up_stdout_log(self):
        stderr_path = self.d / "err"
        stdout_path = self.d / "out"
        # Pre-create stderr_path as a regular file to trigger LogPathExists on stderr open
        stderr_path.write_text("preexisting")
        sentinel = self.d / "sentinel"
        with self.assertRaises(procs.LogPathExists):
            procs.run_stage(
                [sys.executable, "-c",
                 f"import pathlib; pathlib.Path({str(sentinel)!r}).write_text('ran')"],
                cwd=self.d, timeout_s=10, env=None,
                stdout_path=stdout_path, stderr_path=stderr_path)
        # Verify stdout file was cleaned up
        self.assertFalse(stdout_path.exists())
        # Verify command did not execute
        self.assertFalse(sentinel.exists())


class GroupStateTests(unittest.TestCase):
    def test_group_state_dead_for_reaped_process(self):
        p = subprocess.Popen([sys.executable, "-c", "import os; print(os.getpid())"],
                             start_new_session=True, stdout=subprocess.PIPE, text=True)
        pgid, _ = os.getpgid(p.pid), p.communicate()
        p.wait()
        self.assertEqual(procs.group_state(pgid), "dead")

    def test_group_state_alive_for_live_group(self):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             start_new_session=True)
        pgid = os.getpgid(p.pid)
        try:
            self.assertEqual(procs.group_state(pgid), "alive")
        finally:
            procs.kill_group(pgid, grace_s=0.5, reap=p)


if __name__ == "__main__":
    unittest.main()
