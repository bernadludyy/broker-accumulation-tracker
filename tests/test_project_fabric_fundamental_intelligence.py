from __future__ import annotations

import unittest

from project_fabric.fundamental_intelligence import (
    choose_benchmark,
    macro_sector,
    score_fundamental,
)


def row(ticker: str, sector: str = "Coal Production", offset: float = 0.0):
    return {
        "ticker": ticker,
        "company_name": ticker,
        "sector": sector,
        "pe": 8.0 + offset,
        "pb": 0.8 + offset / 10,
        "ps": 1.2 + offset / 10,
        "roe_pct": 20.0 - offset,
        "roa_pct": 10.0 - offset / 2,
        "net_margin_pct": 18.0 - offset / 2,
        "debt_equity_pct": 0.4 + offset / 20,
        "current_ratio": 220.0 - offset,
    }


def statements(negative: bool = False):
    sign = -1 if negative else 1
    return [
        {"period_kind": "quarterly", "timeframe": "Q2 ’25", "revenue": 100,
         "net_profit_loss": 10 * sign, "operating_cash_flow": 8 * sign},
        {"period_kind": "quarterly", "timeframe": "Q1 ’26", "revenue": 60,
         "net_profit_loss": 8 * sign, "operating_cash_flow": 6 * sign},
        {"period_kind": "quarterly", "timeframe": "Q2 ’26", "revenue": 125,
         "net_profit_loss": 15 * sign, "operating_cash_flow": 12 * sign},
        {"period_kind": "annually", "timeframe": "2023", "net_profit_loss": 30 * sign,
         "operating_cash_flow": 25 * sign},
        {"period_kind": "annually", "timeframe": "2024", "net_profit_loss": 35 * sign,
         "operating_cash_flow": 28 * sign},
        {"period_kind": "annually", "timeframe": "2025", "net_profit_loss": 40 * sign,
         "operating_cash_flow": 32 * sign},
    ]


class FundamentalIntelligenceTests(unittest.TestCase):
    def test_exact_sector_with_eight_peers_is_used(self):
        universe = [row(f"C{i}", offset=i) for i in range(8)]
        benchmark = choose_benchmark(universe[0], universe)
        self.assertEqual(benchmark.mode, "SEKTOR")
        self.assertEqual(benchmark.count, 8)

    def test_small_peer_group_falls_back_to_macro(self):
        universe = [row(f"C{i}", "Coal Production", i) for i in range(3)]
        universe += [row(f"M{i}", "Diversified Metals", i) for i in range(7)]
        benchmark = choose_benchmark(universe[0], universe)
        self.assertEqual(macro_sector("Coal Production"), "COMMODITY")
        self.assertEqual(benchmark.mode, "MAKRO SEKTOR")
        self.assertEqual(benchmark.count, 10)

    def test_score_has_sector_categories_growth_and_overlay(self):
        universe = [row(f"C{i}", offset=i) for i in range(12)]
        result = score_fundamental(universe[0], universe, statements())
        self.assertGreaterEqual(result["coverage"], 70)
        self.assertIsNotNone(result["score"])
        self.assertIn(result["overlay"], {-8, 0, 2, 5})
        self.assertFalse(result["red_flag"])
        self.assertGreater(result["growth"]["values"]["revenue"], 20)

    def test_financials_exclude_normal_company_de_and_current_ratio(self):
        universe = [row(f"B{i}", "Banks", i) for i in range(10)]
        result = score_fundamental(universe[0], universe, statements())
        self.assertEqual(result["policy"]["safety"], 0)
        self.assertIsNone(result["categories"]["safety"])
        self.assertIsNotNone(result["score"])

    def test_repeated_negative_profit_and_ocf_blocks_swing(self):
        universe = [row(f"C{i}", offset=i) for i in range(8)]
        result = score_fundamental(universe[0], universe, statements(negative=True))
        self.assertTrue(result["red_flag"])
        self.assertEqual(result["overlay"], -12)
        self.assertIn("BLOCK SWING", result["tier"])

if __name__ == "__main__":
    unittest.main()

