import pathlib
import tempfile
import unittest
from unittest.mock import patch

import plan
import procs


class PureTests(unittest.TestCase):
    def test_assignment_alternates_models_not_roles(self):
        """The role definitions are fixed; the MODEL alternates so the critic is never the plan's vendor.
        (Found live 2026-09-12: swapping definitions told Astra it was the critic of a plan nobody wrote.)"""
        A = type("D", (), {"model": "anthropic/fable"})(); C = type("D", (), {"model": "openai-codex/astra"})()
        with patch.object(plan.agentdef, "load", side_effect=lambda d, n: A if n == "flagship-author" else C):
            cfg = type("Cfg", (), {"pi_agents_dir": "x"})()
            self.assertEqual(plan.assignment(cfg, 1), {"author_model": "anthropic/fable", "critic_model": "openai-codex/astra"})
            self.assertEqual(plan.assignment(cfg, 2), {"author_model": "openai-codex/astra", "critic_model": "anthropic/fable"})

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
    """Drive plan_ticket with a scripted fake agent: the author writes a manifest, the critic writes a verdict."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.wt = pathlib.Path(self.tmp.name) / "wt"; self.wt.mkdir()
        self.stage = pathlib.Path(self.tmp.name) / "stage"
        self.cfg = type("Cfg", (), {"pi_agents_dir": "/nonexistent"})()
        self.script = []           # list of callables(agent_name) executed in order
        self.calls = []; self.models = []

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

    def go(self):
        with patch.object(plan, "_run_agent", self.fake_run_agent), patch.object(plan, "export_ticket", lambda k, d: d), \
             patch.object(plan, "shipped_summary", lambda wt: ""), \
             patch.object(plan, "assignment", lambda cfg, i: {"author_model": "M-author", "critic_model": "M-critic"}):
            return plan.plan_ticket(self.cfg, "ZIP-7873", self.wt, 2, self.stage)

    def test_stale_review_is_moved_aside_before_round_one(self):
        pd = self.wt / "planning" / "zip-7873"; pd.mkdir(parents=True); (pd / "plan-review.md").write_text("VERDICT: revise\nstale")
        self.script = [self.author_writes(self.GOOD), self.critic_says("approve")]
        self.assertEqual(self.go()["result"], "approved")
        self.assertTrue(list(pd.glob("plan-review.stale-*.md")))

    def test_approve_first_round(self):
        self.script = [self.author_writes(self.GOOD), self.critic_says("approve")]
        log = self.go()
        self.assertEqual(log["result"], "approved"); self.assertEqual(self.calls, ["flagship-author", "flagship-critic"])
        self.assertEqual(self.models, ["M-author", "M-critic"])          # roles fixed; models per assignment

    def test_schema_violation_goes_back_to_author_without_critic(self):
        self.script = [self.author_writes('[[tasks]]\nid = "001"\n'), self.author_writes(self.GOOD), self.critic_says("approve")]
        log = self.go()
        self.assertEqual(log["result"], "approved"); self.assertEqual(self.calls[:2], ["flagship-author", "flagship-author"])
        self.assertTrue(log["rounds"][0]["schema_violations"])
        self.assertIn("Schema violations", (self.stage / "author-2" / "prompt.md").read_text())

    def test_revise_then_approve(self):
        self.script = [self.author_writes(self.GOOD), self.critic_says("revise"), self.author_writes(self.GOOD), self.critic_says("approve")]
        log = self.go()
        self.assertEqual(log["result"], "approved"); self.assertEqual(len(log["rounds"]), 2)
        self.assertIn("Critic review to address", (self.stage / "author-2" / "prompt.md").read_text())

    def test_block_stops_immediately(self):
        self.script = [self.author_writes(self.GOOD), self.critic_says("block")]
        self.assertEqual(self.go()["result"], "blocked")

    def test_author_blocked_marker_stops(self):
        def f(agent, pd): return "PLAN: blocked — tier-4 question"
        self.script = [f]; log = self.go()
        self.assertEqual(log["result"], "blocked"); self.assertIn("tier-4", log["reason"])

    def test_rounds_are_bounded(self):
        self.script = [self.author_writes(self.GOOD), self.critic_says("revise")] * 4
        log = self.go()
        self.assertEqual(log["result"], "failed"); self.assertLessEqual(len(self.calls), 2 * (plan.PLAN_ROUNDS + 1))


if __name__ == "__main__":
    unittest.main()
