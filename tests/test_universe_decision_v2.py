"""Regression coverage for cache-only Bot 1/Bot 2 intelligent rankings."""
from __future__ import annotations

import unittest
from unittest.mock import patch

import bot2_entry
import telegram_query_bot
from universe_decision import evaluate_universe, priority_score, summary_line


def cached_signals(distribution=20):
    return {
        "history_last_date": "2026-10-09",
        "summary_observed_at": "2026-10-10T09:10:00+07:00",
        "scores": {
            "accumulation_persistence": {"score": 88, "status": "PRICE_VOLUME_PROXY"},
            "breakout_readiness": {"score": 83},
            "institutional_flow": {"score": 78, "source": "reported_foreign_value"},
            "distribution_risk": {"score": distribution},
            "entry_quality": {"score": 82},
        },
    }


def market(ticker="ABCD", frequency=12000, foreign=10000, opening=100, close=98):
    return {
        "ticker": ticker, "StockCode": ticker,
        "Frequency": frequency, "Close": close, "OpenPrice": opening,
        "foreignBuyValue": foreign + 100, "foreignSellValue": 100,
        "Value": 1500000,
    }


class UniverseDecisionV2Tests(unittest.TestCase):
    def test_all_five_scores_and_risk_penalty(self):
        with patch("universe_decision.tracker.peek_universe_intelligence",
                   return_value=cached_signals()):
            good = evaluate_universe("ABCD", as_of="2026-10-10")
        self.assertEqual(good["coverage"], 5)
        self.assertGreater(good["score"], 70)
        self.assertFalse(good["risk_high"])

        with patch("universe_decision.tracker.peek_universe_intelligence",
                   return_value=cached_signals(89)):
            risky = evaluate_universe("ABCD", as_of="2026-10-10")
        self.assertTrue(risky["risk_high"])
        self.assertEqual(risky["decision"], "WASPADA DISTRIBUSI")
        self.assertLess(priority_score(80, risky), priority_score(80, good))

    def test_missing_data_cannot_block_or_create_bogus_zero(self):
        with patch("universe_decision.tracker.peek_universe_intelligence",
                   side_effect=RuntimeError("Neon down")):
            intel = evaluate_universe("ABCD")
        self.assertIsNone(intel["score"])
        self.assertEqual(intel["coverage"], 0)
        self.assertEqual(priority_score(76, intel), 76)
        self.assertIn("DATA TERBATAS", summary_line(intel))

    def test_stale_history_excluded_not_treated_as_recent(self):
        with patch("universe_decision.tracker.peek_universe_intelligence",
                   return_value=cached_signals(89)):
            intel = evaluate_universe("ABCD", as_of="2026-11-01")
        self.assertIsNone(intel["score"])
        self.assertIsNone(intel["history_date"])
        self.assertFalse(intel["risk_high"])

    def test_estimated_shares_foreign_not_promoted(self):
        with patch("universe_decision.tracker.peek_universe_intelligence",
                   return_value={}):
            intel = evaluate_universe("ABCD", market_row={
                "Close": 100, "Value": 2000000,
                "ForeignBuy": 1000, "ForeignSell": 300,
            })
        self.assertEqual(intel["coverage"], 0)
        self.assertEqual(intel["items"]["institutional_flow"]["source"],
                         "estimated_from_foreign_shares_x_close")

    def test_smart_market_ranks_only_eligible_candidates(self):
        # Two tickers pass top-50 frequency, top-30 net foreign, correction.
        rows = [market("AAAA", 15000, 5000), market("BBBB", 14000, 4000),
                market("CCCC", 2000, 0, close=102)]
        def fake_intel(ticker, **kwargs):
            return {"ticker": ticker, "score": 92 if ticker == "BBBB" else 25,
                    "coverage": 5, "decision": "PRIORITAS TINGGI",
                    "risk_high": False}
        with patch.object(bot2_entry, "load_shared_live_market",
                          return_value=(rows, "2026-10-09", "CACHED")), \
             patch.object(bot2_entry, "evaluate_universe", side_effect=fake_intel):
            report = bot2_entry.format_bot2_market_command("SMART", limit=10)
        self.assertIn("BOT2 SMART RANKING", report)
        self.assertIn("BBBB", report)
        self.assertNotIn("3. CCCC", report)
        self.assertLess(report.index("1. BBBB"), report.index("2. AAAA"))

    def test_telegram_routes_aliases_without_changing_existing_commands(self):
        with patch.object(telegram_query_bot, "format_bot1_smart",
                          return_value="BROKER_SMART") as f:
            self.assertEqual(telegram_query_bot.command_result("BOT1 SMART ADRO", "123"), "BROKER_SMART")
            f.assert_called_with("ADRO")
        with patch.object(telegram_query_bot, "format_universe_report",
                          return_value="INTEL") as f:
            self.assertEqual(telegram_query_bot.command_result("/intel ADRO", "123"), "INTEL")
            self.assertEqual(telegram_query_bot.command_result("ADRO INTEL", "123"), "INTEL")
        with patch.object(telegram_query_bot, "format_bot2_market_command",
                          return_value="RANK") as f:
            self.assertEqual(telegram_query_bot.command_result("BOT2 SMART 8", "123"), "RANK")
            f.assert_called_once_with("SMART", limit=8)

    def test_bot1_smart_without_database_is_safe(self):
        with patch.object(telegram_query_bot, "aggregate_period",
                          return_value=(None, [])), \
             patch.object(telegram_query_bot, "evaluate_universe",
                          return_value={"score": None, "coverage": 0, "decision": "DATA TERBATAS",
                                        "history_date": None, "risk_high": False}):
            report = telegram_query_bot.format_bot1_smart("ADRO")
        self.assertIn("BELUM ADA DATA", report)


if __name__ == "__main__":
    unittest.main()
