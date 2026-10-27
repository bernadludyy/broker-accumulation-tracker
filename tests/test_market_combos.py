"""Offline tests for bounded Bot 1 market combo commands."""
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import broker_ytd_dispatch as dispatcher
import market_combos as combos


def market_row(code, volume, value, frequency, market_cap=None):
    row = {
        "StockCode": code,
        "Volume": volume,
        "Value": value,
        "Frequency": frequency,
        "Close": 100,
    }
    if market_cap is not None:
        row["MarketCap"] = market_cap
    return row


def book(bid_pct):
    bid_lots = bid_pct * 10
    ask_lots = (100 - bid_pct) * 10
    return {
        "data": {
            "bids": [{"lots": bid_lots}],
            "asks": [{"lots": ask_lots}],
            "bestBid": 100,
            "bestAsk": 101,
        }
    }


class MarketComboTests(unittest.TestCase):
    market_time = datetime(2026, 10, 27, 10, 0, tzinfo=ZoneInfo("Asia/Jakarta"))

    def test_combo2_prefilters_volume_frequency_then_live_bid(self):
        rows = [
            market_row("AAAA", 1_000_000, 5_000_000_000, 10_000),
            market_row("BBBB", 900_000, 4_000_000_000, 9_000),
            market_row("CCCC", 800_000, 3_000_000_000, 8_000),
        ]
        calls = []

        def fetcher(code):
            calls.append(code)
            return book({"AAAA": 70, "BBBB": 59, "CCCC": 65}[code])

        report = combos.analyze_market_combo(2, rows, fetcher=fetcher)
        self.assertEqual([r["ticker"] for r in report["rows"]], ["AAAA", "CCCC"])
        self.assertEqual(set(calls), {"AAAA", "BBBB", "CCCC"})
        self.assertEqual(report["checked_count"], 3)

    def test_combo2_never_fetches_more_than_bounded_shortlist(self):
        rows = [
            market_row(f"A{i:03d}", 1_000_000 - i, 5_000_000_000, 10_000 - i)
            for i in range(30)
        ]
        calls = []

        def fetcher(code):
            calls.append(code)
            return book(70)

        report = combos.analyze_market_combo(2, rows, fetcher=fetcher)
        self.assertEqual(len(calls), combos.PREFILTER_LIMIT)
        self.assertEqual(report["checked_count"], combos.PREFILTER_LIMIT)

    def test_combo1_uses_ats_and_does_not_filter_market_cap(self):
        rows = [
            market_row("SMAL", 1_000_000, 12_000_000_000, 1_000, 50_000_000_000),
            market_row("BIGG", 1_000_000, 12_000_000_000, 1_000, 50_000_000_000_000),
            market_row("LOWA", 1_000_000, 1_000_000_000, 1_000, 500_000_000_000),
            market_row("MIDA", 1_000_000, 2_000_000_000, 1_000, 5_000_000_000_000),
            market_row("MIDB", 1_000_000, 3_000_000_000, 1_000, 2_000_000_000_000),
        ]
        report = combos.analyze_market_combo(1, rows, fetcher=lambda _code: book(70))
        tickers = {row["ticker"] for row in report["rows"]}
        self.assertIn("SMAL", tickers)
        self.assertIn("BIGG", tickers)

    def test_combo1_excludes_single_trade_block_noise(self):
        rows = [
            market_row("BLOCK", 10_000_000, 100_000_000_000, 1),
            market_row("GOOD", 1_000_000, 10_000_000_000, 1_000),
        ]
        report = combos.analyze_market_combo(1, rows, fetcher=lambda _code: book(70))
        self.assertNotIn("BLOCK", {row["ticker"] for row in report["rows"]})

    def test_orderbook_fallback_calculates_depth_and_spread(self):
        parsed = combos.analyze_orderbook(book(65))
        self.assertAlmostEqual(parsed["bid_percent"], 65.0)
        self.assertAlmostEqual(parsed["bid_offer_ratio"], 65 / 35)
        self.assertGreater(parsed["spread_pct"], 0)

    def test_format_is_explicit_about_watch_only_and_request_count(self):
        rows = [market_row("AAAA", 1_000_000, 5_000_000_000, 10_000)]
        with patch.object(
            combos, "load_shared_live_market",
            return_value=(rows, "2026-10-27", "TEST CACHE"),
        ), patch.object(combos.tracker, "fetch_orderbook", return_value=book(70)):
            text = combos.format_market_combo(2, now=self.market_time)
        self.assertIn("MARKET COMBO 2", text)
        self.assertIn("ZAPI orderbook maksimal: 1 request", text)
        self.assertIn("WATCH ONLY", text)

    def test_offhours_uses_no_market_or_orderbook_request(self):
        with patch.object(
            combos, "load_shared_live_market", side_effect=AssertionError("market")
        ), patch.object(
            combos.tracker, "fetch_orderbook", side_effect=AssertionError("book")
        ):
            text = combos.format_market_combo(
                2,
                now=datetime(
                    2026, 10, 27, 18, 0, tzinfo=ZoneInfo("Asia/Jakarta")
                ),
            )
        self.assertIn("HANYA JAM BURSA", text)
        self.assertIn("Tidak ada request orderbook", text)

    def test_eod_mode_is_available_offhours_without_orderbook(self):
        rows = [market_row("AAAA", 1_000_000, 5_000_000_000, 10_000)]
        with patch.object(
            combos, "load_cached_market",
            return_value=(rows, "2026-10-27", "TEST EOD CACHE"),
        ) as loader, patch.object(
            combos.tracker, "fetch_orderbook", side_effect=AssertionError("book")
        ):
            text = combos.format_market_combo(
                2,
                mode="EOD",
                now=datetime(
                    2026, 10, 27, 18, 0, tzinfo=ZoneInfo("Asia/Jakarta")
                ),
            )
        loader.assert_called_once()
        self.assertTrue(loader.call_args.kwargs["completed_only"])
        self.assertIn("MARKET COMBO 2 EOD", text)
        self.assertIn("ZAPI/orderbook: 0 request", text)
        self.assertIn("MARKET COMBO 2 LIVE", text)

    def test_eod_mode_warns_when_snapshot_is_stale(self):
        rows = [market_row("AAAA", 1_000_000, 5_000_000_000, 10_000)]
        with patch.object(
            combos, "load_cached_market",
            return_value=(rows, "2026-10-24", "TEST EOD CACHE"),
        ):
            text = combos.format_market_combo(
                1,
                mode="EOD",
                now=datetime(
                    2026, 10, 27, 18, 0, tzinfo=ZoneInfo("Asia/Jakarta")
                ),
            )
        self.assertIn("EOD BELUM TERBARU", text)
        self.assertIn("target 2026-10-27", text)

    def test_command_routes_combo2_and_combo1(self):
        with patch.object(dispatcher, "format_market_combo", return_value="OK") as formatter:
            self.assertEqual(dispatcher.command_result("MARKET COMBO 2", "1"), "OK")
            formatter.assert_called_with(2, limit=5, mode="LIVE")
            self.assertEqual(dispatcher.command_result("MARKET COMBO 1 EOD 3", "1"), "OK")
            formatter.assert_called_with(1, limit=3, mode="EOD")
            self.assertEqual(dispatcher.command_result("MARKET COMBO 2 4 LIVE", "1"), "OK")
            formatter.assert_called_with(2, limit=4, mode="LIVE")
        self.assertIn("Format:", dispatcher.command_result("MARKET COMBO 3", "1"))
        self.assertIn("Format:", dispatcher.command_result("MARKET COMBO 2 EOD LIVE", "1"))
        self.assertIn("MARKET COMBO 2", dispatcher.command_result("/help", "1"))

    def test_bad_market_data_fails_closed_before_orderbook(self):
        calls = []
        with self.assertRaises(ValueError):
            combos.analyze_market_combo(
                2,
                [market_row("AAAA", 0, 0, 0)],
                fetcher=lambda code: calls.append(code),
            )
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
