import pathlib, tempfile, unittest
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

class FeedbackTests(unittest.TestCase):
    def test_append_feedback_accumulates(self):
        with tempfile.TemporaryDirectory() as d:
            p = ladder.append_feedback(pathlib.Path(d), 1, "Lint failed: trailing whitespace in well.rb:12. Tests were not run.")
            ladder.append_feedback(pathlib.Path(d), 2, "Test well_test.rb:33 expects a footer slot; none rendered.")
            text = p.read_text()
            self.assertIn("## Attempt 1", text); self.assertIn("## Attempt 2", text); self.assertIn("footer slot", text)
            self.assertEqual(p.name, "feedback.md")

if __name__ == "__main__":
    unittest.main()
