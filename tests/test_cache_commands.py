import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import cache_commands as cc
import telegram_query_bot as tq


WIB = ZoneInfo("Asia/Jakarta")


def market_rows():
    return [
        {
            "ticker": "AAAA",
            "StockCode": "AAAA",
            "Date": "2026-10-12T00:00:00",
            "OpenPrice": 102,
            "Close": 100,
            "Value": 10_000_000_000,
            "Frequency": 10_000,
            "ListedShares": 1_000_000_000,
            "ForeignBuy": 10_000_000,
            "ForeignSell": 2_000_000,
        },
        {
            "ticker": "BBBB",
            "StockCode": "BBBB",
            "Date": "2026-10-12T00:00:00",
            "OpenPrice": 1020,
            "Close": 1000,
            "Value": 20_000_000_000,
            "Frequency": 20_000,
            "ListedShares": 100_000_000,
            "ForeignBuy": 5_000_000,
            "ForeignSell": 1_000_000,
        },
        {
            "ticker": "CCCC",
            "StockCode": "CCCC",
            "Date": "2026-10-12T00:00:00",
            "OpenPrice": 50,
            "Close": 55,
            "Value": 2_000_000_000,
            "Frequency": 1_000,
            "ListedShares": 500_000_000,
            "ForeignBuy": 500_000,
            "ForeignSell": 600_000,
        },
    ]


class FakeCursor:
    def __init__(self, batches):
        self.batches = list(batches)
        self.current = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, _sql, _params=None):
        self.current = self.batches.pop(0) if self.batches else []

    def fetchall(self):
        return list(self.current)

    def fetchone(self):
        return self.current[0] if self.current else None


class FakeConnection:
    def __init__(self, batches):
        self.cursor_obj = FakeCursor(batches)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return self.cursor_obj


