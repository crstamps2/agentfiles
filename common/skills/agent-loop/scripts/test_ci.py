import unittest

import ci

C = ci.Check


class ClassifyTests(unittest.TestCase):
    def test_all_pass_or_skipped_is_green(self):
        v = ci.classify([C("rubocop", "pass"), C("claude-review", "skipping")], behind=0, attempts=0)
        self.assertEqual(v.action, "green")

    def test_pending_waits_even_if_behind(self):
        v = ci.classify([C("zipline", "pending"), C("rubocop", "pass")], behind=3, attempts=0)
        self.assertEqual(v.action, "wait"); self.assertEqual(v.pending, ["zipline"])

    def test_fail_and_behind_rebases_first_when_failure_is_not_a_flake(self):
        v = ci.classify([C("zipline", "fail")], behind=2, attempts=1, failure_text="NoMethodError")
        self.assertEqual(v.action, "rebase")

    def test_fail_after_a_rebase_does_not_rebase_again(self):
        v = ci.classify([C("zipline", "fail")], behind=1, attempts=2, failure_text="NoMethodError", prior_actions=("rebase",))
        self.assertEqual(v.action, "fix")

    def test_infra_signature_reruns_once_then_treats_as_real(self):
        v = ci.classify([C("zipline", "fail")], behind=0, attempts=1, failure_text="Error: ECONNRESET while fetching")
        self.assertEqual(v.action, "rerun")
        v = ci.classify([C("zipline", "fail")], behind=0, attempts=2, failure_text="Error: ECONNRESET while fetching", prior_actions=("rerun",))
        self.assertEqual(v.action, "fix")

    def test_flaky_capybara_reruns(self):
        v = ci.classify([C("zipline", "fail")], behind=0, attempts=1, failure_text="Capybara::ElementNotFound: Unable to find css")
        self.assertEqual(v.action, "rerun")

    def test_code_failure_is_fix(self):
        v = ci.classify([C("rubocop", "fail")], behind=0, attempts=1, failure_text="Style/StringLiterals: Prefer single-quoted strings")
        self.assertEqual(v.action, "fix"); self.assertEqual(v.failing, ["rubocop"])

    def test_cap_escalates(self):
        v = ci.classify([C("zipline", "fail")], behind=0, attempts=ci.MAX_CI_ATTEMPTS, failure_text="x")
        self.assertEqual(v.action, "escalate")

    def test_cancelled_counts_as_failing(self):
        v = ci.classify([C("zipline", "cancel")], behind=0, attempts=0, failure_text="The operation was canceled")
        self.assertEqual(v.action, "rerun")

    def test_no_log_available_reruns_once_then_fix(self):
        self.assertEqual(ci.classify([C("z", "fail")], behind=0, attempts=1).action, "rerun")
        self.assertEqual(ci.classify([C("z", "fail")], behind=0, attempts=2, prior_actions=("rerun",)).action, "fix")


class LinkParsingTests(unittest.TestCase):
    def test_circleci_workflow_id(self):
        self.assertEqual(ci.circleci_workflow_id("https://app.circleci.com/workflow/d1a2c3f9-f1a7-4af3-ae7d-fc118103a349"),
                         "d1a2c3f9-f1a7-4af3-ae7d-fc118103a349")
        self.assertIsNone(ci.circleci_workflow_id("https://github.com/x/y/actions/runs/1"))


if __name__ == "__main__":
    unittest.main()


class CircleCITests(unittest.TestCase):
    def test_es_shard_503_is_flaky_and_reruns_once(self):
        txt = "## job unit_tests #1 failed\n- Communication::Search::AnalysisTest::x\n  Elastic::Transport::Transport::Errors::ServiceUnavailable: [503] missing shards"
        self.assertTrue(ci.looks_flaky(txt))
        v = ci.classify([ci.Check("zipline", "fail", link="https://app.circleci.com/workflow/8e147625-2636-481a-9cad-2484a176ba46")], behind=11, attempts=1, failure_text=txt)
        self.assertEqual(v.action, "rerun")                  # a visible flake outranks "behind base": a rebase cannot fix an ES 503

    def test_failure_excerpt_reads_failed_jobs_and_tests(self):
        from unittest.mock import patch
        jobs = {"items": [{"name": "unit_tests", "status": "failed", "job_number": 7}, {"name": "jest", "status": "success", "job_number": 8}]}
        tests = {"items": [{"result": "failure", "classname": "A", "name": "b", "message": "boom"}, {"result": "success"}]}
        with patch.object(ci, "_circle", side_effect=lambda p: jobs if p.endswith("/job") else tests):
            txt = ci.circleci_failure_excerpt("https://app.circleci.com/workflow/8e147625-2636-481a-9cad-2484a176ba46")
        self.assertIn("unit_tests #7 failed", txt); self.assertIn("A::b", txt); self.assertIn("boom", txt); self.assertNotIn("jest", txt)

