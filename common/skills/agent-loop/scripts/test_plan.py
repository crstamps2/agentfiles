import pathlib
import tempfile
import unittest
from unittest.mock import patch

import plan
import procs


class PureTests(unittest.TestCase):
    def test_assignment_uses_codex_author_and_opposite_vendor_critic(self):
        cfg = type("Cfg", (), {"pi_agents_dir": "x"})()
        expected = {"author_model": "openai/gpt-5.6-sol", "critic_model": "anthropic/claude-fable-5-1"}
        self.assertEqual(plan.assignment(cfg, 1), expected)
        self.assertEqual(plan.assignment(cfg, 2), expected)

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

    def test_planner_and_critic_are_told_what_workers_cannot_reach(self):
        # 2026-10-06: ZIP-4281's plan made four tasks wait on the WORKER inspecting a Figma node; workers have
        # file and shell tools only, so premium blocked/owner and the ticket stopped.
        with tempfile.TemporaryDirectory() as d:
            wt = pathlib.Path(d); tj = wt / "ticket.json"; tj.write_text("{}")
            for s in (plan._author_prompt("ZIP-1", tj, wt, "", None, []), plan._critic_prompt("ZIP-1", tj, wt)):
                self.assertIn("cannot inspect Figma", s)
                self.assertIn("planning/zip-1/figma-", s)
                # ZIP-4281/010: a `figma connect publish --dry-run` gate needs FIGMA_ACCESS_TOKEN, which workers lack.
                self.assertIn("no credentials or API tokens", s)


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

    def test_writer_schema_violations_go_back_to_the_writer(self):
        # 2026-10-06: one rejected command from the task-writer failed the whole plan; both Nav tickets
        # spent all three replans that way. The author gets its violations back -- so does the writer.
        self.script = [self.author_writes(self.GOOD), self.critic_says("revise"), self.writer_applies('[[tasks]]\nid = "001"\n'),
                       self.writer_applies(self.GOOD), self.critic_says("approve")]
        log = self.go()
        self.assertEqual(log["result"], "approved")
        self.assertEqual(self.calls, ["flagship-author", "flagship-critic", "task-writer", "task-writer", "flagship-critic"])
        self.assertIn("Schema violations", (self.stage / "writer-1-fix-1" / "prompt.md").read_text())

    def test_writer_leaving_invalid_manifest_after_its_fix_passes_fails(self):
        bad = '[[tasks]]\nid = "001"\n'
        self.script = [self.author_writes(self.GOOD), self.critic_says("revise")] + [self.writer_applies(bad)] * 3
        log = self.go(); self.assertEqual(log["result"], "failed"); self.assertIn("task-writer left", log["reason"])
        self.assertEqual(self.calls.count("task-writer"), 3)

    def test_repair_writer_receives_existing_schema_failures_before_its_first_attempt(self):
        pd = self.wt / "planning" / "zip-7873"; pd.mkdir(parents=True)
        (pd / "plan.md").write_text("plan")
        (pd / "tasks.toml").write_text(self.GOOD.replace('["true"]', '["ruby -e \'puts 1\'"]'))
        (pd / "plan-review.md").write_text("VERDICT: revise\nBLOCKERS:\n1. Repair the verification command")

        def repair_when_informed(agent, directory):
            prompt = (self.stage / "writer-1" / "prompt.md").read_text()
            if "inline interpreter code" not in prompt:
                return "REVISION: blocked - missing verification schema feedback"
            (directory / "tasks.toml").write_text(self.GOOD)
            return "REVISION: applied 1 blockers"

        self.script = [repair_when_informed, self.critic_says("approve")]
        with patch.object(plan, "_run_agent", self.fake_run_agent), patch.object(plan, "export_ticket", lambda k, d: d), \
             patch.object(plan, "assignment", lambda cfg, i: {"author_model": "A", "critic_model": "C"}):
            log = plan.repair_plan(self.cfg, "ZIP-7873", self.wt, 2, self.stage)
        self.assertEqual(log["result"], "approved")
        self.assertEqual(plan.validate_manifest(pd / "tasks.toml"), [])

    def test_repair_plan_bounces_writer_violations_to_the_writer(self):
        pd = self.wt / "planning" / "zip-7873"; pd.mkdir(parents=True)
        (pd / "plan.md").write_text("plan"); (pd / "tasks.toml").write_text(self.GOOD); (pd / "plan-review.md").write_text("VERDICT: revise\nBLOCKERS:\n1. x")
        self.script = [self.writer_applies('[[tasks]]\nid = "001"\n'), self.writer_applies(self.GOOD), self.critic_says("approve")]
        with patch.object(plan, "_run_agent", self.fake_run_agent), patch.object(plan, "export_ticket", lambda k, d: d), \
             patch.object(plan, "assignment", lambda cfg, i: {"author_model": "A", "critic_model": "C"}):
            log = plan.repair_plan(self.cfg, "ZIP-7873", self.wt, 2, self.stage)
        self.assertEqual(log["result"], "approved"); self.assertEqual(self.calls, ["task-writer", "task-writer", "flagship-critic"])

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
        self.assertEqual(plan.sibling_model("openai/gpt-6-astra"), "anthropic/claude-fable-5-1")
        self.assertEqual(plan.sibling_model("anthropic/claude-opus-5-5"), "openai/gpt-5.6-sol")
        self.assertEqual(plan.sibling_model("anthropic/claude-sonnet-5-5"), "openai/gpt-5.6-terra")
        self.assertEqual(plan.sibling_model("openai/gpt-5.6-terra"), "anthropic/claude-sonnet-5-5")
        self.assertEqual(plan.sibling_model("anthropic/claude-haiku-4-5"), "openai/gpt-6-luna")
        self.assertEqual(plan.sibling_model("openai/gpt-6-luna"), "anthropic/claude-haiku-4-5")

    def test_workers_fail_over_to_the_same_sibling_table(self):
        import runner
        for model in ("openai/gpt-5.6-terra", "anthropic/claude-haiku-4-5"):
            self.assertEqual(runner.Runner.worker_alternate(model), plan.sibling_model(model))
        self.assertIsNone(plan.sibling_model("something/unknown"))

    def test_subscription_sharing_wording_is_a_provider_refusal_but_prose_is_not(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            stage = pathlib.Path(d)
            (stage / "stderr.log").write_text("The ChatGPT user has reached their Subscription Sharing usage limit.\n")
            self.assertTrue(plan.provider_exhausted(stage))
            (stage / "stderr.log").write_text("")
            (stage / "stdout.log").write_text("Root cause: the classifier missed the usage limit wording and a 429.\n")
            self.assertEqual(plan.provider_exhausted(stage), "")

    def test_provider_exhaustion_retries_on_the_other_vendor_and_records_it(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d); calls = []
            def fake(cfg, agent, prompt, stage, wt, timeout, model=None):
                calls.append(model); stage.mkdir(parents=True, exist_ok=True)
                if model == "openai/gpt-6-astra":
                    (stage / "stderr.log").write_text("Codex error: The usage limit has been reached\n")
                else:
                    (stage / "stdout.log").write_text("ADJUDICATION: written\n")
            with patch.object(plan, "_run_agent", side_effect=fake):
                res, used, out = plan.run_agent_with_failover(None, "review-adjudicator", "p", root / "s", root, 60,
                                                              model="openai/gpt-6-astra", log=lambda m: None)
            self.assertEqual(calls, ["openai/gpt-6-astra", "anthropic/claude-fable-5-1"])
            self.assertEqual(used, "anthropic/claude-fable-5-1"); self.assertEqual(out, "")
            self.assertIn("gpt-6-astra", (root / "s" / "failover.json").read_text())
            self.assertIn("ADJUDICATION: written", (root / "s" / "stdout.log").read_text())   # artifacts copied back

    def test_missing_legacy_codex_credentials_fail_over_to_the_other_vendor(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d); calls = []
            def fake(cfg, agent, prompt, stage, wt, timeout, model=None):
                calls.append(model); stage.mkdir(parents=True, exist_ok=True)
                if model == "openai-codex/gpt-6-astra":
                    (stage / "stderr.log").write_text("No API key found for openai-codex.\n")
                else:
                    (stage / "adjudication.json").write_text('{"decisions": []}')
            with patch.object(plan, "_run_agent", side_effect=fake):
                _res, used, out = plan.run_agent_with_failover(None, "review-adjudicator", "p", root / "s", root, 60,
                                                                 model="openai-codex/gpt-6-astra", log=lambda m: None)
            self.assertEqual(calls, ["openai-codex/gpt-6-astra", "anthropic/claude-fable-5-1"])
            self.assertEqual(used, "anthropic/claude-fable-5-1"); self.assertEqual(out, "")
            self.assertTrue((root / "s" / "adjudication.json").exists())

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

