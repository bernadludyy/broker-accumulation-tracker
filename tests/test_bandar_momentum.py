"""Offline regression tests for the Bot 1 Bandar Momentum command."""
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import bandar_momentum as feature
import broker_ytd_dispatch as dispatcher


def history(code: str, start: float, daily_step: float, days: int = 30):
    del code
    return [
        {
            "trade_date": f"2026-09-{index + 1:02d}",
            "close_price": start + daily_step * index,
        }
        for index in range(days)
    ]


def market_row(code: str, close: float, value: float):
    return {
        "StockCode": code,
        "Close": close,
        "Value": value,
    }


class BandarMomentumTests(unittest.TestCase):
    market_day = "2026-10-01"

    def setUp(self):
        # AAAA jumps >5% on the current day and has positive 1M momentum.
        # BBBB is deliberately flat/weak and should fail the technical filter.
        self.histories = {
            "AAAA": history("AAAA", 80, 0.5),
            "BBBB": history("BBBB", 100, -0.1),
            "CCCC": history("CCCC", 70, 0.2),
        }
        self.market = [
            market_row("AAAA", 105, 10_000_000_000),
            market_row("BBBB", 96, 20_000_000_000),
            market_row("CCCC", 82, 2_000_000_000),
        ]

    def test_requires_all_technical_and_real_broker_cache_gates(self):
        calls = []

        def broker_lookup(ticker):
            calls.append(ticker)
            return {
                "score": 75,
                "broker": "YP",
                "horizon_count": 3,
                "net_lots_20d": 1000,
                "avg_buy_20d": 98,
            }

        report = feature.analyze_bandar_momentum(
            self.market,
            self.histories,
            market_day=self.market_day,
            broker_tickers={"AAAA", "BBBB"},
            broker_lookup=broker_lookup,
        )
        self.assertEqual([row["ticker"] for row in report["rows"]], ["AAAA"])
        self.assertEqual(calls, ["AAAA"])
        self.assertEqual(report["market_count"], 3)
        self.assertEqual(report["broker_coverage"], 2)

    def test_ticker_without_broker_cache_is_never_inferred(self):
        report = feature.analyze_bandar_momentum(
            [market_row("AAAA", 105, 10_000_000_000)],
            {"AAAA": self.histories["AAAA"]},
            market_day=self.market_day,
            broker_tickers=set(),
            broker_lookup=lambda _ticker: self.fail("broker lookup must not run"),
        )
        self.assertEqual(report["rows"], [])

    def test_score_below_twenty_is_rejected(self):
        report = feature.analyze_bandar_momentum(
            [market_row("AAAA", 105, 10_000_000_000)],
            {"AAAA": self.histories["AAAA"]},
            market_day=self.market_day,
            broker_tickers={"AAAA"},
            broker_lookup=lambda _ticker: {"score": 19.9},
        )
        self.assertEqual(report["rows"], [])

    def test_eod_formatter_uses_cache_and_zero_zapi(self):
        fake_report = {
            "market_count": 1,
            "technical_count": 1,
            "broker_coverage": 1,
            "prefilter_count": 1,
            "broker_errors": 0,
            "rows": [{
                "ticker": "AAAA",
                "close": 105.0,
                "value": 10_000_000_000.0,
                "ema_gap_pct": 4.2,
                "rs_rating": 90.0,
                "rsi14": 65.0,
                "return_1d_pct": 6.1,
                "return_1m_pct": 20.0,
                "bandar": {
                    "score": 75.0,
                    "broker": "YP",
                    "horizon_count": 3,
                    "net_lots_20d": 1000,
                    "avg_buy_20d": 98.0,
                },
            }],
        }
        now = datetime(2026, 10, 1, 18, 0, tzinfo=ZoneInfo("Asia/Jakarta"))
        with patch.object(
            feature, "load_cached_market",
            return_value=(self.market[:1], self.market_day, "TEST EOD"),
        ) as cache, patch.object(
            feature, "load_shared_live_market", side_effect=AssertionError("live")
        ), patch.object(feature, "_load_price_history", return_value=self.histories), patch.object(
            feature, "_broker_tickers", return_value={"AAAA"}
        ), patch.object(feature, "analyze_bandar_momentum", return_value=fake_report):
            text = feature.format_bandar_momentum(
                "cache.db", self.market_day, mode="EOD", now=now
            )
        cache.assert_called_once()
        self.assertIn("BANDAR MOMENTUM — EOD", text)
        self.assertIn("BrokerScore 75.0", text)
        self.assertIn("ZAPI: 0 request", text)

    def test_live_is_blocked_off_hours_before_any_request(self):
        now = datetime(2026, 10, 1, 18, 0, tzinfo=ZoneInfo("Asia/Jakarta"))
        with patch.object(
            feature, "load_shared_live_market", side_effect=AssertionError("live")
        ):
            text = feature.format_bandar_momentum(
                "cache.db", self.market_day, mode="LIVE", now=now
            )
        self.assertIn("HANYA JAM BURSA", text)
        self.assertIn("Tidak ada request ZAPI", text)

    def test_command_routes_default_eod_and_live_alias(self):
        with patch.object(dispatcher, "format_bandar_momentum", return_value="OK") as formatter, patch.object(
            dispatcher.legacy, "safe_end_date", return_value="2026-10-01"
        ):
            self.assertEqual(dispatcher.command_result("BANDAR MOMENTUM", "1"), "OK")
            formatter.assert_called_with(
                str(dispatcher.legacy.DB_PATH), "2026-10-01", mode="EOD", limit=10
            )
            self.assertEqual(
                dispatcher.command_result("/bandar_momentum LIVE 5", "1"), "OK"
            )
            formatter.assert_called_with(
                str(dispatcher.legacy.DB_PATH), "2026-10-01", mode="LIVE", limit=5
            )
        self.assertIn(
            "Format:", dispatcher.command_result("BANDAR MOMENTUM LIVE EOD", "1")
        )
        self.assertIn("BANDAR MOMENTUM", dispatcher.command_result("/help", "1"))


if __name__ == "__main__":
    unittest.main()
