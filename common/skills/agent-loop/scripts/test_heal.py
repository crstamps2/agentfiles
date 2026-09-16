import pathlib
import tempfile
import unittest

import heal


class PolicyTests(unittest.TestCase):
    def test_reasons_map_to_actions(self):
        cases = {
            "resource: on battery power": "retry", "heavy lane held by a live owner": "retry",
            "gates failed: yarn build rc=1": "prepare-retry", "visual gate failed: https://x/lookbook/ answered 500": "prepare-retry",
            "guard failed on the BASE tree too (independent of the work): ! grep x": "repair", "contract complaint (stale-todo): ...": "repair",
            "plan failed: critic verdict 'revise' after 2 rounds": "repair",
            "publish failed: PublishError: accepted task 002 left allowlisted paths uncommitted": "republish",
            "adjudication is not valid JSON: Invalid \\escape": "republish", "mark ready failed: x": "republish",
            "rebase onto origin/main conflicted before task 901; needs a human": "human",
            "reviewer questions need Cody: is this public API?": "human", "HUMAN file present": "human",
        }
        for reason, action in cases.items():
            with self.subTest(reason=reason): self.assertEqual(heal.policy(reason)[0], action)

    def test_unknown_reason_gets_one_careful_retry(self):
        self.assertEqual(heal.policy("something new"), ("retry", 2))

    def test_budget_is_per_reason_and_spent_budget_escalates_to_human(self):
        with tempfile.TemporaryDirectory() as d:
            tdir = pathlib.Path(d)
            r = "gates failed: rails test rc=1"
            self.assertEqual(heal.decide(tdir, r)[0], "prepare-retry"); heal.record(tdir, "prepare-retry", r, "gates")
            self.assertEqual(heal.decide(tdir, r)[0], "prepare-retry"); heal.record(tdir, "prepare-retry", r, "gates")
            action, why = heal.decide(tdir, r); self.assertEqual(action, "human"); self.assertIn("budget 2 spent", why)
            # a different reason has its own budget
            self.assertEqual(heal.decide(tdir, "visual gate failed: 500")[0], "prepare-retry")
            self.assertEqual(len(heal.history(tdir)), 2)

    def test_human_policies_never_consume_budget(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(heal.decide(pathlib.Path(d), "rebase conflict; needs a human"), ("human", "policy: needs an owner"))


if __name__ == "__main__":
    unittest.main()
