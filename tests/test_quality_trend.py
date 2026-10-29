import unittest
from datetime import date, timedelta
import importlib.util
from unittest.mock import patch

from market_regime import detect_regime
import quality_trend as feature


def rising_history():
    rows = []
    start = date(2025, 1, 1)
    for index in range(220):
        close = 100.0 + index * 0.5
        rows.append({
            "ticker": "TEST",
            "trade_date": (start + timedelta(days=index)).isoformat(),
            "open_price": close - 0.3,
            "high_price": close + 1.0,
            "low_price": close - 1.0,
            "close_price": close,
            "volume_shares": 1_000_000 + index * 1_000,
            "turnover_idr": 500_000_000,
            "foreign_buy_value_idr": 2_000_000,
            "foreign_sell_value_idr": 1_000_000,
        })
    return rows


def statements():
    return [
        {
            "period_kind": "quarterly", "timeframe": "Q1 2025",
            "revenue": 100, "net_profit_loss": 10,
            "operating_cash_flow": 12,
        },
        {
            "period_kind": "quarterly", "timeframe": "Q1 2026",
            "revenue": 120, "net_profit_loss": 15,
            "operating_cash_flow": 20,
        },
    ]


class QualityTrendTests(unittest.TestCase):
    def test_all_image_rules_pass_with_rising_quality_stock(self):
        history = rising_history()
        latest = dict(history.pop())
        latest["raw_json"] = {"MarketCap": 5_000_000_000_000}
        fundamental = {
            "pe": 10, "pb": 1.5, "roe_pct": 15, "roa_pct": 8,
            "net_margin_pct": 12,
            "raw_fundamental": {"piotroski_f_score": 7},
        }
        result = feature.analyze_quality_trend(
            latest, history, fundamental, statements(), 80.0
        )
        self.assertIsNotNone(result)
        self.assertTrue(all(result["core"].values()))
        self.assertGreater(result["ma20"], result["ma50"])
        self.assertGreater(result["ma50"], result["ma200"])
        self.assertEqual(result["quality"]["source"], "PIOTROSKI")

    def test_quality_proxy_is_transparent_and_requires_full_coverage(self):
        fundamental = {"roe_pct": 12, "roa_pct": 7, "net_margin_pct": 9}
        complete = feature.quality_score(fundamental, statements())
        self.assertEqual(complete["source"], "F-PROXY 9 CHECK")
        self.assertEqual(complete["coverage"], 9)
        self.assertGreater(complete["score"], 5)
        incomplete = feature.quality_score(fundamental, [])
        self.assertIsNone(incomplete["score"])

    def test_market_cap_rank_is_larger_for_larger_company(self):
        rows = [
            {"ticker": "A", "raw_json": {"MarketCap": 2_000}},
            {"ticker": "B", "raw_json": {"MarketCap": 5_000}},
            {"ticker": "C", "raw_json": {"MarketCap": 10_000}},
        ]
        ranks = feature.market_cap_percentiles(rows)
        self.assertLess(ranks["A"], ranks["B"])
        self.assertLess(ranks["B"], ranks["C"])

    def test_output_obeys_cash_market_gate(self):
        regime = detect_regime([
            {"Close": 98, "OpenPrice": 100, "ForeignBuy": 100, "ForeignSell": 200}
            for _ in range(120)
        ])
        row = {
            "ticker": "TEST", "score": 93, "confirmation_count": 4,
            "confirmations": {"macd": True}, "close": 200,
            "ma20": 190, "ma50": 180, "ma200": 150,
            "quality": {"score": 7, "source": "PIOTROSKI"},
            "pe": 10, "pb": 1.5, "market_cap": 5_000_000_000_000,
            "market_cap_percentile": 80, "accumulation": 75,
            "rsi": 60, "volume_ratio": 1.2, "return_1d": 1.0,
            "value_ma5": 500_000_000, "foreign_5d": 1_000_000,
            "entry": 200, "invalidation": 185, "target": 230, "rr": 2,
        }
        text = feature.format_quality_trend(result={
            "as_of": "2026-10-29", "eligible": 1, "rows": [row],
            "regime": regime,
        })
        self.assertIn("WATCH ONLY — MARKET CASH", text)
        self.assertIn("Accum Proxy", text)
        self.assertIn("0 ZAPI", text)

    @unittest.skipUnless(importlib.util.find_spec("requests"), "requests not installed")
    @patch("broker_ytd_dispatch.format_quality_trend", return_value="RATIO_OK")
    def test_dispatch_aliases_and_ticker_detail(self, formatter):
        import broker_ytd_dispatch as dispatch

        self.assertEqual(dispatch.command_result("RATIO TREND", "123"), "RATIO_OK")
        formatter.assert_called_with(limit=10)
        self.assertEqual(dispatch.command_result("/qualitytrend BBRI", "123"), "RATIO_OK")
        formatter.assert_called_with(ticker="BBRI", limit=1)


if __name__ == "__main__":
    unittest.main()
