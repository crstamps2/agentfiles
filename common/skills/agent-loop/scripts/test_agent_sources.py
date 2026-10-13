# test_agent_sources.py — guards the source-of-truth agent files, not the rendered ones
import pathlib, re, tomllib, unittest

ROOT = pathlib.Path(__file__).resolve().parents[4]          # agentfiles repo
AGENTS = ROOT / "common" / "agents"
TIERS = ROOT / "common" / "model-tiers.toml"
WORKERS = ("cloud-worker", "small-worker", "premium-worker")

def fm(path):
    text = path.read_text().splitlines()
    end = text.index("---", 1)
    return dict(l.split(":", 1) for l in text[1:end] if ":" in l)

class WorkerSourceTests(unittest.TestCase):
    def test_worker_sources_exist_with_tier_and_access(self):
        for w in WORKERS:
            f = fm(AGENTS / f"{w}.agent.md")
            self.assertEqual(f["name"].strip(), w)
            self.assertEqual(f["tier"].strip(), f"worker-{w.split('-')[0]}")
            self.assertEqual(f["access"].strip(), "Read, Grep, Glob, Bash, Write, Edit")
            self.assertNotIn("fallbackModels", f)
    def test_work_tiers_prefer_codex_and_review_gates_use_claude(self):
        t = tomllib.loads(TIERS.read_text())
        expected = {
            "orchestrator": "failover/sol",
            "specialist": "openai/gpt-5.6-terra",
            "specialist-execution": "openai/gpt-5.6-terra",
            "specialist-fast": "openai/gpt-6-luna",
            "data-fetch": "openai/gpt-6-luna",
            "worker-cloud": "openai/gpt-5.6-terra",
            "worker-small": "openai/gpt-6-luna",
            "worker-premium": "openai/gpt-5.6-terra",
            "flagship-author": "openai/gpt-5.6-sol",
            "flagship-critic": "anthropic/claude-fable-5-1",
            "task-writer": "openai/gpt-5.6-terra",
            "repair-agent": "openai/gpt-5.6-terra",
            "repair-reviewer": "anthropic/claude-opus-5-5",
        }
        for name, model in expected.items():
            self.assertIn(name, t, name)
            for key in ("claude", "codex_model", "codex_effort", "pi_model", "pi_thinking"):
                self.assertIn(key, t[name], f"{name}.{key}")
            self.assertEqual(t[name]["pi_model"], model, name)
            if name not in ("orchestrator", "flagship-critic", "repair-reviewer"):
                self.assertTrue(model.startswith("openai/"), name)
        self.assertEqual(t["orchestrator"]["pi_thinking"], "high")
        self.assertEqual(t["specialist"]["pi_thinking"], "xhigh")
        self.assertEqual(t["specialist-execution"]["pi_thinking"], "max")
        self.assertNotIn("worker-local", t)   # local-first retired: 0/12 accepted

    def test_execution_specialists_use_the_execution_tier(self):
        execution = {"android-qa-engineer", "frontend-engineer", "qa-engineer", "rails-engineer", "technical-writer"}
        for name in execution:
            self.assertEqual(fm(AGENTS / f"{name}.agent.md")["tier"].strip(), "specialist-execution", name)
    def test_worker_bodies_carry_the_contract(self):
        for w in WORKERS:
            body = (AGENTS / f"{w}.agent.md").read_text()
            for phrase in ("STATUS:", "result.md", "Do not commit", "allowed_files", "Never weaken"):
                self.assertIn(phrase, body, f"{w} missing {phrase!r}")

if __name__ == "__main__":
    unittest.main()
