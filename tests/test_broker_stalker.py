"""Offline regression tests for cache-only BROKER STALKER."""
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import broker_stalker as stalker
import broker_ytd_dispatch as dispatcher


class BrokerStalkerTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        handle.close()
        self.db_path = handle.name
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                "CREATE TABLE broker_daily ("
                "trade_date TEXT NOT NULL,ticker TEXT NOT NULL,"
                "broker TEXT NOT NULL,side TEXT NOT NULL,"
                "lots INTEGER NOT NULL,value REAL NOT NULL)"
            )
            rows = []
            dates = [f"2026-10-{day:02d}" for day in range(5, 10)]
            for day in dates:
                rows += [
                    (day, "AAAA", "NI", "BUY", 100, 1_000_000_000),
                    (day, "AAAA", "CC", "BUY", 80, 800_000_000),
                    (day, "AAAA", "YP", "BUY", 60, 600_000_000),
                    (day, "AAAA", "PD", "SELL", 20, 200_000_000),
                    (day, "BBBB", "NI", "BUY", 40, 400_000_000),
                    (day, "BBBB", "CC", "BUY", 20, 200_000_000),
                    (day, "BBBB", "PD", "SELL", 10, 100_000_000),
                    (day, "CCCC", "NI", "BUY", 100, 1_000_000_000),
                    (day, "CCCC", "CC", "SELL", 200, 2_000_000_000),
                    (day, "DDDD", "NI", "BUY", 50, 500_000_000),
                ]
            con.executemany("INSERT INTO broker_daily VALUES(?,?,?,?,?,?)", rows)

    def tearDown(self):
        os.unlink(self.db_path)

    def test_ranks_multi_broker_accumulation(self):
        report = stalker.analyze_broker_stalker(
            self.db_path, "2026-10-09", lookback=5
        )
        self.assertEqual([row["ticker"] for row in report["rows"]], ["AAAA", "BBBB"])
        self.assertEqual(report["rows"][0]["broker_count"], 3)
        self.assertEqual(report["active_brokers"], 4)
        self.assertEqual(report["stock_count"], 4)
        self.assertEqual(report["multi_broker_count"], 2)

    def test_requires_two_persistent_buyers(self):
        report = stalker.analyze_broker_stalker(
            self.db_path, "2026-10-09", lookback=5
        )
        tickers = {row["ticker"] for row in report["rows"]}
        self.assertNotIn("CCCC", tickers)
        self.assertNotIn("DDDD", tickers)

    def test_report_is_explicit_about_limits_and_foreign_share(self):
        text = stalker.format_broker_stalker(
            self.db_path, "2026-10-09", lookback=5, limit=1
        )
        self.assertIn("BROKER STALKER — 5 SESI", text)
        self.assertIn("1. AAAA | 3 broker", text)
        self.assertNotIn("2. BBBB", text)
        self.assertIn("Porsi asing: N/A", text)
        self.assertIn("bukan sinyal entry", text)

    def test_invalid_legacy_pair_is_quarantined(self):
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                "INSERT INTO broker_daily VALUES(?,?,?,?,?,?)",
                ("2026-10-09", "EEEE", "AB", "BUY", 10, 500_000_000),
            )
            con.execute(
                "INSERT INTO broker_daily VALUES(?,?,?,?,?,?)",
                ("2026-10-09", "EEEE", "AB", "SELL", -1, 1_000_000),
            )
        report = stalker.analyze_broker_stalker(
            self.db_path, "2026-10-09", lookback=5
        )
        self.assertEqual(report["invalid_rows"], 1)
        self.assertNotIn("EEEE", {row["ticker"] for row in report["rows"]})

    def test_command_routes_defaults_and_options(self):
        with patch.object(dispatcher.legacy, "DB_PATH", self.db_path), patch.object(
            dispatcher.legacy, "safe_end_date", return_value="2026-10-09"
        ):
            default = dispatcher.command_result("BROKER STALKER", "123")
            custom = dispatcher.command_result("/stalker 10D 1", "123")
        self.assertIn("BROKER STALKER — 5 SESI", default)
        self.assertIn("BROKER STALKER — 10 SESI", custom)
        self.assertIn("BROKER STALKER", dispatcher.command_result("/help", "123"))

    def test_invalid_command_is_rejected_without_fallback(self):
        with patch.object(
            dispatcher, "_original_command_result", side_effect=AssertionError("legacy")
        ):
            self.assertIn(
                "Format:", dispatcher.command_result("BROKER STALKER 7D", "123")
            )

    def test_missing_database_is_read_only_error(self):
        missing = self.db_path + ".missing"
        self.assertIn(
            "SQLite belum siap",
            stalker.format_broker_stalker(missing, "2026-10-09"),
        )
        self.assertFalse(os.path.exists(missing))


if __name__ == "__main__":
    unittest.main()
