import unittest

import pandas as pd

from project_fabric.bot1_rank import (
    combined_score,
    format_bot1_rank,
    smart_money_from_history,
    smart_money_snapshot,
)


class Bot1RankTests(unittest.TestCase):
    def test_smart_money_excludes_foreign_and_inverts_distribution(self):
        result = smart_money_snapshot({
            "accumulation_persistence": {"score": 70},
            "breakout_readiness": {"score": 65},
            "distribution_risk": {"score": 20},
            "entry_quality": {"score": 75},
            "institutional_flow": {"score": 0, "source": "reported_foreign_value"},
        })
        self.assertTrue(result["passed"])
        self.assertGreaterEqual(result["score"], 60)
        self.assertEqual(result["coverage"], 4)

    def test_high_distribution_fails_smart_money_gate(self):
        result = smart_money_snapshot({
            "accumulation_persistence": {"score": 80},
            "breakout_readiness": {"score": 80},
            "distribution_risk": {"score": 90},
            "entry_quality": {"score": 80},
        })
        self.assertTrue(result["risk_high"])
        self.assertFalse(result["passed"])

    def test_missing_accumulation_fails_closed(self):
        result = smart_money_snapshot({
            "breakout_readiness": {"score": 80},
            "distribution_risk": {"score": 10},
            "entry_quality": {"score": 80},
        })
        self.assertIsNotNone(result["score"])
        self.assertFalse(result["passed"])

    def test_combined_score_uses_35_35_30_weight(self):
        self.assertEqual(combined_score(80, 70, 90), 79.5)

    def test_current_eod_bar_is_included_in_smart_money_proxy(self):
        dates = pd.date_range(end="2026-10-13", periods=40, freq="D")
        frame = pd.DataFrame({
            "date": dates,
            "open": [100 + i for i in range(40)],
            "high": [102 + i for i in range(40)],
            "low": [99 + i for i in range(40)],
            "close": [101 + i for i in range(40)],
            "volume": [1_000_000 + i * 10_000 for i in range(40)],
        })
        result = smart_money_from_history(frame, "TEST", "2026-10-13")
        self.assertEqual(result["history_last_date"], "2026-10-13")
        self.assertEqual(result["coverage"], 4)

    def test_compact_output_explains_refresh_sequence(self):
        result = {
            "as_of": "2026-10-13", "eligible": 1, "skipped": 0,
            "rows": [{
                "ticker": "TEST", "score": 81.2, "status": "SIAP VALIDASI BROKER",
                "fundamental": {"score": 75.0},
                "technical": {"score": 82.0, "trend": "BULLISH", "selected_rr": 1.8,
                              "selected_plan": "PULLBACK", "entry_low": 100,
                              "entry_high": 105, "invalidation": 95, "tp1": 120},
                "smart_money": {"score": 86.0, "accumulation_score": 80.0,
                                "breakout_score": 70.0, "distribution_score": 20.0},
            }],
        }
        text = format_bot1_rank(result=result)
        self.assertIn("SETUP RANK", text)
        self.assertIn("Smart Money 30", text)
        self.assertNotIn("Foreign 5D", text)
        self.assertIn("0 ZAPI", text)
        self.assertIn("/refresh TICKER", text)
        self.assertIn("BOT1 FULL TICKER", text)
        self.assertIn("Broker belum dicek", text)


if __name__ == "__main__":
    unittest.main()
