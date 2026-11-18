import unittest
from dataclasses import replace
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import session_screener as screen
import telegram_query_bot as bot

WIB = ZoneInfo("Asia/Jakarta")
HISTORY = screen.SessionHistory("2026-11-04", 100, 101, 101, 100, 1_000_000, 8e9, 20)


def row(code="TEST", change=2, value=10_000_000_000):
    return dict(StockCode=code, OpenPrice=100, Close=100 + change,
                High=103, Low=99, Value=value, Frequency=1200)


class SessionScreenerTests(unittest.TestCase):
    def test_quality_bsjp_requires_trend_and_relative_volume_and_limits_extension(self):
        item = {"price": 103}
        quote = {"Volume": 1_500_000, "Previous": 100, "Date": "2026-11-05"}
        def evaluate(data=quote, history=HISTORY, price=item):
            return screen.quality_metrics(data, price, history, "BSJP", "LIVE", "2026-11-05")

        self.assertEqual(evaluate(), (3.0, 3.0, 1.5))
        self.assertIsNone(evaluate(dict(quote, Volume=1_499_999)))
        self.assertIsNone(evaluate(history=replace(HISTORY, ma10=99)))
        self.assertIsNone(evaluate(history=replace(HISTORY, ma20=None)))
        self.assertIsNone(evaluate(price={"price": 112}))
        self.assertIsNone(evaluate(dict(quote, Previous=99)))
        self.assertIsNone(evaluate(dict(quote, Date="2026-11-03")))
        self.assertIsNone(evaluate({"Date": "2026-11-04", "Volume": 1_500_000}))
        self.assertEqual(evaluate(dict(quote, Date="2026-11-04")), (3.0, 3.0, 1.5))

    def test_quality_bpjs_does_not_require_full_day_volume_in_the_morning(self):
        quote = {"Volume": 100_000, "Previous": 100, "Date": "2026-11-05"}
        args = (quote, {"price": 102}, HISTORY, "BPJS")
        metrics = screen.quality_metrics(*args, "LIVE", "2026-11-05")
        self.assertEqual(metrics[0], 2.0)
        self.assertEqual(metrics[2], .1)
        self.assertIsNone(screen.quality_metrics(*args, "EOD", "2026-11-05"))
        self.assertIsNone(screen.quality_metrics(
            quote, {"price": 102}, replace(HISTORY, ma5=103), "BPJS", "LIVE", "2026-11-05"))
        self.assertIsNone(screen.quality_metrics(
            quote, {"price": 101}, HISTORY, "BPJS", "LIVE", "2026-11-05"))

    def test_quality_live_reuses_cache_and_respects_morning_window(self):
        when = datetime(2026, 11, 5, 9, 45, tzinfo=WIB)
        rows = [dict(row(code=f"A{i:03d}"), Date="2026-11-05", Volume=100_000)
                for i in range(100)]
        with patch.object(screen.tracker, "peek_shared_stock_summary", return_value=rows) as cache, \
             patch.object(screen, "load_quality_history", return_value={"A000": HISTORY}) as history, \
             patch.object(screen, "load_shared_live_market", side_effect=AssertionError("ZAPI")):
            report = screen.format_session_screener("BPJS", "LIVE", quality=True, now=when)
            self.assertIn("lolos 1", report)
            self.assertIn("volume/rerata harian 20D 0.10x", report)
            self.assertIn("close acuan 2026-11-04", report)
            after_window = screen.format_session_screener(
                "BPJS", "LIVE", quality=True, now=when.replace(hour=11, minute=30))
            self.assertIn("09.30–11.29", after_window)
            cache.assert_called_once_with(max_age_seconds=1200)
            history.assert_called_once_with("2026-11-05")

    def test_quality_live_missing_cache_does_not_call_zapi_or_load_history(self):
        when = datetime(2026, 11, 5, 14, 45, tzinfo=WIB)
        with patch.object(screen.tracker, "peek_shared_stock_summary", return_value=None), \
             patch.object(screen, "load_shared_live_market", side_effect=AssertionError("ZAPI")), \
             patch.object(screen, "load_quality_history", side_effect=AssertionError("history")):
            self.assertIn("maksimal 20 menit belum tersedia",
                          screen.format_session_screener("BSJP", "LIVE", quality=True, now=when))

    def test_quality_eod_delivers_through_production_command_handler(self):
        import broker_ytd_dispatch as dispatch

        rows = [dict(row(code=f"A{i:03d}", change=3), Date="2026-11-05", Volume=1_600_000)
                for i in range(100)]
        update = {"update_id": 7, "message": {
            "chat": {"id": 123, "type": "private"}, "text": "/bsjp@bot EOD QUALITY 7"}}
        with patch.object(bot, "command_result", dispatch.command_result), \
             patch.object(bot, "TELEGRAM_CHAT_ID", "123"), \
             patch.object(bot, "claim_telegram_update", return_value=True), \
             patch.object(bot, "send_telegram") as send, patch.object(bot, "checkpoint_db"), \
             patch.object(screen, "_load_archived_market", return_value=(rows, "2026-11-05")), \
             patch.object(screen, "load_quality_history", return_value={"A000": HISTORY}) as history, \
             patch.object(screen, "load_shared_live_market", side_effect=AssertionError("ZAPI")):
            self.assertEqual(bot.process_telegram_update(update), 0)
            report, chat_id = send.call_args.args
            self.assertEqual(chat_id, "123")
            self.assertIn("BSJP EOD QUALITY | 2026-11-05", report)
            self.assertIn("lolos 1", report)
            self.assertIn("1.60x", report)
            self.assertIn("pantauan sesi berikutnya", report)
            history.assert_called_once_with("2026-11-05")

    def test_quality_cannot_screen_for_groups_or_other_private_chats(self):
        with patch.object(bot, "format_session_screener", side_effect=AssertionError("screen")):
            for configured, caller in (("-100123", "-100123"), ("123", "456"), ("", "123")):
                with patch.object(bot, "TELEGRAM_CHAT_ID", configured):
                    self.assertIn("chat pribadi pemilik", bot.command_result("BPJS LIVE QUALITY 5", caller))
        self.assertIn("Format:", bot.command_result("BSJP EOD QUALITY ²", "123"))

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
        with patch.object(screen, "load_cached_market", return_value=(rows, "2026-11-05", "NEON DAILY EOD SNAPSHOT")) as cache, \
             patch.object(screen, "load_baselines", return_value={"A000": (8_000_000_000, 10)}) as baseline, \
             patch.object(screen, "load_shared_live_market", side_effect=AssertionError("live")):
            result = screen.format_session_screener("BSJP", "EOD", now=now)
        cache.assert_called_once_with(now=now, completed_only=True)
        baseline.assert_called_once_with("2026-11-05")
        self.assertIn("A000", result)
        self.assertIn("pantauan sesi berikutnya", result)

    def test_eod_rejects_unfinalized_local_snapshot(self):
        now = datetime(2026, 11, 5, 18, tzinfo=WIB)
        with patch.object(screen, "load_cached_market", return_value=([row()], "2026-11-05", "LATEST DAILY DATABASE SNAPSHOT")), \
             patch.object(screen, "load_baselines", side_effect=AssertionError("unfinalized")):
            result = screen.format_session_screener("BSJP", "EOD", now=now)
        self.assertIn("arsip final Neon belum tersedia", result)

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
