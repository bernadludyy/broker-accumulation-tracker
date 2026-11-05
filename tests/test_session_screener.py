import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import session_screener as screen
import telegram_query_bot as bot

WIB = ZoneInfo("Asia/Jakarta")


def row(code="TEST", change=2, value=10_000_000_000):
    return dict(StockCode=code, OpenPrice=100, Close=100 + change,
                High=103, Low=99, Value=value, Frequency=1200)


class SessionScreenerTests(unittest.TestCase):
    def test_bsjp_rejects_low_value_and_does_not_invent_missing_baseline(self):
        baseline = {"TEST": (8_000_000_000, 10)}
        self.assertEqual(len(screen.screen_rows([row()], baseline, "BSJP", "LIVE")), 1)
        self.assertEqual(screen.screen_rows([row(value=500_000_000)], baseline, "BSJP", "LIVE"), [])
        self.assertEqual(screen.screen_rows([row()], {}, "BSJP", "LIVE"), [])

    def test_live_bsjp_before_afternoon_does_not_load_anything(self):
        with patch.object(screen, "load_shared_live_market", side_effect=AssertionError("upstream")):
            result = screen.format_session_screener("BSJP", "LIVE", now=datetime(2026, 11, 5, 11, tzinfo=WIB))
        self.assertIn("14.30 WIB", result)

    def test_eod_uses_completed_cache_and_prior_baseline_only(self):
        now = datetime(2026, 11, 5, 18, tzinfo=WIB)
        rows = [row(code=f"A{i:03d}") for i in range(100)]
        with patch.object(screen, "load_cached_market", return_value=(rows, "2026-11-05", "TEST EOD")) as cache, \
             patch.object(screen, "load_baselines", return_value={"A000": (8_000_000_000, 10)}) as baseline, \
             patch.object(screen, "load_shared_live_market", side_effect=AssertionError("live")):
            result = screen.format_session_screener("BSJP", "EOD", now=now)
        cache.assert_called_once_with(now=now, completed_only=True)
        baseline.assert_called_once_with("2026-11-05")
        self.assertIn("A000", result)
        self.assertIn("pantauan sesi berikutnya", result)

    def test_bpjs_live_identifies_proxy_not_30_minute_data(self):
        now = datetime(2026, 11, 5, 10, tzinfo=WIB)
        rows = [row(code=f"A{i:03d}") for i in range(100)]
        with patch.object(screen, "load_shared_live_market", return_value=(rows, "2026-11-05", "TEST LIVE")), \
             patch.object(screen, "load_baselines", return_value={"A000": (8_000_000_000, 10)}):
            result = screen.format_session_screener("BPJS", "LIVE", now=now)
        self.assertIn("30 menit pertama belum tersedia", result)
        self.assertIn("A000", result)

    def test_stale_live_snapshot_is_blocked(self):
        now = datetime(2026, 11, 5, 10, tzinfo=WIB)
        with patch.object(screen, "load_shared_live_market", return_value=([row()], "2026-11-04", "STALE")), \
             patch.object(screen, "load_baselines", side_effect=AssertionError("baseline")):
            result = screen.format_session_screener("BPJS", "LIVE", now=now)
        self.assertIn("bukan sesi hari ini", result)

    def test_telegram_routes_all_four_modes(self):
        with patch.object(bot, "format_session_screener", return_value="SCREEN") as formatter:
            for kind in ("BSJP", "BPJS"):
                for mode in ("LIVE", "EOD"):
                    self.assertEqual(bot.command_result(f"/{kind.lower()} {mode} 7", "123"), "SCREEN")
                    formatter.assert_called_with(kind, mode, 7)
        self.assertIn("Format:", bot.command_result("BSJP EOD 21", "123"))


if __name__ == "__main__":
    unittest.main()
