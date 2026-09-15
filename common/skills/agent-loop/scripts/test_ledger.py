import json
import pathlib
import tempfile
import unittest

import ledger


def turn(model, provider, inp, out, cache, cost, ts="2026-09-14T10:00:00Z"):
    return json.dumps({"timestamp": ts, "message": {"role": "assistant", "provider": provider, "model": model,
                       "usage": {"input": inp, "output": out, "cacheRead": cache, "cost": {"total": cost}}}})


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = pathlib.Path(self.tmp.name)
        a = self.root / "attempts/ZIP-1/001/1/session"; a.mkdir(parents=True)
        (a / "s.jsonl").write_text("\n".join([turn("gpt-5.6-terra", "openai-codex", 1000, 200, 5000, 0.5), turn("gpt-5.6-terra", "openai-codex", 1200, 100, 6000, 0.25), '{"type":"other"}']))
        c = self.root / "attempts/ZIP-1/002/1/session"; c.mkdir(parents=True)
        (c / "s.jsonl").write_text(turn("gpt-oss:20b", "ollama-cloud", 50000, 900, 0, 0.9))
        p = self.root / "plans/ZIP-1/20260914T000000/critic-1/session"; p.mkdir(parents=True)
        (p / "s.jsonl").write_text(turn("claude-fable-5-1", "anthropic", 10, 3000, 900000, 4.0, ts="2026-09-13T22:00:00Z"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_attempt_usage_sums_turns(self):
        u = ledger.attempt_usage(self.root / "attempts/ZIP-1/001/1")
        self.assertEqual((u["tokens_in"], u["tokens_out"], u["tokens_cache_read"], u["turns"]), (2200, 300, 11000, 2))
        self.assertAlmostEqual(u["cost_usd"], 0.75); self.assertEqual(u["usage_model"], "gpt-5.6-terra")

    def test_collect_is_idempotent_and_zeroes_free_providers(self):
        rows = ledger.collect(self.root); rows2 = ledger.collect(self.root)
        self.assertEqual(len(rows), 3); self.assertEqual(len(rows2), 3)
        cloud = next(r for r in rows if r["provider"] == "ollama-cloud")
        self.assertEqual(cloud["billed_usd"], 0.0); self.assertAlmostEqual(cloud["cost_usd"], 0.9)
        self.assertEqual({r["role"] for r in rows}, {"worker", "critic"}); self.assertEqual({r["ticket"] for r in rows}, {"ZIP-1"})

    def test_report_rolls_up_and_filters_by_day(self):
        rep = ledger.report(self.root)
        self.assertIn("billed=$4.75", rep); self.assertIn("critic", rep)
        self.assertIn("billed=$0.75", ledger.report(self.root, since_day="2026-09-14"))


if __name__ == "__main__":
    unittest.main()
