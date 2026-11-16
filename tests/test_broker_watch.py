from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import broker_watch
import broker_ytd_dispatch as dispatch


def make_db(path: str) -> None:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE broker_daily ("
        "trade_date TEXT, ticker TEXT, broker TEXT, side TEXT, "
        "lots REAL, value REAL)"
    )
    for day in ("2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"):
        con.execute(
            "INSERT INTO broker_daily VALUES (?,?,?,?,?,?)",
            (day, "BBRI", "NI", "BUY", 100, 50_000_000),
        )
        con.execute(
            "INSERT INTO broker_daily VALUES (?,?,?,?,?,?)",
            (day, "BBRI", "NI", "SELL", 20, 8_000_000),
        )
    for day in ("2026-11-09", "2026-11-10", "2026-11-11"):
        con.execute(
            "INSERT INTO broker_daily VALUES (?,?,?,?,?,?)",
            (day, "TLKM", "NI", "BUY", 50, 20_000_000),
        )
    for day in ("2026-11-12", "2026-11-13"):
        con.execute(
            "INSERT INTO broker_daily VALUES (?,?,?,?,?,?)",
            (day, "TLKM", "NI", "SELL", 50, 35_000_000),
        )
    con.commit()
    con.close()


def live_fetcher(endpoint: str, params: dict) -> dict:
    if endpoint == "summary":
        return {"items": [
            {"code": "BBRI", "lastPrice": 5200, "open": 5100},
            {"code": "TLKM", "lastPrice": 3000, "open": 3050},
        ]}
    if endpoint == "tradebook":
        ticker = params["code"]
        buy_lots = 800 if ticker == "BBRI" else 200
        sell_lots = 200 if ticker == "BBRI" else 800
        return {"byPrice": [{
            "price": 5000 if ticker == "BBRI" else 3000,
            "buyLots": buy_lots,
            "sellLots": sell_lots,
        }]}
    raise AssertionError(endpoint)


class BrokerWatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "broker.db")
        make_db(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def test_selects_only_persistent_positive_net_broker_rows(self):
        result = broker_watch.select_candidates(
            self.db_path, "ni", "2026-11-13", lookback=5, limit=5
        )
        self.assertEqual([row["ticker"] for row in result["rows"]], ["BBRI"])
        row = result["rows"][0]
        self.assertEqual(row["positive_days"], 5)
        self.assertEqual(row["net_value"], 210_000_000)
        self.assertEqual(row["avg_buy"], 5000)

    def test_live_proxy_is_bounded_and_never_claims_broker_identity(self):
        calls = []

        def fetch(endpoint, params):
            calls.append((endpoint, params))
            return live_fetcher(endpoint, params)

        output = broker_watch.format_broker_watch(
            self.db_path, "NI", "2026-11-13", limit=3, fetcher=fetch
        )
        self.assertEqual(len(calls), 2)  # one shared quote + one eligible tradebook
        self.assertIn("DIDUKUNG ORDER FLOW (PROXY)", output)
        self.assertIn("identitas broker intraday TIDAK terkonfirmasi", output)
        self.assertIn("bukan transaksi broker yang dipilih", output)
        self.assertIn("Operasi ZAPI: maks. 2; aktual 2", output)

    def test_no_candidates_makes_no_live_request(self):
        calls = []
        output = broker_watch.format_broker_watch(
            self.db_path, "AK", "2026-11-13",
            fetcher=lambda *args: calls.append(args),
        )
        self.assertEqual(calls, [])
        self.assertIn("Tidak ada request live", output)

    @patch.object(dispatch.legacy, "authorized", return_value=False)
    @patch.object(dispatch, "format_broker_watch")
    def test_dispatch_authorizes_before_db_or_api(self, formatter, authorized):
        output = dispatch.command_result("BROKER WATCH NI", "group")
        self.assertIn("chat pribadi", output)
        formatter.assert_not_called()

    @patch.object(dispatch.legacy, "authorized", return_value=True)
    @patch.object(dispatch.legacy, "safe_end_date", return_value="2026-11-13")
    @patch.object(dispatch, "format_broker_watch", return_value="ok")
    def test_dispatch_parses_period_limit_and_alias(self, formatter, safe, authorized):
        self.assertEqual(dispatch.command_result("BROKER WATCH NI 10D 5", "owner"), "ok")
        formatter.assert_called_with(
            str(dispatch.legacy.DB_PATH), "NI", "2026-11-13", lookback=10, limit=5
        )
        self.assertEqual(dispatch.command_result("/brokerwatch NI", "owner"), "ok")
        self.assertIn("Format:", dispatch.command_result("BROKER WATCH NI 7", "owner"))

    def test_help_mentions_proxy_and_limits(self):
        output = dispatch.command_result("HELP BROKER", "owner")
        self.assertIn("BROKER WATCH NI", output)
        self.assertIn("PROXY", output)
        self.assertIn("maks. 6 ZAPI", output)


if __name__ == "__main__":
    unittest.main()
