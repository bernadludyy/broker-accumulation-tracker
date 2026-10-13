import unittest

import pandas as pd

from project_fabric.bot1_rank import (
    combined_score,
    foreign_snapshot,
    format_bot1_rank,
    universe_snapshot,
)


class Bot1RankTests(unittest.TestCase):
    def test_foreign_gate_requires_all_three_conditions(self):
        frame = pd.DataFrame({
            "foreign_buy": [10, 20, 30, 40, 50],
            "foreign_sell": [5, 25, 20, 35, 40],
        })
        result = foreign_snapshot(frame)
        self.assertTrue(result["passed"])
        self.assertEqual(result["positive_days"], 4)
        self.assertGreater(result["net_5d"], 0)

    def test_foreign_missing_value_fails_closed(self):
        frame = pd.DataFrame({
            "foreign_buy": [10, 20, None, 40, 50],
            "foreign_sell": [5, 10, 20, 30, 40],
        })
        result = foreign_snapshot(frame)
        self.assertIsNone(result["score"])
        self.assertFalse(result["passed"])

    def test_universe_inverts_distribution_risk(self):
        low_risk = universe_snapshot({
            "accumulation_persistence": {"score": 70},
            "breakout_readiness": {"score": 70},
            "institutional_flow": {"score": 70, "source": "reported_foreign_value"},
            "distribution_risk": {"score": 10},
            "entry_quality": {"score": 70},
        })
        high_risk = universe_snapshot({
            "accumulation_persistence": {"score": 70},
            "breakout_readiness": {"score": 70},
            "institutional_flow": {"score": 70, "source": "reported_foreign_value"},
            "distribution_risk": {"score": 90},
            "entry_quality": {"score": 70},
        })
        self.assertGreater(low_risk["score"], high_risk["score"])
        self.assertTrue(high_risk["risk_high"])

    def test_combined_score_renormalizes_missing_universe(self):
        self.assertEqual(combined_score(80, 80, 80, None), 80.0)

    def test_compact_output_explains_refresh_sequence(self):
        result = {
            "as_of": "2026-10-13", "eligible": 1, "skipped": 0,
            "rows": [{
                "ticker": "TEST", "score": 81.2, "status": "SIAP VALIDASI BROKER",
                "fundamental": {"score": 75.0},
                "technical": {"score": 82.0, "trend": "BULLISH", "selected_rr": 1.8,
                              "selected_plan": "PULLBACK", "entry_low": 100,
                              "entry_high": 105, "invalidation": 95, "tp1": 120},
                "foreign": {"score": 94.0, "net_5d": 2_000_000_000, "positive_days": 4},
                "universe": {"score": 69.0},
            }],
        }
        text = format_bot1_rank(result=result)
        self.assertIn("SETUP RANK", text)
        self.assertIn("0 ZAPI", text)
        self.assertIn("/refresh TICKER", text)
        self.assertIn("BOT1 FULL TICKER", text)
        self.assertIn("Broker belum dicek", text)


if __name__ == "__main__":
    unittest.main()
