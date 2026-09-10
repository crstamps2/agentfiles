import json, os, pathlib, subprocess, sys, tempfile, unittest
import locks

class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "heavy"
    def tearDown(self):
        self.tmp.cleanup()

    def test_acquire_writes_owner_record(self):
        l = locks.Lease(self.path, "heavy")
        self.assertTrue(l.acquire())
        rec = locks.owner(self.path)
        self.assertEqual(rec["pid"], os.getpid())
        self.assertEqual(rec["boot_id"], locks.boot_id())
        self.assertEqual(rec["name"], "heavy")

    def test_second_acquire_by_live_owner_fails(self):
        a = locks.Lease(self.path, "heavy"); self.assertTrue(a.acquire())
        b = locks.Lease(self.path, "heavy"); self.assertFalse(b.acquire())

    def test_release_allows_reacquire(self):
        a = locks.Lease(self.path, "heavy"); a.acquire(); a.release()
        self.assertIsNone(locks.owner(self.path))
        self.assertTrue(locks.Lease(self.path, "heavy").acquire())

    def test_dead_pid_is_reclaimable(self):
        # spawn a child that exits immediately; its pid is dead by the time we read it
        child = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
        dead = int(child.stdout.strip())
        self.path.write_text(json.dumps({"pid": dead, "boot_id": locks.boot_id(), "heartbeat_utc": "2000-01-01T00:00:00Z", "name": "heavy"}))
        self.assertTrue(locks.Lease(self.path, "heavy").acquire())

    def test_live_pid_old_heartbeat_is_NOT_reclaimable(self):
        self.path.write_text(json.dumps({"pid": os.getpid(), "boot_id": locks.boot_id(), "heartbeat_utc": "2000-01-01T00:00:00Z", "name": "heavy"}))
        self.assertFalse(locks.Lease(self.path, "heavy").acquire())

    def test_different_boot_id_is_reclaimable_even_if_pid_alive(self):
        self.path.write_text(json.dumps({"pid": os.getpid(), "boot_id": "not-this-boot", "heartbeat_utc": "2999-01-01T00:00:00Z", "name": "heavy"}))
        self.assertTrue(locks.Lease(self.path, "heavy").acquire())

    def test_context_manager_releases(self):
        with locks.Lease(self.path, "heavy") as held:
            self.assertTrue(held)
        self.assertIsNone(locks.owner(self.path))

    def test_heartbeat_updates_timestamp(self):
        l = locks.Lease(self.path, "heavy"); l.acquire()
        t0 = locks.owner(self.path)["heartbeat_utc"]
        l.heartbeat()
        self.assertGreaterEqual(locks.owner(self.path)["heartbeat_utc"], t0)

if __name__ == "__main__":
    unittest.main()
