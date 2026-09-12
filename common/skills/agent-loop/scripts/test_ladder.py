import pathlib, tempfile, unittest
import attempt as attempt_mod
import ladder
from ladder import Attempt, Rung

ALT = ["cloud", "local"]

class AssignArmTests(unittest.TestCase):
    def test_pin_wins(self):
        self.assertEqual(ladder.assign_arm(0, False, "local", ALT), "local")
        self.assertEqual(ladder.assign_arm(1, True, "cloud", ALT), "cloud")
    def test_alternates_by_index(self):
        self.assertEqual([ladder.assign_arm(i, False, None, ALT) for i in range(4)], ["cloud", "local", "cloud", "local"])
    def test_invalid_pin_raises(self):
        with self.assertRaises(ValueError):
            ladder.assign_arm(0, False, "premium", ALT)
    def test_empty_alternate_raises(self):
        with self.assertRaises(ValueError):
            ladder.assign_arm(0, False, None, [])

class NextRungTests(unittest.TestCase):
    def test_sequence_for_cloud_arm(self):
        r1 = ladder.next_rung([], "cloud")
        self.assertEqual(r1, Rung("cloud-worker", "cheap", 1))
        r2 = ladder.next_rung([Attempt(r1, "rejected")], "cloud")
        self.assertEqual(r2, Rung("cloud-worker", "cheap", 2))
        r3 = ladder.next_rung([Attempt(r1, "rejected"), Attempt(r2, "timeout")], "cloud")
        self.assertEqual(r3, Rung("premium-worker", "premium", 1))
        self.assertIsNone(ladder.next_rung([Attempt(r1, "rejected"), Attempt(r2, "timeout"), Attempt(r3, "protocol")], "cloud"))
    def test_local_arm_uses_local_worker(self):
        self.assertEqual(ladder.next_rung([], "local").agent, "local-worker")
    def test_accepted_ends_ladder(self):
        r1 = ladder.next_rung([], "cloud")
        self.assertIsNone(ladder.next_rung([Attempt(r1, "accepted")], "cloud"))
    def test_environment_does_not_consume_rung(self):
        r1 = ladder.next_rung([], "cloud")
        self.assertEqual(ladder.next_rung([Attempt(r1, "environment")], "cloud"), r1)
        self.assertEqual(ladder.next_rung([Attempt(r1, "environment"), Attempt(r1, "environment")], "cloud"), r1)
    def test_unknown_outcome_raises(self):
        with self.assertRaises(ValueError):
            ladder.next_rung([Attempt(Rung("cloud-worker", "cheap", 1), "rejeted")], "cloud")

class FeedbackTests(unittest.TestCase):
    def test_append_feedback_accumulates(self):
        with tempfile.TemporaryDirectory() as d:
            p = ladder.append_feedback(pathlib.Path(d), 1, "Lint failed: trailing whitespace in well.rb:12. Tests were not run.")
            ladder.append_feedback(pathlib.Path(d), 2, "Test well_test.rb:33 expects a footer slot; none rendered.")
            text = p.read_text()
            self.assertIn("## Attempt 1", text); self.assertIn("## Attempt 2", text); self.assertIn("footer slot", text)
            self.assertEqual(p.name, "feedback.md")

    def test_append_feedback_creates_file_via_safe_write_when_absent(self):
        with tempfile.TemporaryDirectory() as d:
            p = ladder.append_feedback(pathlib.Path(d), 1, "first")
            self.assertTrue(p.is_file())
            self.assertIn("## Attempt 1", p.read_text())

    def test_append_feedback_on_symlink_raises_unsafe_path(self):
        with tempfile.TemporaryDirectory() as d:
            outside = pathlib.Path(d) / "outside.md"
            outside.write_text("secret\n")
            task_dir = pathlib.Path(d) / "task"
            task_dir.mkdir()
            (task_dir / "feedback.md").symlink_to(outside)
            with self.assertRaises(attempt_mod.UnsafePath):
                ladder.append_feedback(task_dir, 1, "gate summary")
            self.assertEqual(outside.read_text(), "secret\n")


class NextActionTests(unittest.TestCase):
    def test_blocked_outcome_always_blocks(self):
        self.assertEqual(ladder.next_action([], "cloud", "blocked", 0), "block")

    def test_accepted_never_blocks_even_if_it_were_the_last_rung(self):
        r1 = ladder.next_rung([], "cloud")
        r2 = ladder.next_rung([Attempt(r1, "rejected")], "cloud")
        r3 = ladder.next_rung([Attempt(r1, "rejected"), Attempt(r2, "timeout")], "cloud")
        history = [Attempt(r1, "rejected"), Attempt(r2, "timeout")]
        self.assertEqual(ladder.next_action(history, "cloud", "accepted", 0), "none")

    def test_ladder_exhausted_and_not_accepted_blocks(self):
        r1 = ladder.next_rung([], "cloud")
        r2 = ladder.next_rung([Attempt(r1, "rejected")], "cloud")
        history = [Attempt(r1, "rejected"), Attempt(r2, "timeout")]
        # this attempt is the premium rung; a non-accepted outcome exhausts the ladder
        self.assertEqual(ladder.next_action(history, "cloud", "protocol", 0), "block")

    def test_more_rungs_remaining_and_not_accepted_does_not_block(self):
        self.assertEqual(ladder.next_action([], "cloud", "rejected", 0), "none")

    def test_environment_outcome_below_threshold_is_none(self):
        self.assertEqual(ladder.next_action([], "cloud", "environment", 1), "none")

    def test_environment_outcome_at_threshold_pauses_env(self):
        self.assertEqual(ladder.next_action([], "cloud", "environment", 2), "pause-env")
        self.assertEqual(ladder.next_action([], "cloud", "environment", 3), "pause-env")

if __name__ == "__main__":
    unittest.main()


class SummarizeFeedbackTests(unittest.TestCase):
    def test_wall_of_paths_is_grouped_and_counted(self):
        wall = "; ".join([f"tmp/cache/bootsnap/{i:02d}: not in allowed_files for task 001" for i in range(120)]
                         + ["app/models/user.rb: not in allowed_files for task 001", "bin/x: protected path (never editable by workers)"])
        out = ladder.summarize_feedback(wall)
        self.assertLess(len(out), 600); self.assertIn("122 findings", out); self.assertIn("121×", out)
        self.assertIn("app/models/user.rb", out); self.assertIn("(+118 more)", out)
    def test_short_reason_passes_through(self):
        self.assertEqual(ladder.summarize_feedback("verification failed: bin/rails test x"), "verification failed: bin/rails test x")

