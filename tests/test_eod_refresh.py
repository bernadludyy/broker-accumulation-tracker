"""Offline tests: Buddy EOD commands never contact the real exchange or Neon."""
import unittest
from datetime import datetime
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from project_fabric.eod_refresh import (
    EODNotReady, _validate, eod_status, format_eod_reply, refresh_eod,
)

WIB = ZoneInfo("Asia/Jakarta")


def sample(date_text="2026-10-14", n=963):
    rows = [
        {"StockCode": f"Z{i:04d}", "Date": date_text + "T00:00:00",
         "OpenPrice": 100, "High": 110, "Low": 90, "Close": 103,
         "Volume": 100000, "Value": 10_000_000}
        for i in range(n)
    ]
    return {"data": {"data": rows, "total": n}}


class EODRefreshTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 14, 18, 0, tzinfo=WIB)
        self.archive = Mock(ready=True)
        self.archive.save_summary.return_value = 963
        self.gateway = Mock()
        self.gateway.get_json.return_value = sample()
        self.gateway.usage.return_value = {"totals": {"upstream_attempts": 1}}

    def test_before_market_close_does_not_read_neon_or_zapi(self):
        with patch("project_fabric.eod_refresh._counts") as get_counts:
            result = refresh_eod(asof=datetime(2026, 10, 14, 16, 59, tzinfo=WIB),
                                 archive=self.archive, gateway=self.gateway)
        self.assertEqual(result["status"], "TOO_EARLY")
        get_counts.assert_not_called()
        self.gateway.get_json.assert_not_called()

    @patch("project_fabric.eod_refresh._counts", return_value=(963, 963))
    def test_completed_eod_is_idempotent_zero_zapi(self, _counts):
        result = refresh_eod(asof=self.now, archive=self.archive, gateway=self.gateway)
        self.assertEqual(result["status"], "CURRENT")
        self.assertEqual(result["zapi_calls"], 0)
        self.gateway.get_json.assert_not_called()
        self.archive.save_summary.assert_not_called()

    @patch("project_fabric.eod_refresh._counts", side_effect=[(0, 963), (963, 963)])
    @patch.dict("os.environ", {"ZPI_API_KEY": "offline-test-key"})
    def test_missing_eod_updates_exact_date_and_canonical_cache(self, _counts):
        result = refresh_eod(asof=self.now, archive=self.archive, gateway=self.gateway)
        self.assertEqual(result["status"], "UPDATED")
        self.assertEqual(result["rows"], 963)
        self.assertEqual(result["zapi_calls"], 1)
        request = self.gateway.get_json.call_args.kwargs
        self.assertEqual(request["params"]["date"], "2026-10-14")
        self.assertEqual(request["policy"].ttl_seconds, 0)
        self.assertEqual(request["policy"].stale_if_error_seconds, 0)
        self.assertTrue(request["validate"](sample()))
        self.archive.save_summary.assert_called_once()
        self.assertEqual(self.archive.save_summary.call_args.kwargs["completed_through"], "2026-10-14")
        self.assertEqual(self.gateway.cache.set.call_count, 2)

    @patch("project_fabric.eod_refresh._counts", return_value=(0, 963))
    @patch.dict("os.environ", {"ZPI_API_KEY": "offline-test-key"})
    def test_stale_source_never_writes_neon(self, _counts):
        def invalid_source(**kwargs):
            return kwargs["validate"](sample("2026-10-13"))
        self.gateway.get_json.side_effect = invalid_source
        with self.assertRaises(EODNotReady):
            refresh_eod(asof=self.now, archive=self.archive, gateway=self.gateway)
        self.archive.save_summary.assert_not_called()
        self.gateway.cache.set.assert_not_called()

    def test_truncated_and_duplicate_snapshot_rejected(self):
        with self.assertRaises(EODNotReady):
            _validate(sample(n=250), self.now.date(), 900)
        bad = sample()
        bad["data"]["data"][1]["StockCode"] = "Z0000"
        with self.assertRaises(EODNotReady):
            _validate(bad, self.now.date(), 900)
        bad = sample()
        bad["data"]["total"] = 1200
        with self.assertRaises(EODNotReady):
            _validate(bad, self.now.date(), 900)

    @patch("project_fabric.eod_refresh._counts", return_value=(0, 963))
    def test_status_is_read_only(self, _counts):
        result = eod_status(asof=self.now, archive=self.archive)
        self.assertEqual(result["status"], "STALE")
        self.assertEqual(result["zapi_calls"], 0)
        self.gateway.get_json.assert_not_called()

    def test_reply_explicitly_separates_bot4_finalization(self):
        msg = format_eod_reply({"status": "UPDATED", "date": "2026-10-14",
                                "rows": 963, "zapi_calls": 1})
        self.assertIn("SETUP RANK", msg)
        self.assertIn("Bot 4", msg)


class TelegramEODRoutingTests(unittest.TestCase):
    @patch("telegram_query_bot.authorized", return_value=True)
    @patch("telegram_query_bot.refresh_eod", return_value={"status": "CURRENT", "date": "2026-10-14", "rows": 963, "zapi_calls": 0})
    def test_refresh_command_reaches_service(self, refresh, _auth):
        from telegram_query_bot import command_result
        result = command_result("/eod refresh", "123")
        self.assertIn("963", result)
        refresh.assert_called_once_with()

    @patch("telegram_query_bot.authorized", return_value=True)
    @patch("telegram_query_bot.eod_status", return_value={"status": "STALE", "date": "2026-10-14", "rows": 0, "zapi_calls": 0})
    def test_status_command_reaches_read_only(self, status, _auth):
        from telegram_query_bot import command_result
        result = command_result("/eod status", "123")
        self.assertIn("belum tersedia", result)
        status.assert_called_once_with()

    @patch("telegram_query_bot.authorized", return_value=False)
    @patch("telegram_query_bot.refresh_eod")
    def test_non_owner_chat_cannot_refresh(self, refresh, _auth):
        from telegram_query_bot import command_result
        self.assertIn("hanya tersedia", command_result("/eod refresh", "-100123"))
        refresh.assert_not_called()

    @patch("telegram_query_bot.authorized", return_value=True)
    def test_invalid_subcommand_is_rejected(self, _auth):
        from telegram_query_bot import command_result
        self.assertIn("Format:", command_result("/eod tomorrow", "123"))


if __name__ == "__main__":
    unittest.main()
