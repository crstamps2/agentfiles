# test_budget.py — the configured spend ceilings must actually stop spending
import pathlib, tempfile, unittest
from unittest.mock import patch

import budget, config, heal


class FakeBudgets:
    premium_daily_usd = 5.0
    premium_monthly_usd = 40.0
    total_unattended_daily_usd = 15.0


class FakeCfg:
    def __init__(self, root): self.state_root = root; self.budgets = FakeBudgets()


class BudgetAdmissionTests(unittest.TestCase):
    """Branch review 2026-09-23: premium_daily_usd, premium_monthly_usd and
    total_unattended_daily_usd were configured, documented and never read by any production path."""

    def rows(self, *rows): return list(rows)

    def test_under_every_ceiling_admits(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = FakeCfg(pathlib.Path(d))
            with patch.object(budget.ledger, "collect", return_value=self.rows(
                    {"day": budget._today(), "role": "premium", "billed_usd": 1.0})):
                self.assertEqual(budget.check(cfg, premium=True), "")

    def test_premium_daily_ceiling_stops_premium_but_not_cheap(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = FakeCfg(pathlib.Path(d))
            rows = self.rows({"day": budget._today(), "role": "premium", "billed_usd": 6.0})
            with patch.object(budget.ledger, "collect", return_value=rows):
                self.assertIn("premium daily budget", budget.check(cfg, premium=True))
                self.assertEqual(budget.check(cfg, premium=False), "")   # 6 < 15 total

    def test_total_daily_ceiling_stops_everything(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = FakeCfg(pathlib.Path(d))
            rows = self.rows({"day": budget._today(), "role": "worker", "billed_usd": 16.0})
            with patch.object(budget.ledger, "collect", return_value=rows):
                self.assertIn("daily budget reached", budget.check(cfg, premium=False))

    def test_premium_monthly_ceiling_counts_the_month(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = FakeCfg(pathlib.Path(d))
            rows = self.rows({"day": budget._month() + "-01", "role": "author", "billed_usd": 41.0})
            with patch.object(budget.ledger, "collect", return_value=rows):
                self.assertIn("premium monthly budget", budget.check(cfg, premium=True))

    def test_a_budget_stop_is_an_owner_decision_never_a_retry(self):
        self.assertEqual(heal.policy("budget: premium daily budget reached: $6.00")[0], "human")

    def test_the_real_config_exposes_all_three_ceilings(self):
        cfg = config.load()
        for k in ("premium_daily_usd", "premium_monthly_usd", "total_unattended_daily_usd"):
            self.assertTrue(getattr(cfg.budgets, k) > 0, k)

    def test_every_paid_dispatch_consults_the_budget(self):
        """Plan/critique, review adjudication and worker attempts are the three paid entry points."""
        import inspect, lifecycle, runner
        life = inspect.getsource(lifecycle.step)
        self.assertEqual(life.count("budget.check(cfg, premium=True)"), 2)   # plan stage + adjudication
        self.assertIn("budget.check(self.cfg", inspect.getsource(runner.Runner._attempt))


if __name__ == "__main__":
    unittest.main()
