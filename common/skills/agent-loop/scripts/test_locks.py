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

    def test_write_is_atomic_no_tmp_left_and_valid_json_always(self):
        """Verify _write() is atomic: no .tmp files left and owner() always returns valid JSON with all keys."""
        l = locks.Lease(self.path, "heavy"); l.acquire()
        l.heartbeat()
        # Check no .tmp sibling remains
        tmp_files = list(self.path.parent.glob(self.path.name + ".tmp*"))
        self.assertEqual(len(tmp_files), 0, f"Found leftover temp files: {tmp_files}")
        # Check owner() returns dict with all four keys
        rec = locks.owner(self.path)
        self.assertIsNotNone(rec)
        self.assertIsInstance(rec, dict)
        self.assertIn("pid", rec)
        self.assertIn("boot_id", rec)
        self.assertIn("heartbeat_utc", rec)
        self.assertIn("name", rec)

    def test_record_without_pid_is_reclaimable(self):
        """Records with missing or malformed pid should be reclaimable."""
        # Test 1: missing pid
        self.path.write_text(json.dumps({"boot_id": locks.boot_id(), "heartbeat_utc": "2999-01-01T00:00:00Z", "name": "heavy"}))
        self.assertTrue(locks.Lease(self.path, "heavy").acquire())
        
        # Test 2: malformed pid (garbage string)
        self.path.unlink()
        self.path.write_text(json.dumps({"pid": "garbage", "boot_id": locks.boot_id(), "heartbeat_utc": "2999-01-01T00:00:00Z", "name": "heavy"}))
        self.assertTrue(locks.Lease(self.path, "heavy").acquire())
        
        # Test 3: pid is None
        self.path.unlink()
        self.path.write_text(json.dumps({"pid": None, "boot_id": locks.boot_id(), "heartbeat_utc": "2999-01-01T00:00:00Z", "name": "heavy"}))
        self.assertTrue(locks.Lease(self.path, "heavy").acquire())

    def test_held_acquire_keeps_fd_open_and_release_closes_it(self):
        l = locks.Lease(self.path, "heavy")
        self.assertTrue(l.acquire(hold=True))
        self.assertIsNotNone(l._held_fd)
        self.assertFalse(l._held_fd.closed)
        l.release()
        self.assertTrue(l._held_fd is None or l._held_fd.closed)

    def test_held_acquire_nonblocking_second_process_fails_while_first_lives(self):
        script_dir = pathlib.Path(__file__).resolve().parent
        holder_src = (
            "import sys, time\n"
            f"sys.path.insert(0, {str(script_dir)!r})\n"
            "import locks\n"
            f"l = locks.Lease({str(self.path)!r}, 'heavy')\n"
            "ok = l.acquire(hold=True)\n"
            "print(ok, flush=True)\n"
            "time.sleep(30)\n"
        )
        holder = subprocess.Popen([sys.executable, "-c", holder_src], stdout=subprocess.PIPE, text=True)
        try:
            line = holder.stdout.readline().strip()
            self.assertEqual(line, "True")

            other_src = (
                "import sys\n"
                f"sys.path.insert(0, {str(script_dir)!r})\n"
                "import locks\n"
                f"l = locks.Lease({str(self.path)!r}, 'heavy')\n"
                "print(l.acquire(hold=True), flush=True)\n"
            )
            r = subprocess.run([sys.executable, "-c", other_src], capture_output=True, text=True)
            self.assertEqual(r.stdout.strip(), "False")

            # Simulate a crash: kill the holder without release(); its fd is closed by the kernel.
            holder.kill()
            holder.wait(timeout=5)

            r2 = subprocess.run([sys.executable, "-c", other_src], capture_output=True, text=True)
            self.assertEqual(r2.stdout.strip(), "True")
        finally:
            if holder.poll() is None:
                holder.kill()
                holder.wait(timeout=5)

if __name__ == "__main__":
    unittest.main()
