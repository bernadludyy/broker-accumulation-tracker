"""Offline regression tests for GAINER PROGRESS and PRE GAINER."""
import unittest
from datetime import date, timedelta
from unittest.mock import patch

import gainer_progress as feature


START = date(2026, 8, 1)
MARKET_DAY = (START + timedelta(days=60)).isoformat()
BROKER_DATES = [(START + timedelta(days=41 + index)).isoformat() for index in range(20)]


def history(*, high=110.0, low=100.0, value=1_000_000_000.0):
    rows = []
    pattern = [104.0, 105.0, 103.5, 105.5, 104.0, 106.0]
    for index in range(60):
        close = pattern[index % len(pattern)]
        rows.append(
            {
                "trade_date": (START + timedelta(days=index)).isoformat(),
                "open_price": close,
                "high_price": max(high, close),
                "low_price": min(low, close),
                "close_price": close,
                "turnover_idr": value,
            }
        )
    return rows


def market_row(close=109.0, value=1_100_000_000.0):
    return {
        "StockCode": "AAAA",
        "Open": 105.0,
        "High": close,
        "Low": min(104.0, close),
        "Close": close,
        "Value": value,
    }


def broker_flows(*, latest_lots=30_000.0, latest_sell=0.0):
    rows = []
    for index, day in enumerate(BROKER_DATES[-5:]):
        buy_lots = latest_lots if index == 4 else 10_000.0
        rows.append(
            {
                "date": day,
                "broker": "XL",
                "buy_value": buy_lots * 100 * 105,
                "sell_value": latest_sell * 100 * 105 if index == 4 else 0.0,
                "buy_lots": buy_lots,
                "sell_lots": latest_sell if index == 4 else 0.0,
            }
        )
        rows.append(
            {
                "date": day,
                "broker": "YP",
                "buy_value": 5_000 * 100 * 105,
                "sell_value": 4_000 * 100 * 105,
                "buy_lots": 5_000.0,
                "sell_lots": 4_000.0,
            }
        )
    return rows


class GainerProgressTests(unittest.TestCase):
    def analyze(self, row, flows=None):
        return feature.analyze_gainer_progress(
            [row],
            {"AAAA": history()},
            {"AAAA": flows or broker_flows()},
            BROKER_DATES,
            market_day=MARKET_DAY,
            ticker="AAAA",
        )["diagnostics"][0]

    def test_pre_ignition_combines_price_and_broker_persistence(self):
        row = self.analyze(market_row(close=109.0))
        self.assertEqual(row["phase"], "PRE-IGNITION")
        self.assertGreaterEqual(row["score"], feature.MIN_PRE_GAINER_SCORE)
        self.assertTrue(row["eligible"])

    def test_ignition_and_extended_are_not_pre_gainer_candidates(self):
        ignition = self.analyze(market_row(close=113.0, value=2_000_000_000.0))
        extended = self.analyze(market_row(close=130.0, value=3_000_000_000.0))
        self.assertEqual(ignition["phase"], "IGNITION/MARKUP")
        self.assertEqual(extended["phase"], "LATE/EXTENDED")
        self.assertFalse(ignition["eligible"])
        self.assertFalse(extended["eligible"])

    def test_large_lot_is_relative_not_an_absolute_claim(self):
        row = self.analyze(market_row(), broker_flows(latest_lots=90_000.0))
        broker = row["broker"]
        self.assertTrue(broker["large_lot"])
        self.assertGreaterEqual(broker["lot_spike"], 8.0)
        self.assertGreater(broker["concentration"], 0.90)

    def test_missing_broker_is_data_incomplete_not_zero(self):
        report = feature.analyze_gainer_progress(
            [market_row()],
            {"AAAA": history()},
            {},
            BROKER_DATES,
            market_day=MARKET_DAY,
            ticker="AAAA",
        )
        row = report["diagnostics"][0]
        self.assertFalse(row["eligible"])
        self.assertIn("belum tersedia", " ".join(row["reasons"]))

    def test_stale_broker_outside_latest_five_is_unavailable(self):
        stale = [{
            "date": BROKER_DATES[0],
            "broker": "XL",
            "buy_value": 10_000_000.0,
            "sell_value": 0.0,
            "buy_lots": 2_500.0,
            "sell_lots": 0.0,
        }]
        report = feature.analyze_gainer_progress(
            [market_row()],
            {"AAAA": history()},
            {"AAAA": stale},
            BROKER_DATES,
            market_day=MARKET_DAY,
            ticker="AAAA",
        )
        row = report["diagnostics"][0]
        self.assertFalse(row["eligible"])
        self.assertIn("belum tersedia", " ".join(row["reasons"]))

    def test_pre_gainer_ranking_excludes_late_rows(self):
        report = feature.analyze_gainer_progress(
            [market_row(close=109.0)],
            {"AAAA": history()},
            {"AAAA": broker_flows()},
            BROKER_DATES,
            market_day=MARKET_DAY,
        )
        self.assertEqual([row["ticker"] for row in report["rows"]], ["AAAA"])

    def test_formatters_hold_live_mode_outside_market_hours(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        now = datetime(2026, 11, 22, 21, 0, tzinfo=ZoneInfo("Asia/Jakarta"))
        with patch.object(feature, "_load_analysis", side_effect=AssertionError("must not load")):
            detail = feature.format_gainer_progress(
                "AAAA", "cache.db", "2026-11-20", mode="LIVE", now=now
            )
            scan = feature.format_pre_gainer(
                "cache.db", "2026-11-20", mode="LIVE", now=now
            )
        self.assertIn("HANYA JAM BURSA", detail)
        self.assertIn("HANYA JAM BURSA", scan)


class GainerCommandRoutingTests(unittest.TestCase):
    def test_dispatch_routes_detail_and_scan(self):
        import broker_ytd_dispatch as dispatcher

        with patch.object(dispatcher, "format_gainer_progress", return_value="DETAIL") as detail, patch.object(
            dispatcher, "format_pre_gainer", return_value="SCAN"
        ) as scan, patch.object(
            dispatcher.legacy, "safe_end_date", return_value="2026-11-20"
        ):
            self.assertEqual(
                dispatcher.command_result("GAINER PROGRESS NTBK", "1"), "DETAIL"
            )
            detail.assert_called_with(
                "NTBK",
                str(dispatcher.legacy.DB_PATH),
                "2026-11-20",
                mode="EOD",
            )
            self.assertEqual(
                dispatcher.command_result("PRE GAINER LIVE 5", "1"), "SCAN"
            )
            scan.assert_called_with(
                str(dispatcher.legacy.DB_PATH),
                "2026-11-20",
                mode="LIVE",
                limit=5,
            )
        self.assertIn("PRE GAINER", dispatcher.command_result("/help", "1"))
        self.assertIn("Format:", dispatcher.command_result("PRE GAINER 20", "1"))


if __name__ == "__main__":
    unittest.main()

