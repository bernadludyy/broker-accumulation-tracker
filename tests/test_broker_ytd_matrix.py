"""Offline regression tests for cache-only broker YTD matrix commands."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import broker_ytd_matrix as matrix
import broker_ytd_dispatch as dispatcher
import telegram_query_bot as legacy


class BrokerYtdMatrixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / "broker.db")
        with sqlite3.connect(self.db_path) as con:
            con.executescript("""
                CREATE TABLE broker_daily (
                    trade_date TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    broker TEXT NOT NULL,
                    side TEXT NOT NULL,
                    lots INTEGER NOT NULL,
                    value REAL NOT NULL,
                    PRIMARY KEY (trade_date,ticker,broker,side)
                );
                CREATE TABLE stock_summary_snapshots (
                    trade_date TEXT PRIMARY KEY
                );
            """)
            rows = [
                ("2025-12-30", "BBRI", "NI", "BUY", 100, 999999),
                ("2026-01-05", "BBRI", "NI", "BUY", 10, 4000),
                ("2026-01-05", "BBRI", "NI", "SELL", 3, 1000),
                ("2026-01-06", "BBRI", "NI", "BUY", 15, 5000),
                ("2026-01-06", "BBRI", "NI", "SELL", 1, 500),
                ("2026-10-14", "BBRI", "NI", "BUY", 5, 2500),
                ("2026-01-05", "BBRI", "CC", "BUY", 4, 2000),
                ("2026-01-06", "BBRI", "CC", "BUY", 1, 500),
                ("2026-10-14", "BBRI", "YP", "SELL", 12, 6000),
                ("2026-01-05", "BBRI", "HY", "BUY", 2, 1000),
                ("2026-01-05", "BMRI", "NI", "BUY", 30, 12000),
                ("2026-10-14", "BMRI", "NI", "SELL", 25, 10000),
                ("2026-01-07", "BBNI", "NI", "BUY", 2, 1000),
                ("2026-10-14", "BBNI", "CC", "BUY", 2, 700),
                ("2026-01-07", "TLKM", "YP", "SELL", 20, 8000),
                ("2026-01-07", "TLKM", "CC", "SELL", 10, 3500),
            ]
            con.executemany(
                "INSERT INTO broker_daily "
                "(trade_date,ticker,broker,side,lots,value) VALUES (?,?,?,?,?,?)",
                rows,
            )
            con.executemany(
                "INSERT INTO stock_summary_snapshots (trade_date) VALUES (?)",
                [("2026-01-08",), ("2026-10-14",)],
            )
            con.commit()

    def test_ticker_has_sorted_buy_sell_and_correct_totals(self):
        reply = matrix.format_ticker_top("BBRI", self.db_path, "2026-10-14")
        self.assertIn("TOP 3 NET BUY", reply)
        self.assertIn("1. NI | +Rp 10.0 Rb", reply)
        self.assertIn("2. CC | +Rp 2.5 Rb", reply)
        self.assertIn("3. HY | +Rp 1.0 Rb", reply)
        self.assertIn("Total Top 3: +Rp 13.5 Rb", reply)
        self.assertIn("WAvg Buy: Rp 3.83", reply)
        self.assertEqual(reply.count("WAvg Buy:"), 3)
        self.assertIn("1. YP | -Rp 6.0 Rb", reply)
        self.assertIn("WAvg Sell: Rp 5.00", reply)
        self.assertIn("Total Top 3: -Rp 6.0 Rb", reply)
        self.assertIn("PARTIAL", reply)
        self.assertNotIn("999,999", reply)
        self.assertIn("bukan posisi saham", reply)

    def test_broker_rank_all_cached_tickers_not_just_watchlist(self):
        reply = matrix.format_broker_top("NI", self.db_path, "2026-10-14")
        self.assertIn("1. BBRI | +Rp 10.0 Rb", reply)
        self.assertIn("2. BMRI | +Rp 2.0 Rb", reply)
        self.assertIn("3. BBNI | +Rp 1.0 Rb", reply)
        self.assertIn("Total Net BUY Top 3: +Rp 13.0 Rb", reply)
        self.assertIn("Cakupan: 4 ticker", reply)
        self.assertIn("WAvg Buy: Rp 3.83", reply)
        self.assertIn("WAvg Buy: Rp 4.00", reply)
        self.assertIn("WAvg Buy: Rp 5.00", reply)
        self.assertEqual(reply.count("WAvg Buy:"), 3)
        self.assertIn("PARTIAL", reply)

    def test_broker_sell_mode_and_empty_result(self):
        reply = matrix.format_broker_top(
            "YP", self.db_path, "2026-10-14", mode="SELL"
        )
        self.assertIn("1. TLKM | -Rp 8.0 Rb", reply)
        self.assertIn("2. BBRI | -Rp 6.0 Rb", reply)
        self.assertIn("Total Net SELL Top 3: -Rp 14.0 Rb", reply)
        self.assertIn("WAvg Sell: Rp 4.00", reply)
        self.assertIn("WAvg Sell: Rp 5.00", reply)
        self.assertIn(
            "Tidak ada Net BUY ZZ",
            matrix.format_broker_top("ZZ", self.db_path, "2026-10-14"),
        )

    def test_weighted_average_is_side_based_not_net_cost_basis(self):
        row = {
            "buy_value": 11500, "buy_lots": 30,
            "sell_value": 1500, "sell_lots": 4,
            "net_value": 10000, "net_lots": 26,
        }
        self.assertAlmostEqual(matrix._weighted_avg_price(row, "BUY"), 11500 / 3000)
        self.assertAlmostEqual(matrix._weighted_avg_price(row, "SELL"), 1500 / 400)
        self.assertNotAlmostEqual(
            matrix._weighted_avg_price(row, "BUY"),
            row["net_value"] / (row["net_lots"] * 100),
        )
        with self.assertRaises(ValueError):
            matrix._weighted_avg_price(row, "NET")

    def test_zero_lots_or_missing_data_yield_na(self):
        for row in (
            {"buy_value": 1200, "buy_lots": 0},
            {"buy_value": 0, "buy_lots": 10},
            {"buy_value": 1000, "buy_lots": None},
            {"buy_value": float("inf"), "buy_lots": 10},
        ):
            self.assertEqual(matrix._format_avg_price(row, "BUY"), "N/A")
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                "INSERT INTO broker_daily "
                "(trade_date,ticker,broker,side,lots,value) "
                "VALUES ('2026-10-14','ZERO','ZZ','BUY',0,1000)"
            )
            con.commit()
        reply = matrix.format_ticker_top("ZERO", self.db_path, "2026-10-14")
        self.assertIn("WAvg Buy: N/A", reply)

    def test_no_data_and_invalid_inputs(self):
        self.assertIn(
            "belum tersimpan",
            matrix.format_ticker_top("ABCD", self.db_path, "2026-10-14"),
        )
        self.assertIn(
            "tidak valid",
            matrix.format_ticker_top("BBRI;DELETE", self.db_path, "2026-10-14"),
        )
        self.assertIn(
            "tidak valid",
            matrix.format_broker_top("NI'", self.db_path, "2026-10-14"),
        )
        self.assertIn(
            "belum siap",
            matrix.format_ticker_top(
                "BBRI", str(Path(self.tmp.name) / "not_created.db"),
                "2026-10-14",
            ),
        )

    def test_read_only_matrix_does_not_write_or_call_zapi(self):
        with sqlite3.connect(self.db_path) as con:
            before = con.execute("SELECT COUNT(*) FROM broker_daily").fetchone()[0]
        with patch.object(
            legacy.tracker, "fetch_broker_summary",
            side_effect=AssertionError("ZAPI forbidden"),
        ):
            matrix.format_ticker_top("BBRI", self.db_path, "2026-10-14")
            matrix.format_broker_top("NI", self.db_path, "2026-10-14")
        with sqlite3.connect(self.db_path) as con:
            after = con.execute("SELECT COUNT(*) FROM broker_daily").fetchone()[0]
        self.assertEqual(before, after)

    def test_telegram_routing_and_legacy_fallback(self):
        with patch.object(dispatcher, "format_ticker_top", return_value="ticker ok") as ticker:
            self.assertEqual(
                dispatcher.command_result("BROKER TOP BBRI YTD", "123"),
                "ticker ok",
            )
            ticker.assert_called_once_with(
                "BBRI", str(legacy.DB_PATH), legacy.safe_end_date()
            )
        with patch.object(dispatcher, "format_broker_top", return_value="broker ok") as broker:
            self.assertEqual(
                dispatcher.command_result("CC NI TOP SELL YTD", "123"),
                "broker ok",
            )
            self.assertEqual(broker.call_args.kwargs["mode"], "SELL")
        with patch.object(
            dispatcher, "_original_command_result", return_value="existing"
        ) as old:
            self.assertEqual(
                dispatcher.command_result("CC NI ACCUM YTD", "123"),
                "existing",
            )
            old.assert_called_once()
        self.assertIn("BROKER TOP BBRI YTD", dispatcher.command_result("/help", "123"))
        self.assertIn(
            "Format: CC NI TOP YTD",
            dispatcher.command_result("CC NI TOP UNKNOWN", "123"),
        )


if __name__ == "__main__":
    unittest.main()
