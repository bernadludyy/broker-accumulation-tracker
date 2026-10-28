import unittest

from market_regime import detect_regime, format_regime


def rows(n, change, net):
    return [
        {"Close": 100 + change, "OpenPrice": 100, "NetForeignValue": net}
        for _ in range(n)
    ]


class MarketRegimeTests(unittest.TestCase):
    def test_bearish_cash(self):
        result = detect_regime(rows(150, -2, -1000))
        self.assertEqual((result.label, result.mode), ("BEARISH", "CASH"))

    def test_bullish_swing(self):
        result = detect_regime(rows(150, 2, 1000))
        self.assertEqual((result.label, result.mode), ("BULLISH", "SWING"))

    def test_insufficient_data_is_unknown(self):
        self.assertEqual(detect_regime(rows(99, 2, 1000)).label, "UNKNOWN")

    def test_production_share_fields_are_converted_to_value(self):
        production_rows = [
            {
                "Close": 98,
                "OpenPrice": 100,
                "ForeignBuy": 1_000,
                "ForeignSell": 2_000,
            }
            for _ in range(150)
        ]
        result = detect_regime(production_rows)
        self.assertEqual((result.label, result.mode), ("BEARISH", "CASH"))
        self.assertEqual(result.sample, 150)
        self.assertTrue(result.foreign_estimated)
        self.assertEqual(result.foreign_net, -14_700_000)
        self.assertIn("estimasi shares × close", format_regime(result))

    def test_snake_case_value_fields_are_supported(self):
        result = detect_regime([
            {
                "close": 102,
                "open": 100,
                "foreign_buy_value": 2_000,
                "foreign_sell_value": 1_000,
            }
            for _ in range(150)
        ])
        self.assertEqual(result.label, "BULLISH")
        self.assertFalse(result.foreign_estimated)


if __name__ == "__main__":
    unittest.main()
