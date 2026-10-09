from __future__ import annotations

import tempfile
import unittest
from unittest.mock import Mock

from project_fabric.data_gateway import CachePolicy, DataGateway, market_policy


class Phase2GatewayTests(unittest.TestCase):
    def gateway(self, root: str) -> DataGateway:
        return DataGateway("phase2-test", runtime_dir=root, run_id="test-run")

    def test_cache_hit_suppresses_second_upstream_operation(self):
        with tempfile.TemporaryDirectory() as root:
            gateway = self.gateway(root)
            loader = Mock(return_value={"value": 1})
            policy = CachePolicy(60, 120)

            first = gateway.get_or_load(
                endpoint="idx:stock-summary",
                params={"length": 5000},
                loader=loader,
                policy=policy,
            )
            second = gateway.get_or_load(
                endpoint="idx:stock-summary",
                params={"length": 5000},
                loader=loader,
                policy=policy,
            )

            self.assertEqual(first, second)
            self.assertEqual(loader.call_count, 1)
            totals = gateway.usage()["totals"]
            self.assertEqual(totals["upstream_operations"], 1)
            self.assertEqual(totals["cache_hits"], 1)

    def test_stale_fallback_is_explicit_and_metered(self):
        with tempfile.TemporaryDirectory() as root:
            gateway = self.gateway(root)
            gateway.get_or_load(
                endpoint="idx:stock-history",
                params={"code": "TEST"},
                loader=lambda: {"rows": [1]},
                policy=CachePolicy(60, 600),
            )

            value = gateway.get_or_load(
                endpoint="idx:stock-history",
                params={"code": "TEST"},
                loader=Mock(side_effect=RuntimeError("temporary outage")),
                policy=CachePolicy(0, 600),
            )

            self.assertEqual(value, {"rows": [1]})
            totals = gateway.usage()["totals"]
            self.assertEqual(totals["stale_hits"], 1)
            self.assertEqual(totals["errors"], 1)

    def test_live_microstructure_endpoints_are_never_cached(self):
        self.assertEqual(market_policy("pluang", "orderbook").ttl_seconds, 0)
        self.assertEqual(market_policy("pluang", "running-trades").ttl_seconds, 0)
        self.assertEqual(market_policy("pluang", "summary").ttl_seconds, 0)

    def test_stock_summary_has_short_bounded_freshness(self):
        policy = market_policy("idx", "stock-summary")
        self.assertEqual(policy.ttl_seconds, 300)
        self.assertEqual(policy.stale_if_error_seconds, 900)


if __name__ == "__main__":
    unittest.main()
