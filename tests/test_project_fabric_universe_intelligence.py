"""Contract tests for the five shared Universe intelligence features."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from project_fabric.data_gateway import DataGateway
from project_fabric.universe_intelligence import (
    HISTORY_ENDPOINT,
    SUMMARY_ENDPOINT,
    SUMMARY_PARAMS,
    _cache_key,
    history_features,
    institutional_flow,
    publish_history,
    publish_summary,
    read_ticker,
)


class FakeCache:
    def __init__(self):
        self.values = {}

    def set(self, key, value):
        self.values[key] = value

    def get(self, key, ttl_seconds):
        return self.values.get(key)


def make_history(count=45):
    rows = []
    start = datetime(2026, 8, 1)
    for i in range(count):
        price = 100 + i * 0.3
        rows.append({
            "Date": (start + timedelta(days=i)).strftime("%Y-%m-%d"),
            "Open": price - 0.5,
            "High": price + 1,
            "Low": price - 1,
            "Close": price,
            "Volume": 100000 + i * 1000,
        })
    return {"data": rows}


ASOF = datetime(2026, 10, 10, 14, 0, tzinfo=timezone(timedelta(hours=7)))


class UniverseIntelligenceTests(unittest.TestCase):
    def test_cache_key_matches_project_fabric_contract(self):
        self.assertEqual(
            _cache_key(SUMMARY_ENDPOINT, SUMMARY_PARAMS),
            DataGateway._cache_key(SUMMARY_ENDPOINT, SUMMARY_PARAMS),
        )

    def test_five_features_computed_and_read_from_shared_cache(self):
        cache = FakeCache()
        published = publish_summary(cache, {"data": [
            {"StockCode": "ABCD", "Close": 120, "Value": 10000000,
             "foreignBuyValue": 2000000, "foreignSellValue": 1000000},
        ]})
        history = publish_history(cache, "ABCD", make_history())
        self.assertEqual(published["count"], 1)
        self.assertEqual(history["history_sessions"], 45)
        combined = read_ticker(cache, "ABCD")
        self.assertTrue(combined["available"])
        self.assertEqual(
            set(combined["scores"]),
            {"accumulation_persistence", "breakout_readiness",
             "institutional_flow", "distribution_risk", "entry_quality"},
        )
        for part in combined["scores"].values():
            self.assertIsNotNone(part["score"])
            self.assertGreaterEqual(part["score"], 0)
            self.assertLessEqual(part["score"], 100)
        self.assertFalse(combined["scores"]["accumulation_persistence"]["broker_confirmed"])

    def test_foreign_shares_estimated_but_not_reported_value(self):
        flow = institutional_flow(
            {"Close": 100, "Value": 1000000, "ForeignBuy": 1000, "ForeignSell": 400}
        )
        self.assertEqual(flow["net_foreign_value_idr"], 60000)
        self.assertEqual(flow["source"], "estimated_from_foreign_shares_x_close")
        self.assertIsNone(flow["broker_concentration"])

    def test_unavailable_does_not_create_false_neutral_score(self):
        flow = institutional_flow({"Close": 100, "Volume": 300})
        self.assertIsNone(flow["score"])
        cache = FakeCache()
        read = read_ticker(cache, "NONE")
        self.assertFalse(read["available"])
        self.assertTrue(all(item["score"] is None for item in read["scores"].values()))

    def test_partial_current_session_excluded(self):
        payload = make_history()
        payload["data"].append({
            "Date": "2026-10-10", "High": 900, "Low": 10, "Close": 500,
            "Volume": 999999999,
        })
        out = history_features("ABCD", payload, asof=ASOF)
        self.assertEqual(out["history_sessions"], 45)
        self.assertLess(out["entry_quality"]["recent_support_idr"], 200)

    def test_short_history_cannot_overwrite_good_history(self):
        cache = FakeCache()
        publish_history(cache, "ABCD", make_history())
        before = cache.values[_cache_key(HISTORY_ENDPOINT, {"ticker": "ABCD"})]
        incomplete = publish_history(cache, "ABCD", {"data": []})
        self.assertEqual(incomplete["history_sessions"], 0)
        self.assertEqual(
            cache.values[_cache_key(HISTORY_ENDPOINT, {"ticker": "ABCD"})], before
        )

    def test_zero_volume_and_bad_foreign_fields_fail_closed(self):
        rows = make_history()["data"]
        for item in rows:
            item["Volume"] = 0
        score = history_features("TEST", {"data": rows}, asof=ASOF)
        self.assertIsNone(score["entry_quality"]["score"])
        self.assertIsNone(
            institutional_flow({
                "Close": 120, "Value": 5000000,
                "foreignBuyValue": -200, "foreignSellValue": 500,
            })["score"]
        )


if __name__ == "__main__":
    unittest.main()
