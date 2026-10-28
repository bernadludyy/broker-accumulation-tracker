import unittest
from market_regime import detect_regime

def rows(n, change, net):
    return [{"Close": 100 + change, "OpenPrice": 100, "NetForeignValue": net} for _ in range(n)]

class MarketRegimeTests(unittest.TestCase):
    def test_bearish_cash(self): self.assertEqual((detect_regime(rows(150, -2, -1000)).label, detect_regime(rows(150, -2, -1000)).mode), ("BEARISH", "CASH"))
    def test_bullish_swing(self): self.assertEqual((detect_regime(rows(150, 2, 1000)).label, detect_regime(rows(150, 2, 1000)).mode), ("BULLISH", "SWING"))
    def test_insufficient_unknown(self): self.assertEqual(detect_regime(rows(99, 2, 1000)).label, "UNKNOWN")
