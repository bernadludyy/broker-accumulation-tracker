# Project Fabric v1

Project Fabric is the shared engineering foundation for this repository and the rest of the bot ecosystem.

## Goals

- Keep existing strategy/business logic intact.
- Standardize configuration parsing, retries, validation, caching, Jakarta time handling, and structured telemetry.
- Prevent uncontrolled retry loops and duplicate infrastructure code.
- Make API usage easier to reduce through TTL caching.
- Prepare a clean migration path from runner-local state/SQLite to a persistent cloud database.
- Add a preflight check to GitHub Actions without changing signal logic.

## Runtime modules

- `project_fabric.core.env_bool/env_int`: strict environment parsing.
- `project_fabric.core.ResilientHttp`: bounded GET/HEAD retries with Retry-After support.
- `project_fabric.core.JsonTTLCache`: atomic TTL cache for stock-summary, metadata, and other reusable payloads.
- `project_fabric.core.in_time_windows`: reusable Asia/Jakarta session guard.
- `project_fabric.core.as_float`: defensive numeric normalization.
- `project_fabric.core.emit`: JSON telemetry with basic secret redaction.
- `project_fabric.core.RunMetrics`: counters for API calls, cache hits, alerts, errors, and elapsed time.
- `python -m project_fabric.selfcheck`: zero-network preflight check.

## Rollout contract

Phase 1 (this PR) is deliberately non-breaking: add the framework, tests, CI, and a workflow preflight check. Existing bot logic remains unchanged.

Phase 2: COMPLETE. Broker/IDX reads are now wrapped by `DataGateway` for endpoint freshness, safe cache reuse and per-run API telemetry. Existing `bot_io.get_json` remains the bounded transport adapter, preserving its tested retry/error behavior. Live quote/summary and broker-summary remain uncached; reference IDX endpoints use bounded TTLs.

Phase 3: shared PostgreSQL/Neon cache backend implemented. Compatible cache keys can be reused across repositories through one `PROJECT_FABRIC_DATABASE_URL`, while SQLite snapshots and local JSON remain resilience layers.

Phase 4: central dashboard/observability can read the persistent run, API-usage, signal, and delivery tables.

## Example

```python
from project_fabric.core import JsonTTLCache, ResilientHttp

http = ResilientHttp(retries=2)
cache = JsonTTLCache()

payload = cache.get("stock-summary", ttl_seconds=60)
if payload is None:
    payload = http.request_json("GET", "https://example.invalid/data")
    cache.set("stock-summary", payload)
```

Do not cache highly time-sensitive quote/orderbook data unless the strategy explicitly defines a very short TTL. Do not automatically retry Telegram POST delivery unless an idempotency mechanism is present.


## Broker Phase 2 freshness contract

- `idx:stock-summary`: 120s fresh TTL; stale fallback 600s after upstream failure.
- `idx:stock-history`: 6h fresh TTL; stale fallback 24h.
- `idx:foreign-flow`: 120s fresh TTL; stale fallback 600s.
- `pluang:broker-summary`: live/no cache.
- `pluang:quote` and `pluang:summary`: live/no cache.
- Telegram delivery is never cached or automatically replayed.
- Existing database snapshots remain the primary reusable daily-data store.


## Phase 3 shared-cache architecture

Daily Broker Update and Telegram Query Worker now support the same shared PostgreSQL cache namespace as the momentum/scalping repositories. Configure the same `PROJECT_FABRIC_DATABASE_URL` secret in every repository and keep `PROJECT_FABRIC_CACHE_NAMESPACE=shared-v1`.

The shared schema contains `cache_entries`, `cache_leases`, and `api_usage_runs`. Broker-summary, live quote/summary, and Telegram delivery remain uncached.
