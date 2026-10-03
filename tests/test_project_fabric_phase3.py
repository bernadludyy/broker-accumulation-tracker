from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import Mock

from project_fabric.data_gateway import CachePolicy, DataGateway
from project_fabric.postgres_backend import PostgresTTLCache


@unittest.skipUnless(
    os.getenv("PROJECT_FABRIC_DATABASE_URL", "").strip()
    and os.getenv("PROJECT_FABRIC_INTEGRATION_TESTS", "").strip() == "1",
    "shared PostgreSQL integration requires explicit PROJECT_FABRIC_INTEGRATION_TESTS=1",
)
class Phase3SharedCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.url = os.environ["PROJECT_FABRIC_DATABASE_URL"]
        cls.namespace = f"phase3-test-{os.getenv('GITHUB_RUN_ID', 'local')}"
        cls._previous_namespace = os.environ.get("PROJECT_FABRIC_CACHE_NAMESPACE")
        os.environ["PROJECT_FABRIC_CACHE_NAMESPACE"] = cls.namespace
        cls.admin = PostgresTTLCache(
            cls.url,
            namespace=cls.namespace,
            service="phase3-test-admin",
            auto_migrate=True,
        )
        cls.admin.clear_namespace()

    @classmethod
    def tearDownClass(cls):
        cls.admin.clear_namespace()
        if cls._previous_namespace is None:
            os.environ.pop("PROJECT_FABRIC_CACHE_NAMESPACE", None)
        else:
            os.environ["PROJECT_FABRIC_CACHE_NAMESPACE"] = cls._previous_namespace

    def setUp(self):
        self.admin.clear_namespace()

    def test_two_independent_gateways_share_one_cache(self):
        with tempfile.TemporaryDirectory() as root_a, tempfile.TemporaryDirectory() as root_b:
            first_loader = Mock(return_value={"data": [{"StockCode": "TEST"}]})
            second_loader = Mock(side_effect=AssertionError("second upstream call must not happen"))

            gateway_a = DataGateway(
                "repo-a",
                runtime_dir=root_a,
                run_id="run-a",
            )
            gateway_b = DataGateway(
                "repo-b",
                runtime_dir=root_b,
                run_id="run-b",
            )

            first = gateway_a.get_or_load(
                endpoint="idx:stock-summary",
                params={"length": 5000, "start": 0},
                loader=first_loader,
                policy=CachePolicy(120, 600),
            )
            second = gateway_b.get_or_load(
                endpoint="idx:stock-summary",
                params={"length": 5000, "start": 0},
                loader=second_loader,
                policy=CachePolicy(120, 600),
            )

            self.assertEqual(first, second)
            self.assertEqual(first_loader.call_count, 1)
            self.assertEqual(second_loader.call_count, 0)
            self.assertEqual(gateway_a.cache_backend, "postgres+local")
            self.assertEqual(gateway_b.cache_backend, "postgres+local")
            self.assertEqual(gateway_b.usage()["totals"]["cache_hits"], 1)

    def test_shared_lease_blocks_duplicate_owner_until_release(self):
        cache_a = PostgresTTLCache(
            self.url,
            namespace=self.namespace,
            service="repo-a",
            auto_migrate=True,
        )
        cache_b = PostgresTTLCache(
            self.url,
            namespace=self.namespace,
            service="repo-b",
            auto_migrate=True,
        )

        self.assertTrue(cache_a.acquire_lease("same-key", "owner-a", 20))
        self.assertFalse(cache_b.acquire_lease("same-key", "owner-b", 20))
        cache_a.release_lease("same-key", "owner-a")
        self.assertTrue(cache_b.acquire_lease("same-key", "owner-b", 20))
        cache_b.release_lease("same-key", "owner-b")

    def test_usage_snapshot_can_be_persisted(self):
        cache = PostgresTTLCache(
            self.url,
            namespace=self.namespace,
            service="usage-test",
            auto_migrate=True,
        )
        cache.persist_usage(
            {
                "service": "usage-test",
                "run_id": "phase3-run",
                "started_at": "2026-10-03T00:00:00+07:00",
                "totals": {"operations": 1},
                "endpoints": {"idx:stock-summary": {"operations": 1}},
            }
        )
        status = cache.health_check()
        self.assertTrue(status.reachable)


if __name__ == "__main__":
    unittest.main()
