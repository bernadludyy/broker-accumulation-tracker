import unittest
from unittest.mock import patch

import pandas as pd

import foreign_entry as feature
import entry_engine


def history(open_price=100):
    n = 60
    return pd.DataFrame({
        "date": pd.bdate_range(end="2026-10-01", periods=n),
        "open": [open_price] * n,
        "high": [102] * n,
        "low": [99] * n,
        "close": [101] * n,
        "volume": [1000] * n,
        "foreignBuyValue": [3_000_000] * n,
        "foreignSellValue": [1_000_000] * n,
    })


def entry():
    return {
        "score": 75, "filter_status": "PASS", "filters": [],
        "selected_rr": 2, "atr14": 2, "close": 101,
        "selected_plan": "PULLBACK", "entry1_low": 99,
        "entry1_high": 101, "invalidation": 97,
        "pullback_tp1": 106, "pullback_tp2": 110,
    }


class ZeroOpenRegressionTests(unittest.TestCase):
    def evaluate(self, frame):
        with patch.object(feature, "load_stock_history", return_value=frame), \
             patch.object(feature, "build_entry_score", return_value=entry()) as build, \
             patch.object(feature, "evaluate_universe", return_value={"score": 50}), \
             patch.object(feature, "priority_score", return_value=60), \
             patch.object(feature.tracker, "get_safe_end_date", return_value="2026-10-01"):
            return feature.evaluate_foreign_entry("PACK"), build

    def test_zero_open_does_not_hide_foreign_or_setup(self):
        result, build = self.evaluate(history(open_price=0))
        self.assertTrue(result["passed"])
        self.assertTrue(result["foreign"]["confirmed"])
        self.assertIsNotNone(result["entry"])
        self.assertIn("Open tidak tersedia pada 60/60 candle", result["warnings"][0])
        build.assert_called_once()

    def test_invalid_required_hlcv_still_fails_closed_but_keeps_foreign(self):
        frame = history(open_price=0)
        frame.loc[frame.index[-1], "volume"] = float("nan")
        result, build = self.evaluate(frame)
        self.assertFalse(result["passed"])
        self.assertTrue(result["foreign"]["confirmed"])
        self.assertIn("HLCV tidak valid", result["reasons"][-1])
        build.assert_not_called()

    def test_provider_zero_range_non_trading_rows_are_filtered(self):
        valid = history(open_price=0).to_dict("records")
        no_trade = [{
            "date": "2026-09-16", "open": 0, "high": 0, "low": 0,
            "close": 560, "volume": 0,
        }]
        with patch.object(entry_engine.tracker, "fetch_stock_history",
                          return_value={"items": no_trade + valid}):
            result = entry_engine.load_stock_history("PACK", 100)
        self.assertEqual(len(result), 60)
        self.assertTrue((result[["high", "low", "close"]] > 0).all().all())


if __name__ == "__main__":
    unittest.main()
