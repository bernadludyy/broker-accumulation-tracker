from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

import tracker
from project_fabric.data_gateway import DataGateway


class TrackerGatewayTests(unittest.TestCase):
    def test_stock_summary_reuses_fresh_cache(self):
        with tempfile.TemporaryDirectory() as root, patch.object(
            tracker,
            "_GATEWAY",
            DataGateway("broker-test", runtime_dir=root, run_id="test"),
        ), patch.object(tracker, "_headers", return_value={}), patch.object(
            tracker,
            "get_json",
            return_value={"items": [{"StockCode": "TEST"}]},
        ) as fetch:
            params = {"length": 5000, "start": 0}
            first = tracker._get_idx("stock-summary", params)
            second = tracker._get_idx("stock-summary", params)

        self.assertEqual(first, second)
        self.assertEqual(fetch.call_count, 1)

    def test_live_multi_quote_is_not_cached(self):
        with tempfile.TemporaryDirectory() as root, patch.object(
            tracker,
            "_GATEWAY",
            DataGateway("broker-test", runtime_dir=root, run_id="test"),
        ), patch.object(tracker, "_headers", return_value={}), patch.object(
            tracker,
            "get_json",
            return_value={"items": []},
        ) as fetch:
            params = {"codes": "TEST"}
            tracker._get("summary", params)
            tracker._get("summary", params)

        self.assertEqual(fetch.call_count, 2)


if __name__ == "__main__":
    unittest.main()
