"""Offline regression tests for the Bot 1 Silent Accum command."""
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import broker_ytd_dispatch as dispatcher
import silent_accum as feature


def history(*, quiet: bool = True):
    start = date(2026, 7, 8)
    rows = []
    for index in range(61):
        value = 3_000_000_000 if quiet and index >= 56 else 6_000_000_000
        rows.append(
            {
                "trade_date": (start + timedelta(days=index)).isoformat(),
                "open_price": 102,
                "high_price": 106,
                "low_price": 100,
                "close_price": 103,
                "turnover_idr": value,
            }
        )
    return rows


def market_row(code="AAAA", close=104, value=3_000_000_000):
    return {
        "StockCode": code,
        "Open": 103,
        "High": 106,
        "Low": 101,
        "Close": close,
        "Value": value,
    }


def broker(buy_days=4):
    return {
        "broker": "YP",
        "net_value": 2_000_000_000,
        "buy_days": buy_days,
        "sell_days": 1,
        "observed_days": 5,
        "avg_buy_price": 102,
    }


class SilentAccumTests(unittest.TestCase):
    market_day = "2026-09-07"

    def test_requires_bottom_quiet_value_and_persistent_broker(self):
        result = feature.analyze_silent_accumulation(
            [market_row()],
            {"AAAA": history()},
            {"AAAA": [broker()]},
            market_day=self.market_day,
        )
        self.assertEqual([row["ticker"] for row in result["rows"]], ["AAAA"])
        self.assertEqual(result["rows"][0]["status"], "AKUMULASI TERKONFIRMASI")
        self.assertGreater(result["rows"][0]["score"], 0)

    def test_rejects_active_value_or_missing_broker(self):
        active = feature.analyze_silent_accumulation(
            [market_row()],
            {"AAAA": history(quiet=False)},
            {"AAAA": [broker()]},
            market_day=self.market_day,
        )
        missing = feature.analyze_silent_accumulation(
            [market_row()],
            {"AAAA": history()},
            {},
            market_day=self.market_day,
        )
        self.assertEqual(active["rows"], [])
        self.assertIn("value 5 sesi", active["diagnostics"][0]["reasons"][0])
        self.assertEqual(missing["rows"], [])
        self.assertIn("broker net buy", missing["diagnostics"][0]["reasons"][0])

    def test_ticker_detail_explains_failure(self):
        result = feature.analyze_silent_accumulation(
            [market_row("AAAA", close=130)],
            {"AAAA": history()},
            {"AAAA": [broker()]},
            market_day=self.market_day,
            ticker="AAAA",
        )
        self.assertEqual(result["rows"], [])
        self.assertEqual(len(result["diagnostics"]), 1)
        self.assertIn("low 60 sesi", " ".join(result["diagnostics"][0]["reasons"]))

    def test_ticker_detail_without_persistent_broker_is_safe(self):
        result = feature.analyze_silent_accumulation(
            [market_row()],
            {"AAAA": history()},
            {},
            market_day=self.market_day,
            ticker="AAAA",
        )
        text = "\n".join(feature._format_candidate(result["diagnostics"][0]))
        self.assertIn("Skor N/A", text)
        self.assertIn("BELUM LOLOS", text)

    def test_formatter_uses_eod_cache_and_zero_zapi(self):
        now = datetime(2026, 9, 7, 18, 0, tzinfo=ZoneInfo("Asia/Jakarta"))
        with patch.object(
            feature,
            "load_cached_market",
            return_value=([market_row()], self.market_day, "TEST EOD"),
        ), patch.object(
            feature, "_load_price_history", return_value={"AAAA": history()}
        ), patch.object(
            feature,
            "_load_broker_accumulation",
            return_value={
                "dates": ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04", self.market_day],
                "by_ticker": {"AAAA": [broker()]},
                "invalid_rows": 0,
            },
        ):
            text = feature.format_silent_accum("cache.db", self.market_day, now=now)
        self.assertIn("SILENT ACCUM — EOD", text)
        self.assertIn("AAAA", text)
        self.assertIn("ZAPI: 0 request", text)

    def test_command_routes_scan_limit_and_ticker(self):
        with patch.object(dispatcher, "format_silent_accum", return_value="OK") as formatter, patch.object(
            dispatcher.legacy, "safe_end_date", return_value=self.market_day
        ):
            self.assertEqual(dispatcher.command_result("SILENT ACCUM", "1"), "OK")
            formatter.assert_called_with(
                str(dispatcher.legacy.DB_PATH), self.market_day, limit=10
            )
            self.assertEqual(dispatcher.command_result("/silent_accum 5", "1"), "OK")
            formatter.assert_called_with(
                str(dispatcher.legacy.DB_PATH), self.market_day, limit=5
            )
            self.assertEqual(dispatcher.command_result("SILENT ACCUM BBRI", "1"), "OK")
            formatter.assert_called_with(
                str(dispatcher.legacy.DB_PATH), self.market_day, ticker="BBRI", limit=1
            )
        self.assertIn("SILENT ACCUM", dispatcher.command_result("/help", "1"))
        self.assertIn("Format:", dispatcher.command_result("SILENT ACCUM 20", "1"))


if __name__ == "__main__":
    unittest.main()
