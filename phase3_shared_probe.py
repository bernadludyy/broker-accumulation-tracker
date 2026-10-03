from __future__ import annotations

import json
import os

from project_fabric.data_gateway import CachePolicy, DataGateway


SERVICE = os.getenv("PHASE3_PROBE_SERVICE", "powerfull-momentum-bot").strip()
MODE = os.getenv("PHASE3_PROBE_MODE", "consumer").strip().lower()
EXPECTED_PRODUCER = os.getenv("PHASE3_EXPECTED_PRODUCER", "powerfull-momentum-bot").strip()

gateway = DataGateway(SERVICE)
if gateway.cache_backend != "postgres+local":
    raise SystemExit(f"Phase 3 shared backend inactive: {gateway.cache_backend}")

policy = CachePolicy(ttl_seconds=3600, stale_if_error_seconds=3600)

if MODE == "seed":
    def loader():
        return {
            "contract": "project-fabric-phase3-production-v1",
            "producer": SERVICE,
            "shared": True,
        }
else:
    def loader():
        raise AssertionError("shared cache MISS: consumer attempted its own loader")

payload = gateway.get_or_load(
    endpoint="phase3:shared-production-probe",
    params={"contract": "shared-v1", "version": 1},
    loader=loader,
    policy=policy,
)

if payload.get("contract") != "project-fabric-phase3-production-v1":
    raise SystemExit(f"Unexpected shared payload: {payload!r}")
if payload.get("producer") != EXPECTED_PRODUCER:
    raise SystemExit(
        f"Cross-repo cache producer mismatch: expected {EXPECTED_PRODUCER}, got {payload.get('producer')}"
    )

print(json.dumps({
    "phase3": "PASS",
    "service": SERVICE,
    "mode": MODE,
    "backend": gateway.cache_backend,
    "producer": payload.get("producer"),
    "cache_hits": gateway.usage()["totals"].get("cache_hits", 0),
    "upstream_operations": gateway.usage()["totals"].get("upstream_operations", 0),
}, sort_keys=True))