class CacheCommandTests(unittest.TestCase):
    def test_offhours_prefers_newer_shared_cache_over_older_local(self):
        now = datetime(2026, 10, 12, 19, 0, tzinfo=WIB)
        shared = {"data": market_rows()}
        friday = [dict(market_rows()[0], Date="2026-10-09T00:00:00")]

        with patch.object(
            cc.tracker,
            "latest_stock_summary_snapshot_date",
            return_value="2026-10-09",
        ), patch.object(
            cc.tracker,
            "load_stock_summary_snapshot",
            return_value=friday,
        ), patch.object(
            cc.tracker,
            "peek_shared_stock_summary",
            return_value=shared,
        ):
            rows, day, source = cc.load_cached_market(now=now)

        self.assertEqual(day, "2026-10-12")
        self.assertEqual(len(rows), 3)
        self.assertEqual(source, "PROJECT FABRIC SHARED CACHE")

    def test_top_value_turnover_and_foreign_dip_are_cache_only_views(self):
        cached = (market_rows(), "2026-10-12", "TEST CACHE")
        with patch.object(cc, "load_cached_market", return_value=cached):
            top_value = cc.format_top_value(2)
            turnover = cc.format_turnover(2)
            dip = cc.format_foreign_dip(20)

        self.assertIn("1. BBBB", top_value)
        self.assertIn("Rp 20,00 Miliar", top_value)

        self.assertIn("1. BBBB", turnover)
        self.assertIn("Turnover   : 20,00%", turnover)
        self.assertIn("2. AAAA", turnover)

        self.assertIn("FOREIGN DIP", dip)
        self.assertIn("BBBB", dip)
        self.assertIn("AAAA", dip)
        self.assertNotIn("CCCC", dip)
        self.assertIn("Rp +4,00 Miliar*", dip)

    def test_breakout_ready_reads_cached_intelligence(self):
        rows = [
            (
                "AAAA", "2026-10-09", 91.5, "OBSERVED",
                1.2, 0.65, 84.0, 12.0,
            ),
            (
                "BBBB", "2026-10-09", 88.0, "OBSERVED",
                2.0, 0.75, 80.0, 25.0,
            ),
        ]
        with patch.object(cc, "_connect", return_value=FakeConnection([rows])):
            text = cc.format_breakout_ready(20)

        self.assertIn("BREAKOUT READY — TOP 2", text)
        self.assertIn("History EOD: 2026-10-09", text)
        self.assertIn("Status: FRESH EOD ✅", text)
        self.assertIn("AAAA", text)
        self.assertIn("91.5/100", text)
        self.assertIn("Distribution Risk  : 12.0/100", text)

    def test_dynamic_liquidity_reads_bot4_observation_events(self):
        occurred = datetime(2026, 10, 12, 14, 30, tzinfo=WIB)
        rows = [
            (
                "TEST", occurred, "FILTER1_CORE", 400_000_000_000,
                1_600_000_000, 1_000_000_000, 0.4,
                "PRE_MOMENTUM", 76.0,
            )
        ]
        with patch.object(cc, "_connect", return_value=FakeConnection([rows])):
            text = cc.format_dynamic_liquidity(20)

        self.assertIn("DYNAMIC LIQUIDITY MATCH — TOP 1", text)
        self.assertIn("Market Cap : Rp 400,00 Miliar", text)
        self.assertIn("Value MA50 : Rp 1,60 Miliar", text)
        self.assertIn("MA50/MCap  : 0,40%", text)
        self.assertIn("14:30 WIB", text)

    def test_cache_status_never_calls_upstream(self):
        local = [dict(market_rows()[0])]
        shared = {"data": market_rows()}
        cache_saved = datetime(2026, 10, 12, 15, 45, tzinfo=WIB)
        intel_saved = datetime(2026, 10, 12, 15, 46, tzinfo=WIB)
        eod_saved = datetime(2026, 10, 12, 17, 5, tzinfo=WIB)
        fake = FakeConnection([
            [(cache_saved, "cuankus-momentum")],
            [(intel_saved, 963)],
            [(7,)],
            [("SUCCESS", "2026-10-12", 84, 0, eod_saved)],
        ])
        with patch.object(
            cc.tracker,
            "latest_stock_summary_snapshot_date",
            return_value="2026-10-12",
        ), patch.object(
            cc.tracker,
            "load_stock_summary_snapshot",
            return_value=local,
        ), patch.object(
            cc.tracker,
            "peek_shared_stock_summary",
            return_value=shared,
        ), patch.object(
            cc,
            "_connect",
            return_value=fake,
        ):
            text = cc.format_cache_status()

        self.assertIn("CACHE STATUS", text)
        self.assertIn("2026-10-12", text)
        self.assertIn("Tickers   : 963", text)
        self.assertIn("Event hari ini : 7", text)
        self.assertIn("EOD FINALIZER 17:00 WIB", text)
        self.assertIn("Trade Day : 2026-10-12", text)
        self.assertIn("Finalized : 84 ticker", text)
        self.assertIn("ZAPI Call : 0", text)
        self.assertIn("tidak memanggil ZAPI", text)

    def test_telegram_parser_routes_six_priority_commands(self):
        with patch.object(tq, "format_cache_status", return_value="CACHE") as cache, \
             patch.object(tq, "format_top_value", return_value="VALUE") as value, \
             patch.object(tq, "format_turnover", return_value="TURN") as turnover, \
             patch.object(tq, "format_foreign_dip", return_value="DIP") as dip, \
             patch.object(tq, "format_breakout_ready", return_value="BREAK") as breakout, \
             patch.object(tq, "format_dynamic_liquidity", return_value="DYNAMIC") as dynamic:
            self.assertEqual(tq.command_result("CACHE STATUS", "1"), "CACHE")
            self.assertEqual(tq.command_result("TOP VALUE 20", "1"), "VALUE")
            self.assertEqual(tq.command_result("TURNOVER 20", "1"), "TURN")
            self.assertEqual(tq.command_result("FOREIGN DIP 20", "1"), "DIP")
            self.assertEqual(tq.command_result("BREAKOUT READY 20", "1"), "BREAK")
            self.assertEqual(tq.command_result("DYNAMIC LIQUIDITY 20", "1"), "DYNAMIC")

        cache.assert_called_once_with()
        value.assert_called_once_with(limit=20)
        turnover.assert_called_once_with(limit=20)
        dip.assert_called_once_with(limit=20)
        breakout.assert_called_once_with(limit=20)
        dynamic.assert_called_once_with(limit=20)


if __name__ == "__main__":
    unittest.main()
