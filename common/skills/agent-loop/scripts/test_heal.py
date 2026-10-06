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
            "plan failed: critic verdict 'revise' after 2 rounds": "replan",
            "publish failed: PublishError: accepted task 002 left allowlisted paths uncommitted": "republish",
            "adjudication is not valid JSON: Invalid \\escape": "republish", "mark ready failed: x": "republish",
            "rebase onto origin/main conflicted before task 901; needs a human": "human",
            "reviewer questions need Cody: is this public API?": "human", "HUMAN file present": "human",
        }
        for reason, action in cases.items():
            with self.subTest(reason=reason): self.assertEqual(heal.policy(reason)[0], action)

    def test_provider_exhaustion_retries_behind_a_cooling_off_window(self):
        with tempfile.TemporaryDirectory() as d:
            tdir = pathlib.Path(d); r = "provider unavailable: Codex error: The usage limit has been reached"
            self.assertEqual(heal.policy(r), ("retry", 100))
            self.assertEqual(heal.decide(tdir, r)[0], "retry"); heal.record(tdir, "retry", r, "bot-loop")
            action, why = heal.decide(tdir, r)                      # immediately after: cool off, do not burn budget
            self.assertEqual(action, "wait"); self.assertIn("cooling off", why)

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

    def test_a_ticket_cannot_heal_forever(self):
        """Branch review: budgets were per reason bucket, so one root defect presenting under several
        reasons had no ticket-wide stop."""
        import tempfile, pathlib as _p
        with tempfile.TemporaryDirectory() as d:
            tdir = _p.Path(d)
            for i in range(heal.MAX_HEALS_PER_TICKET):
                heal.record(tdir, "retry", f"resource: transient number {i}", "implement")
            action, why = heal.decide(tdir, "resource: yet another transient")
            self.assertEqual(action, "human")
            self.assertIn("self-healed", why)

    def test_the_same_failure_recurring_stops_even_under_a_fresh_reason_string(self):
        import tempfile, pathlib as _p
        with tempfile.TemporaryDirectory() as d:
            tdir = _p.Path(d)
            for i in range(heal.MAX_SAME_SIGNATURE):
                # same failure, different volatile numbers/paths each time
                heal.record(tdir, "retry", f"gates failed: rails test /Users/x/wt{i}/test/a_test.rb:{i}", "gates")
            action, why = heal.decide(tdir, "gates failed: rails test /Users/x/wt99/test/a_test.rb:99")
            self.assertEqual(action, "human")
            self.assertIn("recurred", why)

    def test_an_operator_resume_starts_fresh_budgets_without_erasing_history(self):
        # 2026-10-06: ZIP-4281 spent its three replans on a runner defect; once it was fixed and the
        # ticket resumed, its first genuine critic revise was refused as "budget 3 spent".
        with tempfile.TemporaryDirectory() as d:
            tdir = pathlib.Path(d); r = "plan failed: task-writer left the manifest invalid: x"
            for _ in range(3): heal.record(tdir, "replan", r, "plan")
            self.assertEqual(heal.decide(tdir, "plan failed: critic verdict 'revise' after repair")[0], "human")
            heal.mark_operator_resume(tdir, "runner defect fixed")
            self.assertEqual(heal.decide(tdir, "plan failed: critic verdict 'revise' after repair"), ("replan", "replan (1/3)"))
            self.assertEqual(len(heal.history(tdir)), 4)                     # the audit trail is kept

    def test_distinct_failures_still_heal(self):
        import tempfile, pathlib as _p
        with tempfile.TemporaryDirectory() as d:
            tdir = _p.Path(d)
            heal.record(tdir, "retry", "resource: on battery power", "implement")
            self.assertEqual(heal.decide(tdir, "gates failed: yarn build rc=1")[0], "prepare-retry")


if __name__ == "__main__":
    unittest.main()
