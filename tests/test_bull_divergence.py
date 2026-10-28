import unittest
from datetime import date, timedelta
from unittest.mock import patch

import bull_divergence as feature


def divergence_history():
    rows = []
    for index in range(60):
        if index <= 34:
            close = 120 - (18 * index / 34)
        elif index <= 41:
            close = 102 + (8 * (index - 34) / 7)
        elif index <= 54:
            close = 110 - (10 * (index - 41) / 13)
        else:
            close = 100 + (8 * (index - 54) / 5)
        low = close - 1
        if index == 34:
            close, low = 102.0, 100.0
        if index == 54:
            close, low = 100.0, 98.0
        rows.append({
            "trade_date": (date(2026, 7, 1) + timedelta(days=index)).isoformat(),
            "low_price": low,
            "high_price": close + 2,
            "close_price": close,
            "volume_shares": 1_000_000,
            "foreign_buy_value_idr": 2_000_000,
            "foreign_sell_value_idr": 1_000_000,
        })
    return rows


class BullDivergenceTests(unittest.TestCase):
    def test_regular_divergence_is_detected_from_pivots(self):
        history = divergence_history()
        # Force a clear RSI higher low while preserving the price lower low.
        with patch.object(feature, "_rsi14") as rsi:
            values = [30.0] * len(history)
            values[34] = 25.0
            values[54] = 36.0
            values[-1] = 42.0
            import pandas as pd
            rsi.return_value = pd.Series(values)
            result = feature.analyze_divergence(
                "TEST", history,
                {"close_price": 108, "turnover_idr": 5_000_000_000},
            )
        self.assertIsNotNone(result)
        self.assertEqual(result["ticker"], "TEST")
        self.assertGreaterEqual(result["rsi_gap"], 3)

    def test_same_direction_price_and_rsi_is_rejected(self):
        history = divergence_history()
        with patch.object(feature, "_rsi14") as rsi:
            import pandas as pd
            values = [40.0] * len(history)
            values[34] = 35.0
            values[54] = 30.0
            rsi.return_value = pd.Series(values)
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
