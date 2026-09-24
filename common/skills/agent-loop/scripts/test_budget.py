# test_budget.py — spend must be legible; it is deliberately NOT a gate (Cody, 2026-09-23)
import pathlib, tempfile, unittest
from unittest.mock import patch

import budget


def row(ticket="ZIP-1", model="m", stage="plan", usd=1.0, day="2026-09-22", role="author"):
    return {"ticket": ticket, "model": model, "stage": stage, "billed_usd": usd, "day": day, "role": role}


class SpendReportTests(unittest.TestCase):
    def render(self, rows, **kw):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(budget.ledger, "collect", return_value=rows):
                return budget.render(pathlib.Path(d), **kw)

    def test_totals_and_shares_per_ticket(self):
        out = self.render([row(ticket="ZIP-1", usd=75.0), row(ticket="ZIP-2", usd=25.0)], by=("ticket",))
        self.assertIn("$100.00", out)
        self.assertIn("ZIP-1", out)
        self.assertIn("75.0%", out)

    def test_grouping_by_model_and_stage(self):
        rows = [row(model="opus", stage="plan", usd=10.0), row(model="sonnet", stage="worker", usd=5.0)]
        out = self.render(rows, by=("model", "stage"))
        self.assertIn("by model", out)
        self.assertIn("by stage", out)
        self.assertIn("opus", out)
        self.assertIn("worker", out)

    def test_small_values_still_draw_a_bar(self):
        self.assertTrue(budget.bar(0.01, 100.0).strip(), "a tiny but non-zero cost must remain visible")
        self.assertEqual(budget.bar(0.0, 0.0), "")

    def test_since_filters_by_day(self):
        rows = [row(day="2026-09-01", usd=50.0), row(day="2026-09-22", usd=5.0)]
        out = self.render(rows, since="2026-09-20", by=("ticket",))
        self.assertIn("$5.00", out)
        self.assertNotIn("55.00", out)

    def test_empty_ledger_says_so_instead_of_dividing_by_zero(self):
        self.assertIn("no recorded spend", self.render([]))

    def test_per_ticket_detail_breaks_down_stage_and_model(self):
        rows = [row(ticket="ZIP-7874", stage="plan", model="opus", usd=60.0),
                row(ticket="ZIP-7874", stage="worker", model="sonnet", usd=40.0),
                row(ticket="ZIP-OTHER", usd=999.0)]
        with tempfile.TemporaryDirectory() as d:
            with patch.object(budget.ledger, "collect", return_value=rows):
                out = budget.per_ticket_detail(pathlib.Path(d), "ZIP-7874")
        self.assertIn("$100.00", out)          # the other ticket is excluded
        self.assertIn("plan", out)
        self.assertIn("sonnet", out)

    def test_reporting_never_blocks_work(self):
        """Regression on intent: budget.py must expose no gate that a dispatch path could call."""
        self.assertFalse(hasattr(budget, "check"))
        import lifecycle, runner, inspect
        for src in (inspect.getsource(lifecycle), inspect.getsource(runner)):
            self.assertNotIn("budget.check", src)


if __name__ == "__main__":
    unittest.main()
