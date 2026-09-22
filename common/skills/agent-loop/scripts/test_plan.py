import pathlib
import tempfile
import unittest
from unittest.mock import patch

import plan
import procs


class PureTests(unittest.TestCase):
    def test_assignment_alternates_vendors_author_below_flagship(self):
        cfg = type("Cfg", (), {"pi_agents_dir": "x"})()
        self.assertEqual(plan.assignment(cfg, 1), {"author_model": "anthropic/claude-opus-5-5", "critic_model": "openai-codex/gpt-6-astra"})
        self.assertEqual(plan.assignment(cfg, 2), {"author_model": "openai-codex/gpt-5.6-sol", "critic_model": "anthropic/claude-fable-5-1"})

    def test_review_verdict_parsing(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "r.md"
            self.assertEqual(plan.review_verdict(p), "missing")
            p.write_text("notes\nVERDICT: Revise\nBLOCKERS:\n1. x"); self.assertEqual(plan.review_verdict(p), "revise")
            p.write_text("no verdict line"); self.assertEqual(plan.review_verdict(p), "malformed")

    def test_validate_manifest_reports_missing_and_invalid(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "tasks.toml"
            self.assertTrue(plan.validate_manifest(p)[0].endswith("was not written"))
            p.write_text("this is = not toml ["); self.assertIn("not valid TOML", plan.validate_manifest(p)[0])
            p.write_text('[[tasks]]\nid = "001"\n'); self.assertTrue(plan.validate_manifest(p))

    def test_author_prompt_passes_paths_not_content_and_flags_review(self):
        with tempfile.TemporaryDirectory() as d:
            wt = pathlib.Path(d); tj = wt / "ticket.json"; tj.write_text("{}")
            s = plan._author_prompt("ZIP-1", tj, wt, "abc123 shipped thing", None, [])
            self.assertIn(str(tj), s); self.assertIn("planning/zip-1/tasks.toml", s); self.assertIn("ABSOLUTE", s)
            s2 = plan._author_prompt("ZIP-1", tj, wt, "", wt / "planning/zip-1/plan-review.md", ["tasks[0]: missing slug"])
            self.assertIn("Critic review to address", s2); self.assertIn("missing slug", s2)


class LoopTests(unittest.TestCase):
    """Drive plan_ticket with a scripted fake agent. Shape: author → (critic → task-writer)×≤PLAN_ROUNDS."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.wt = pathlib.Path(self.tmp.name) / "wt"; self.wt.mkdir()
        self.stage = pathlib.Path(self.tmp.name) / "stage"
        self.cfg = type("Cfg", (), {"pi_agents_dir": "/nonexistent"})()
        self.script = []; self.calls = []; self.models = []

    def tearDown(self):
        self.tmp.cleanup()

    def fake_run_agent(self, cfg, agent_name, prompt, stage_dir, wt, timeout_s, model=None):
        stage_dir.mkdir(parents=True, exist_ok=True); (stage_dir / "prompt.md").write_text(prompt)
        self.calls.append(agent_name); self.models.append(model)
        marker = self.script.pop(0)(agent_name, self.wt / "planning" / "zip-7873")
        (stage_dir / "stdout.log").write_text(marker + "\n")
        return procs.StageResult(returncode=0, timed_out=False, elapsed_s=1.0, pgid=0)

    GOOD = ('[[tasks]]\nid = "001"\nslug = "s"\nsummary = "x"\nallowed_files = ["a/**"]\n'
            'verification_commands = ["true"]\nacceptance = ["AC-1"]\n')

    def author_writes(self, manifest_text):
        def f(agent, pd):
            pd.mkdir(parents=True, exist_ok=True); (pd / "plan.md").write_text("plan"); (pd / "tasks.toml").write_text(manifest_text); return "PLAN: written"
        return f

    def critic_says(self, verdict):
        def f(agent, pd):
            (pd / "plan-review.md").write_text(f"VERDICT: {verdict}\nBLOCKERS:\n"); return f"REVIEW: {verdict}"
        return f

    def writer_applies(self, manifest_text=None):
        def f(agent, pd):
            if manifest_text is not None: (pd / "tasks.toml").write_text(manifest_text)
            return "REVISION: applied 1 blockers"
        return f

    def go(self):
        with patch.object(plan, "_run_agent", self.fake_run_agent), patch.object(plan, "export_ticket", lambda k, d: d), \
             patch.object(plan, "shipped_summary", lambda wt: ""), patch.object(plan, "_spent", lambda root: 0.0), \
             patch.object(plan, "assignment", lambda cfg, i: {"author_model": "M-author", "critic_model": "M-critic"}):
            return plan.plan_ticket(self.cfg, "ZIP-7873", self.wt, 2, self.stage)

    def test_approve_first_round(self):
        self.script = [self.author_writes(self.GOOD), self.critic_says("approve")]
        log = self.go()
        self.assertEqual(log["result"], "approved"); self.assertEqual(self.calls, ["flagship-author", "flagship-critic"])
        self.assertEqual(self.models, ["M-author", "M-critic"])

    def test_schema_violation_goes_back_to_author_without_critic(self):
        self.script = [self.author_writes('[[tasks]]\nid = "001"\n'), self.author_writes(self.GOOD), self.critic_says("approve")]
        log = self.go()
        self.assertEqual(log["result"], "approved"); self.assertEqual(self.calls[:2], ["flagship-author", "flagship-author"])
        self.assertIn("Schema violations", (self.stage / "author-2" / "prompt.md").read_text())

    def test_revise_goes_to_task_writer_then_final_critic_round(self):
        self.script = [self.author_writes(self.GOOD), self.critic_says("revise"), self.writer_applies(), self.critic_says("approve")]
        log = self.go()
        self.assertEqual(log["result"], "approved")
        self.assertEqual(self.calls, ["flagship-author", "flagship-critic", "task-writer", "flagship-critic"])   # NO second author round
        self.assertIn("FINAL ROUND", (self.stage / "critic-2" / "prompt.md").read_text())
        self.assertNotIn("FINAL ROUND", (self.stage / "critic-1" / "prompt.md").read_text())

    def test_second_revise_fails_the_plan_bounded(self):
        self.script = [self.author_writes(self.GOOD), self.critic_says("revise"), self.writer_applies(), self.critic_says("revise")]
        log = self.go()
        self.assertEqual(log["result"], "failed"); self.assertIn("after 2 rounds", log["reason"]); self.assertEqual(len(self.calls), 4)

    def test_writer_leaving_invalid_manifest_fails(self):
        self.script = [self.author_writes(self.GOOD), self.critic_says("revise"), self.writer_applies('[[tasks]]\nid = "001"\n')]
        log = self.go(); self.assertEqual(log["result"], "failed"); self.assertIn("task-writer left", log["reason"])

    def test_block_stops_immediately(self):
        self.script = [self.author_writes(self.GOOD), self.critic_says("block")]
        self.assertEqual(self.go()["result"], "blocked")

    def test_author_blocked_marker_stops(self):
        def f(agent, pd): return "PLAN: blocked — tier-4 question"
        self.script = [f]; log = self.go()
        self.assertEqual(log["result"], "blocked"); self.assertIn("tier-4", log["reason"])

    def test_budget_stops_planning(self):
        self.script = [self.author_writes(self.GOOD), self.critic_says("revise"), self.writer_applies(), self.critic_says("revise")]
        with patch.object(plan, "_run_agent", self.fake_run_agent), patch.object(plan, "export_ticket", lambda k, d: d), \
             patch.object(plan, "shipped_summary", lambda wt: ""), patch.object(plan, "_spent", side_effect=[0.0, 30.0]), \
             patch.object(plan, "assignment", lambda cfg, i: {"author_model": "a", "critic_model": "c"}):
            log = plan.plan_ticket(self.cfg, "ZIP-7873", self.wt, 2, self.stage)
        self.assertEqual(log["result"], "failed"); self.assertIn("budget exceeded", log["reason"])

    def test_repair_plan_applies_review_then_one_critic_round(self):
        pd = self.wt / "planning" / "zip-7873"; pd.mkdir(parents=True)
        (pd / "plan.md").write_text("plan"); (pd / "tasks.toml").write_text(self.GOOD); (pd / "plan-review.md").write_text("VERDICT: revise\nBLOCKERS:\n1. x")
        self.script = [self.writer_applies(), self.critic_says("approve")]
        with patch.object(plan, "_run_agent", self.fake_run_agent), patch.object(plan, "export_ticket", lambda k, d: d), \
             patch.object(plan, "assignment", lambda cfg, i: {"author_model": "A", "critic_model": "C"}):
            log = plan.repair_plan(self.cfg, "ZIP-7873", self.wt, 2, self.stage)
        self.assertEqual(log["result"], "approved"); self.assertEqual(self.calls, ["task-writer", "flagship-critic"])   # no author round
        self.assertIn("FINAL ROUND", (self.stage / "critic-1" / "prompt.md").read_text())

    def test_repair_plan_refuses_without_a_revise_review(self):
        with patch.object(plan, "assignment", lambda cfg, i: {"author_model": "A", "critic_model": "C"}):
            log = plan.repair_plan(self.cfg, "ZIP-7873", self.wt, 2, self.stage)
        self.assertEqual(log["result"], "failed"); self.assertIn("nothing to repair", log["reason"])

    def test_stale_review_is_moved_aside_before_round_one(self):
        pd = self.wt / "planning" / "zip-7873"; pd.mkdir(parents=True); (pd / "plan-review.md").write_text("VERDICT: revise\nstale")
        self.script = [self.author_writes(self.GOOD), self.critic_says("approve")]
        self.assertEqual(self.go()["result"], "approved"); self.assertTrue(list(pd.glob("plan-review.stale-*.md")))

    def test_resumes_from_existing_revise_review(self):
        pd = self.wt / "planning" / "zip-7873"; pd.mkdir(parents=True)
        (pd / "plan.md").write_text("plan"); (pd / "tasks.toml").write_text(self.GOOD); (pd / "plan-review.md").write_text("VERDICT: revise\nBLOCKERS:\n1. x")
        self.script = [self.author_writes(self.GOOD), self.critic_says("approve")]
        log = self.go(); self.assertEqual(log["result"], "approved"); self.assertTrue(log.get("resumed_from_review"))
        self.assertIn("Critic review to address", (self.stage / "author-1" / "prompt.md").read_text())


if __name__ == "__main__":
    unittest.main()


class FailoverTests(unittest.TestCase):
    def test_sibling_model_is_the_other_vendor_same_tier(self):
        self.assertEqual(plan.sibling_model("openai-codex/gpt-6-astra"), "anthropic/claude-fable-5-1")
        self.assertEqual(plan.sibling_model("anthropic/claude-opus-5-5"), "openai-codex/gpt-5.6-sol")
        self.assertEqual(plan.sibling_model("anthropic/claude-sonnet-5"), "openai-codex/gpt-5.4-mini")
        self.assertIsNone(plan.sibling_model("something/unknown"))

    def test_provider_exhaustion_retries_on_the_other_vendor_and_records_it(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d); calls = []
            def fake(cfg, agent, prompt, stage, wt, timeout, model=None):
                calls.append(model); stage.mkdir(parents=True, exist_ok=True)
                if model == "openai-codex/gpt-6-astra":
                    (stage / "stderr.log").write_text("Codex error: The usage limit has been reached\n")
                else:
                    (stage / "stdout.log").write_text("ADJUDICATION: written\n")
            with patch.object(plan, "_run_agent", side_effect=fake):
                res, used, out = plan.run_agent_with_failover(None, "review-adjudicator", "p", root / "s", root, 60,
                                                              model="openai-codex/gpt-6-astra", log=lambda m: None)
            self.assertEqual(calls, ["openai-codex/gpt-6-astra", "anthropic/claude-fable-5-1"])
            self.assertEqual(used, "anthropic/claude-fable-5-1"); self.assertEqual(out, "")
            self.assertIn("gpt-6-astra", (root / "s" / "failover.json").read_text())
            self.assertIn("ADJUDICATION: written", (root / "s" / "stdout.log").read_text())   # artifacts copied back

    def test_both_vendors_exhausted_reports_it(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            def dead(cfg, agent, prompt, stage, wt, timeout, model=None):
                stage.mkdir(parents=True, exist_ok=True); (stage / "stderr.log").write_text("429 rate limit\n")
            with patch.object(plan, "_run_agent", side_effect=dead):
                res, used, out = plan.run_agent_with_failover(None, "r", "p", root / "s", root, 60,
                                                              model="anthropic/claude-fable-5-1", log=lambda m: None)
            self.assertTrue(out)

    def test_a_review_older_than_the_plan_is_not_this_round_s_verdict(self):
        """ZIP-7874: the critic died on a usage limit, wrote nothing, and a 3-day-old 'revise' was read as
        the verdict for a freshly repaired plan -- spending the last replan budget on a phantom."""
        import os, time as _t, tempfile as _tf
        with _tf.TemporaryDirectory() as d:
            pd = pathlib.Path(d); review = pd / "plan-review.md"; plan_md = pd / "plan.md"
            review.write_text("VERDICT: revise\n"); plan_md.write_text("# plan")
            old = _t.time() - 3 * 86400
            os.utime(review, (old, old))
            self.assertFalse(plan.review_is_current(review, plan_md))       # stale: ignore it
            os.utime(review, None)
            self.assertTrue(plan.review_is_current(review, plan_md))

