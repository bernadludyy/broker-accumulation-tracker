import unittest
from datetime import date, timedelta
from unittest.mock import patch

import pandas as pd

import bull_divergence as feature


def divergence_history(second_index=54, total=60):
    rows = []
    for index in range(total):
        if index <= 34:
            close = 120 - (18 * index / 34)
        elif index <= 44:
            close = 102 + (8 * (index - 34) / 10)
        elif index <= second_index:
            close = 110 - (10 * (index - 44) / max(1, second_index - 44))
        else:
            close = 100 + (2 * (index - second_index))
        low = close - 1
        high = close + 2
        if index == 34:
            close, low, high = 102.0, 100.0, 104.0
        if index == second_index:
            close, low, high = 100.0, 98.0, 102.0
        rows.append({
            "trade_date": (date(2026, 7, 1) + timedelta(days=index)).isoformat(),
            "low_price": low,
            "high_price": high,
            "close_price": close,
            "volume_shares": 1_000_000,
            "foreign_buy_value_idr": 2_000_000,
            "foreign_sell_value_idr": 1_000_000,
        })
    return rows


def rsi_values(length, second_index, latest=42.0):
    values = [30.0] * length
    values[34] = 25.0
    values[second_index] = 36.0
    values[-1] = latest
    return pd.Series(values)


class BullDivergenceTests(unittest.TestCase):
    def test_regular_divergence_is_detected_from_adjacent_pivots(self):
        history = divergence_history()
        with patch.object(feature, "_rsi14", return_value=rsi_values(len(history), 54)):
            result = feature.analyze_divergence(
                "TEST", history,
                {"close_price": 110, "turnover_idr": 5_000_000_000},
            )
        self.assertIsNotNone(result)
        self.assertEqual(result["ticker"], "TEST")
        self.assertGreaterEqual(result["rsi_gap"], 3)

    def test_intervening_lower_low_rejects_old_coin_style_pair(self):
        history = divergence_history()
        # A deeper valley exists between the two endpoints. The old detector
        # skipped this pivot and paired index 34 directly with index 54.
        history[45].update(low_price=79.0, close_price=80.0, high_price=83.0)
        values = rsi_values(len(history), 54)
        values.iloc[45] = 20.0
        with patch.object(feature, "_rsi14", return_value=values):
            result = feature.analyze_divergence(
                "COIN", history,
                {"close_price": 110, "turnover_idr": 5_000_000_000},
            )
        self.assertIsNone(result)

    def test_latest_provisional_pivot_is_early(self):
        history = divergence_history(second_index=59)
        values = rsi_values(len(history), 59, latest=36.0)
        with patch.object(feature, "_rsi14", return_value=values):
            result = feature.analyze_divergence(
                "EARLY", history,
                {"close_price": 100, "turnover_idr": 5_000_000_000},
            )
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "EARLY")
        self.assertFalse(result["pivot_confirmed"])

    def test_one_right_bar_reversal_is_validated_without_waiting_for_full_pivot(self):
        history = divergence_history(second_index=58)
        history[59].update(low_price=99.0, close_price=101.0, high_price=102.0)
        values = rsi_values(len(history), 58, latest=40.0)
        with (
            patch.object(feature, "_rsi14", return_value=values),
            patch.object(feature, "MAX_EXTENSION_PCT", 10.0),
            patch.object(feature, "MAX_EXTENSION_ATR", 10.0),
        ):
            result = feature.analyze_divergence(
                "VALID", history,
                {"close_price": 101, "turnover_idr": 5_000_000_000},
            )
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "VALIDATED")
        self.assertFalse(result["pivot_confirmed"])

    def test_two_right_bars_and_break_high_is_confirmed(self):
        history = divergence_history(second_index=57)
        history[58].update(low_price=99.0, close_price=100.0, high_price=101.0)
        history[59].update(low_price=100.0, close_price=103.0, high_price=104.0)
        values = rsi_values(len(history), 57, latest=42.0)
        with (
            patch.object(feature, "_rsi14", return_value=values),
            patch.object(feature, "MAX_EXTENSION_PCT", 10.0),
            patch.object(feature, "MAX_EXTENSION_ATR", 10.0),
        ):
            result = feature.analyze_divergence(
                "CONF", history,
                {"close_price": 103, "turnover_idr": 5_000_000_000},
            )
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "CONFIRMED")
        self.assertTrue(result["pivot_confirmed"])

    def test_extended_move_is_labeled_late_instead_of_entry(self):
        history = divergence_history()
        values = rsi_values(len(history), 54)
        with patch.object(feature, "_rsi14", return_value=values):
            result = feature.analyze_divergence(
                "LATE", history,
                {"close_price": 110, "turnover_idr": 5_000_000_000},
            )
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "LATE/EXTENDED")
        self.assertGreater(result["extension_pct"], 5)

    def test_same_direction_price_and_rsi_is_rejected(self):
        history = divergence_history()
        values = [40.0] * len(history)
        values[34] = 35.0
        values[54] = 30.0
        with patch.object(feature, "_rsi14", return_value=pd.Series(values)):
            result = feature.analyze_divergence(
                "TEST", history,
                {"close_price": 108, "turnover_idr": 5_000_000_000},
            )
        self.assertIsNone(result)

    def test_detail_uses_full_market_rows_for_regime_gate(self):
        history = divergence_history()
        summaries = [{
            "ticker": "TEST", "trade_date": "2026-08-29",
            "close_price": 108, "high_price": 109, "low_price": 106,
            "volume_shares": 1_000_000, "turnover_idr": 5_000_000_000,
        }]
        regime_rows = [
            {"Close": 98, "OpenPrice": 100, "ForeignBuy": 100, "ForeignSell": 200}
            for _ in range(150)
        ]
        with patch.object(
            feature, "_load_snapshot",
            return_value=("2026-08-29", summaries, {"TEST": history}, regime_rows),
        ), patch.object(feature, "analyze_divergence", return_value=None):
            report = feature.format_bull_divergence(ticker="TEST", limit=1)
        self.assertIn("Market Gate: BEARISH → CASH", report)


if __name__ == "__main__":
    unittest.main()
