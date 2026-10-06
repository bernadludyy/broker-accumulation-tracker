from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd

import entry_engine
import telegram_query_bot


def _frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


class FibonacciGoldenRatioTests(unittest.TestCase):
    def test_bullish_impulse_golden_pocket(self):
        dates = pd.date_range("2026-01-01", periods=20, freq="D")
        rows = []
        for i, dt in enumerate(dates):
            low = 100.0 + i
            high = 120.0 + i
            close = 110.0 + i
            rows.append(
                {
                    "date": dt,
                    "low": low,
                    "high": high,
                    "close": close,
                }
            )

        rows[0]["low"] = 100.0
        rows[-1]["high"] = 200.0
        rows[-1]["close"] = 137.0

        result = entry_engine.calculate_fibonacci_golden_ratio(
            _frame(rows),
            20,
        )

        self.assertEqual(result["direction"], "BULLISH")
        self.assertAlmostEqual(result["fib_618"], 138.2, places=6)
        self.assertAlmostEqual(result["fib_650"], 135.0, places=6)
        self.assertAlmostEqual(result["golden_low"], 135.0, places=6)
        self.assertAlmostEqual(result["golden_high"], 138.2, places=6)
        self.assertEqual(result["position"], "IN GOLDEN POCKET")
        self.assertEqual(result["distance_pct"], 0.0)

    def test_bearish_impulse_golden_pocket(self):
        dates = pd.date_range("2026-01-01", periods=20, freq="D")
        rows = []
        for i, dt in enumerate(dates):
            rows.append(
                {
                    "date": dt,
                    "low": 120.0 - i * 0.5,
                    "high": 150.0 - i * 0.5,
                    "close": 135.0,
                }
            )

        rows[0]["high"] = 200.0
        rows[-1]["low"] = 100.0
        rows[-1]["close"] = 163.0

        result = entry_engine.calculate_fibonacci_golden_ratio(
            _frame(rows),
            20,
        )

        self.assertEqual(result["direction"], "BEARISH")
        self.assertAlmostEqual(result["fib_618"], 161.8, places=6)
        self.assertAlmostEqual(result["fib_650"], 165.0, places=6)
        self.assertEqual(result["position"], "IN GOLDEN POCKET")

    @patch("telegram_query_bot.format_fibonacci_report")
    def test_ticker_first_fibo_command(self, mocked):
        mocked.return_value = "OK FIBO"

        result = telegram_query_bot.command_result(
            "ADRO FIBO 20D",
            "test-chat",
        )

        self.assertEqual(result, "OK FIBO")
        mocked.assert_called_once_with("ADRO", 20)

    @patch("telegram_query_bot.format_fibonacci_report")
    def test_fibo_first_command(self, mocked):
        mocked.return_value = "OK FIBO"

        result = telegram_query_bot.command_result(
            "FIBO ADRO 100D",
            "test-chat",
        )

        self.assertEqual(result, "OK FIBO")
        mocked.assert_called_once_with("ADRO", 100)


if __name__ == "__main__":
    unittest.main()
