# test_state.py
import json, pathlib, tempfile, unittest
import state

class TransitionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.d = pathlib.Path(self.tmp.name)
    def tearDown(self):
        self.tmp.cleanup()

    def test_new_ticket_is_queued_and_persists_atomically(self):
        t = state.load(self.d / "ZIP-1")
        self.assertEqual(t.state, "queued"); self.assertEqual(t.key, "ZIP-1")
        state.save(self.d / "ZIP-1", t)
        self.assertTrue((self.d / "ZIP-1" / "state.json").exists())
        self.assertFalse(list((self.d / "ZIP-1").glob("*.tmp")))
        self.assertEqual(state.load(self.d / "ZIP-1").state, "queued")

    def test_forward_path(self):
        t = state.load(self.d / "Z")
        for s in ("spinup", "plan", "plan-review", "implement", "gates", "draft-pr", "ready", "bot-loop", "human-gate-1", "colleague-loop", "human-gate-2", "done"):
            t = state.transition(t, s)
        self.assertEqual(t.state, "done")

    def test_illegal_skip_raises(self):
        t = state.load(self.d / "Z")
        with self.assertRaises(state.IllegalTransition):
            state.transition(t, "ready")

    def test_new_sha_rule_returns_to_gates(self):
        t = state.load(self.d / "Z")
        for s in ("spinup", "plan", "plan-review", "implement", "gates", "draft-pr", "ready", "bot-loop"):
            t = state.transition(t, s)
        t = state.transition(t, "gates", reason="new commit abc")
        self.assertEqual(t.state, "gates"); self.assertEqual(t.previous, "bot-loop")

    def test_pause_and_resume(self):
        t = state.transition(state.load(self.d / "Z"), "spinup")
        t = state.transition(t, "paused", reason="resource")
        self.assertEqual(t.reason, "resource")
        t = state.transition(t, "spinup")           # resume to previous only
        self.assertEqual(t.state, "spinup")
        with self.assertRaises(state.IllegalTransition):
            state.transition(state.transition(t, "paused"), "gates")

    def test_blocked_can_only_return_to_implement_or_plan(self):
        t = state.load(self.d / "Z")
        for s in ("spinup", "plan", "plan-review", "implement"):
            t = state.transition(t, s)
        t = state.transition(t, "blocked", reason="ladder exhausted")
        self.assertEqual(state.transition(t, "plan").state, "plan")
        with self.assertRaises(state.IllegalTransition):
            state.transition(t, "gates")

    def test_operator_files(self):
        root = self.d
        self.assertFalse(state.paused(root)); (root / "PAUSE").touch(); self.assertTrue(state.paused(root))
        td = root / "tickets" / "Z"; td.mkdir(parents=True)
        self.assertFalse(state.human_owned(td)); (td / "HUMAN").touch(); self.assertTrue(state.human_owned(td))

    def test_vendor_alternation(self):
        self.assertEqual(state.assign_vendors(1), ("fable", "astra"))
        self.assertEqual(state.assign_vendors(2), ("astra", "fable"))

if __name__ == "__main__":
    unittest.main()
