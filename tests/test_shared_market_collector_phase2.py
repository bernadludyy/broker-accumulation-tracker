from __future__ import annotations

import unittest
from unittest.mock import patch

import tracker


class SharedMarketCollectorPhase2Tests(unittest.TestCase):
    def test_shared_cache_wrapper_is_normalized_for_bot1(self):
        raw = {
            "project": "finance:idx:stock-summary",
            "data": {
                "timestamp": "2026-10-09T10:00:00Z",
                "data": [{"StockCode": "TEST", "Close": 100}],
            },
        }

        with patch.object(tracker._GATEWAY, "get_or_load", return_value=raw):
            result = tracker._get_idx(
                "stock-summary",
                {"length": 5000, "start": 0},
            )

        self.assertIsInstance(result, dict)
        self.assertEqual(result["data"][0]["StockCode"], "TEST")

    def test_stock_history_uses_canonical_120_and_trims_locally(self):
        items = [
            {"date": f"2026-10-{day:02d}", "close": day}
            for day in range(30, 0, -1)
        ]
        items.extend(
            {"date": f"2026-09-{day:02d}", "close": day}
            for day in range(30, 0, -1)
        )
        items.extend(
            {"date": f"2026-08-{day:02d}", "close": day}
            for day in range(30, 0, -1)
        )
        items.extend(
            {"date": f"2026-07-{day:02d}", "close": day}
            for day in range(30, 0, -1)
        )
        payload = {
            "project": "finance:idx:stock-history",
            "data": {
                "code": "TEST",
                "count": 120,
                "to": items[0]["date"],
                "from": items[-1]["date"],
                "items": items,
            },
        }

        with patch.object(tracker, "_get_idx", return_value=payload) as get_idx:
            result = tracker.fetch_stock_history("TEST", length=5)

        get_idx.assert_called_once_with(
            "stock-history",
            {"code": "TEST", "length": 120},
        )
        self.assertEqual(result["data"]["count"], 5)
        self.assertEqual(len(result["data"]["items"]), 5)
        self.assertEqual(result["data"]["to"], items[0]["date"])
        self.assertEqual(result["data"]["from"], items[4]["date"])

        # Shared cached object must stay untouched.
        self.assertEqual(payload["data"]["count"], 120)
        self.assertEqual(len(payload["data"]["items"]), 120)

    def test_force_history_refresh_bypasses_then_replaces_shared_cache(self):
        raw = {
            "project": "finance:idx:stock-history",
            "data": {"code": "TRUE", "items": [{"date": "2026-10-28"}]},
        }
        with (
            patch.object(tracker._GATEWAY, "get_json", return_value=raw) as get_json,
            patch.object(tracker._GATEWAY.cache, "set") as cache_set,
            patch.object(tracker, "_headers", return_value={"x-api-key": "test"}),
            patch.object(tracker, "_ARCHIVE") as archive,
        ):
            archive.ready = False
            result = tracker.refresh_stock_history("true", length=120)

        self.assertEqual(result["code"], "TRUE")
        self.assertEqual(get_json.call_args.kwargs["policy"].ttl_seconds, 0)
        self.assertEqual(get_json.call_args.kwargs["policy"].stale_if_error_seconds, 0)
        cache_set.assert_called_once()
        self.assertEqual(cache_set.call_args.args[1], raw)

    def test_foreign_flow_first_page_uses_canonical_length(self):
        with patch.object(tracker, "_get_idx", return_value={"data": []}) as get_idx:
            tracker.fetch_foreign_flow(length=20, start=0)

        get_idx.assert_called_once_with(
            "foreign-flow",
            {"sort": "net", "length": 50, "start": 0},
        )

    def test_foreign_flow_nonzero_start_preserves_requested_page_size(self):
        with patch.object(tracker, "_get_idx", return_value={"data": []}) as get_idx:
            tracker.fetch_foreign_flow(length=20, start=10)

        get_idx.assert_called_once_with(
            "foreign-flow",
            {"sort": "net", "length": 20, "start": 10},
        )


if __name__ == "__main__":
    unittest.main()
