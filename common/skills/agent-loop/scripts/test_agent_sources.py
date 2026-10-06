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
    def test_tiers_define_all_five_new_tables_with_all_keys(self):
        t = tomllib.loads(TIERS.read_text())
        for name in ("worker-cloud", "worker-small", "worker-premium", "flagship-author", "flagship-critic", "task-writer"):
            self.assertIn(name, t, name)
            for k in ("claude", "codex_model", "codex_effort", "pi_model", "pi_thinking"):
                self.assertIn(k, t[name], f"{name}.{k}")
        self.assertTrue(t["worker-cloud"]["pi_model"].startswith("anthropic/"))   # Ollama Cloud disallowed (security)
        self.assertEqual(t["worker-small"]["pi_model"], "anthropic/claude-haiku-4-5")   # truly small tasks (Cody 2026-10-06)
        self.assertNotIn("worker-local", t)                                              # local-first retired: 0/12 accepted
        self.assertEqual(t["flagship-author"]["pi_model"], "anthropic/claude-opus-5-5")   # author tier one below flagship (2026-09-15)
        self.assertEqual(t["flagship-critic"]["pi_model"], "openai-codex/gpt-6-astra")
        # Anthropic tiers use the 5.5 family (Cody 2026-10-06).
        self.assertEqual(t["worker-cloud"]["pi_model"], "anthropic/claude-sonnet-5-5")
        self.assertEqual(t["task-writer"]["pi_model"], "anthropic/claude-sonnet-5-5")
    def test_worker_bodies_carry_the_contract(self):
        for w in WORKERS:
            body = (AGENTS / f"{w}.agent.md").read_text()
            for phrase in ("STATUS:", "result.md", "Do not commit", "allowed_files", "Never weaken"):
                self.assertIn(phrase, body, f"{w} missing {phrase!r}")

if __name__ == "__main__":
    unittest.main()
