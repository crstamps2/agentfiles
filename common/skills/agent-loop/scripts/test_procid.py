import os, subprocess, sys, time, unittest
from unittest.mock import patch
import locks, procid

class CaptureTests(unittest.TestCase):
    def test_capture_self_has_all_fields(self):
        p = procid.capture(os.getpid())
        self.assertEqual(p.boot_id, locks.boot_id()); self.assertEqual(p.pid, os.getpid())
        self.assertEqual(p.pgid, os.getpgid(os.getpid())); self.assertTrue(p.start_time); self.assertTrue(p.cmd)
        self.assertEqual(procid.ProcId.from_dict(p.to_dict()), p)

class ClassifyTests(unittest.TestCase):
    def setUp(self):
        self.p = subprocess.Popen(["sleep", "60"], start_new_session=True); time.sleep(0.1)
        self.rec = procid.capture(self.p.pid)
    def tearDown(self):
        try: os.killpg(self.rec.pgid, 9); self.p.wait(timeout=2)
        except Exception: pass

    def test_live_matching_leader_is_ours_alive(self):
        self.assertEqual(procid.classify(self.rec), "ours-alive")
    def test_none_is_dead(self):
        self.assertEqual(procid.classify(None), "dead")
    def test_dead_group_is_dead(self):
        os.killpg(self.rec.pgid, 9); self.p.wait(timeout=2); time.sleep(0.1)
        self.assertEqual(procid.classify(self.rec), "dead")
    def test_other_boot_id_is_unknown(self):
        rec = procid.ProcId(boot_id="other-boot", pgid=self.rec.pgid, pid=self.rec.pid, start_time=self.rec.start_time, cmd=self.rec.cmd)
        self.assertEqual(procid.classify(rec), "unknown")
    def test_proc_from_an_earlier_real_boot_is_dead(self):
        # Nothing survives a reboot; treating it as unknown fenced the loop forever after one.
        rec = procid.ProcId(**{**self.rec.to_dict(), "boot_id": "A9619ECE-8CE9-4A7E-9570-47EE2C55C878"})
        with patch.object(locks, "boot_id", return_value="28CD7EE2-C0CC-4CEA-ACE3-F1535DB1A4BE"):
            self.assertEqual(procid.classify(rec), "dead")
    def test_unreadable_current_boot_stays_unknown(self):
        rec = procid.ProcId(**{**self.rec.to_dict(), "boot_id": "A9619ECE-8CE9-4A7E-9570-47EE2C55C878"})
        with patch.object(locks, "boot_id", return_value="unknown-boot"):
            self.assertEqual(procid.classify(rec), "unknown")
    def test_start_time_mismatch_is_unknown(self):
        rec = procid.ProcId(**{**self.rec.to_dict(), "start_time": "Thu Jan  1 00:00:00 1970"})
        self.assertEqual(procid.classify(rec), "unknown")
    def test_cmd_mismatch_is_still_ours_alive(self):
        # cmd is informational: the launch-gate shell execs into the real command, so the
        # recorded cmd (sh) legitimately differs from the live one. Identity = boot_id+pid+start_time.
        rec = procid.ProcId(**{**self.rec.to_dict(), "cmd": "definitely-not-sleep"})
        self.assertEqual(procid.classify(rec), "ours-alive")
    def test_leader_gone_group_populated_is_unknown(self):
        # spawn a leader that forks a child then exits: group stays populated without its leader
        code = "import os,time,subprocess; subprocess.Popen(['sleep','60']); time.sleep(0.2)"
        p = subprocess.Popen([sys.executable, "-c", code], start_new_session=True); time.sleep(0.1)
        rec = procid.capture(p.pid); p.wait(timeout=5); time.sleep(0.2)
        try:
            self.assertEqual(procid.classify(rec), "unknown")
        finally:
            try: os.killpg(rec.pgid, 9)
            except ProcessLookupError: pass
    def test_eperm_is_unknown(self):
        with patch("procid.os.killpg", side_effect=PermissionError):
            self.assertEqual(procid.classify(self.rec), "unknown")

if __name__ == "__main__": unittest.main()
