import json
import pathlib
import tempfile
import time
import unittest

import providers


class ProviderHealthTests(unittest.TestCase):
    """Cody, 2026-10-08: a ChatGPT plan limit ("Try again in ~8405 min") stalled every Codex-backed agent.
    Once a provider is known to be exhausted, every launch goes straight to the other vendor's model."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)

    def test_models_pass_through_while_their_provider_is_healthy(self):
        self.assertEqual(providers.resolve(self.root, "openai/gpt-5.6-terra"), "openai/gpt-5.6-terra")

    def test_an_exhausted_provider_routes_every_model_to_its_equivalent(self):
        providers.mark_exhausted(self.root, "openai", "You have hit your ChatGPT usage limit (prolite plan). Try again in ~8405 min.")
        cases = {"openai/gpt-5.6-sol": "anthropic/claude-opus-5-5", "openai/gpt-6-astra": "anthropic/claude-fable-5-1",
                 "openai/gpt-5.6-terra": "anthropic/claude-sonnet-5-5", "openai/gpt-6-luna": "anthropic/claude-haiku-4-5",
                 "anthropic/claude-sonnet-5-5": "anthropic/claude-sonnet-5-5"}
        for model, want in cases.items():
            with self.subTest(model=model):
                self.assertEqual(providers.resolve(self.root, model), want)

    def test_and_vice_versa(self):
        providers.mark_exhausted(self.root, "anthropic", "rate_limit_error: usage limit reached")
        self.assertEqual(providers.resolve(self.root, "anthropic/claude-sonnet-5-5"), "openai/gpt-5.6-terra")
        self.assertEqual(providers.resolve(self.root, "anthropic/claude-opus-5-5"), "openai/gpt-5.6-sol")

    def test_the_reset_time_in_the_message_is_honoured_and_then_expires(self):
        providers.mark_exhausted(self.root, "openai", "Try again in ~8405 min.")
        until = json.loads((self.root / "providers.json").read_text())["openai"]["until"]
        self.assertAlmostEqual(until - time.time(), 8405 * 60, delta=120)
        providers.mark_exhausted(self.root, "openai", "The usage limit has been reached", now=time.time() - 10 * 86400)
        self.assertEqual(providers.resolve(self.root, "openai/gpt-5.6-terra"), "anthropic/claude-sonnet-5-5")   # a shorter mark never shortens a longer one

    def test_an_expired_mark_lets_the_model_back(self):
        providers.mark_exhausted(self.root, "openai", "usage limit has been reached", now=time.time() - 3 * 3600)
        self.assertEqual(providers.resolve(self.root, "openai/gpt-5.6-terra"), "openai/gpt-5.6-terra")

    def test_mark_healthy_immediately_restores_the_primary_provider_and_clears_legacy_alias(self):
        providers.mark_exhausted(self.root, "openai", "Try again in ~8405 min")
        providers.mark_exhausted(self.root, "openai-codex", "Try again in ~8405 min")
        self.assertEqual(providers.resolve(self.root, "openai/gpt-5.6-terra"), "anthropic/claude-sonnet-5-5")
        self.assertTrue(providers.mark_healthy(self.root, "openai"))
        self.assertEqual(providers.resolve(self.root, "openai/gpt-5.6-terra"), "openai/gpt-5.6-terra")
        health = json.loads((self.root / "providers.json").read_text())
        self.assertNotIn("openai", health)
        self.assertNotIn("openai-codex", health)

    def test_both_vendors_down_keeps_the_model(self):
        providers.mark_exhausted(self.root, "openai", "Try again in ~60 min")
        providers.mark_exhausted(self.root, "anthropic", "Try again in ~60 min")
        self.assertEqual(providers.resolve(self.root, "openai/gpt-5.6-terra"), "openai/gpt-5.6-terra")

    def test_exhaustion_is_detected_from_agent_logs(self):
        self.assertEqual(providers.exhausted_provider("Error: You have hit your ChatGPT usage limit (prolite plan).", "openai/gpt-5.6-sol"), "openai")
        self.assertEqual(providers.exhausted_provider("Codex error: The usage limit has been reached", "openai/x"), "openai")
        self.assertIsNone(providers.exhausted_provider("AssertionError: 1 != 2", "openai/x"))

    def test_every_launch_path_resolves_through_the_registry(self):
        import inspect, plan, runner, repair
        self.assertIn("providers.resolve(", inspect.getsource(plan._run_agent))
        self.assertIn("providers.resolve(", inspect.getsource(runner.Runner._attempt))
        self.assertIn("providers.note_run(", inspect.getsource(plan._run_agent))


if __name__ == "__main__":
    unittest.main()
