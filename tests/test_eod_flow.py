import unittest
from unittest.mock import patch

from project_fabric.eod_flow import analyze_eod_flow, format_eod_flow


def row(day, close, high, low, foreign_buy, foreign_sell, bid=10_000, offer=10_000):
    return {
        "trade_date": day,
        "warehouse_date": "2026-11-02",
        "ticker": "BUMI",
        "open_price": close - 1,
        "high_price": high,
        "low_price": low,
        "close_price": close,
        "raw_json": {
            "ForeignBuy": foreign_buy,
            "ForeignSell": foreign_sell,
            "BidVolume": bid,
            "OfferVolume": offer,
        },
    }


class EODFlowTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            row("2026-11-02", 128, 131, 117, 947_361_700, 578_957_000, 37_644_800, 4_314_400),
            row("2026-10-30", 119, 123, 115, 937_032_900, 589_716_600),
            row("2026-10-29", 121, 131, 120, 758_885_900, 470_273_300),
            row("2026-10-28", 127, 129, 120, 921_782_500, 983_646_400),
            row("2026-10-27", 128, 141, 128, 578_020_400, 323_155_300),
            row("2026-10-26", 137, 145, 137, 317_516_800, 372_242_900),
        ]

    def test_bumi_rebound_uses_price_reclaim_and_positive_foreign(self):
        result = analyze_eod_flow(self.rows, broker_last_date="2026-10-12")
        self.assertEqual(result["status"], "REBOUND CONFIRMED")
        self.assertAlmostEqual(result["day_change_pct"], 7.563, places=2)
        self.assertAlmostEqual(result["period_change_pct"], -6.569, places=2)
        self.assertEqual(result["foreign_net_1d"], 368_404_700)
        self.assertEqual(result["foreign_net_5d"], 1_197_334_800)
        self.assertEqual(result["net_buy_days"], 4)
        self.assertGreater(result["queue_imbalance"], 0.7)
        self.assertFalse(result["broker_current"])

    def test_falling_price_negative_flow_is_selling_pressure(self):
        rows = [
            row("2026-11-01", 100, 105, 99, 100, 200, 100, 500),
            row("2026-11-02", 94, 99, 93, 100, 300, 100, 500),
        ]
        for item in rows:
            item["warehouse_date"] = "2026-11-02"
        result = analyze_eod_flow(rows)
        self.assertEqual(result["status"], "SELLING PRESSURE")

    def test_output_discloses_proxy_estimates_and_stale_broker(self):
        text = format_eod_flow("BUMI", rows=self.rows, broker_last_date="2026-10-12")
        self.assertIn("REBOUND CONFIRMED", text)
        self.assertIn("+368.4M", text)
        self.assertIn("estimasi", text)
        self.assertIn("DATA STALE", text)
        self.assertIn("bukan aggressor trade delta live", text)


class TelegramEODFlowRoutingTests(unittest.TestCase):
    @patch("telegram_query_bot.format_eod_flow", return_value="FLOW OK")
    def test_ticker_flow_eod_route(self, formatter):
        from telegram_query_bot import command_result
        self.assertEqual(command_result("BUMI FLOW EOD", "123"), "FLOW OK")
        formatter.assert_called_once_with("BUMI")

    @patch("telegram_query_bot.format_eod_flow", return_value="FLOW OK")
    def test_slash_flow_route(self, formatter):
        from telegram_query_bot import command_result
        self.assertEqual(command_result("/flow BUMI", "123"), "FLOW OK")
        formatter.assert_called_once_with("BUMI")


if __name__ == "__main__":
    unittest.main()
