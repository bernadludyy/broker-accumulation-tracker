from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .core import JsonTTLCache, ResilientHttp, emit, new_run_id, now_jakarta


@dataclass(frozen=True)
class CachePolicy:
    ttl_seconds: int = 0
    stale_if_error_seconds: int = 0

    def __post_init__(self) -> None:
        if self.ttl_seconds < 0 or self.stale_if_error_seconds < 0:
            raise ValueError("cache TTL values cannot be negative")
        if self.stale_if_error_seconds and self.stale_if_error_seconds < self.ttl_seconds:
            raise ValueError("stale_if_error_seconds must be >= ttl_seconds")


_LIVE = CachePolicy()
_MARKET_POLICIES: dict[tuple[str, str], CachePolicy] = {
    ("idx", "stock-summary"): CachePolicy(120, 600),
    ("idx", "stock-history"): CachePolicy(21_600, 86_400),
    ("idx", "foreign-flow"): CachePolicy(120, 600),
    ("idx", "company-announcements"): CachePolicy(21_600, 86_400),
    ("idx", "top-movers"): CachePolicy(30, 120),
}


def market_policy(family: str, endpoint: str) -> CachePolicy:
    """Central freshness contract. Live quote/orderbook/trades are never cached."""
    key = (family.strip().lower(), endpoint.strip().lstrip("/").lower())
    return _MARKET_POLICIES.get(key, _LIVE)


class ApiUsageMeter:
    def __init__(self, service: str, path: Path, run_id: str):
        self.service = service
        self.path = path
        self.run_id = run_id
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {
            "schema_version": 1,
            "service": service,
            "run_id": run_id,
            "started_at": now_jakarta().isoformat(timespec="seconds"),
            "updated_at": now_jakarta().isoformat(timespec="seconds"),
            "totals": {
                "operations": 0,
                "upstream_operations": 0,
                "upstream_attempts": 0,
                "cache_hits": 0,
                "stale_hits": 0,
                "successes": 0,
                "errors": 0,
            },
            "endpoints": {},
        }
        self._persist()

    def _endpoint(self, name: str) -> dict[str, Any]:
        return self._data["endpoints"].setdefault(
            name,
            {
                "operations": 0,
                "upstream_operations": 0,
                "upstream_attempts": 0,
                "cache_hits": 0,
                "stale_hits": 0,
                "successes": 0,
                "errors": 0,
                "last_latency_ms": None,
            },
        )

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data["updated_at"] = now_jakarta().isoformat(timespec="seconds")
        temp = self.path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(self._data, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        temp.replace(self.path)

    def record_attempt(self, endpoint: str) -> None:
        with self._lock:
            self._data["totals"]["upstream_attempts"] += 1
            self._endpoint(endpoint)["upstream_attempts"] += 1
            self._persist()

    def record(
        self,
        endpoint: str,
        outcome: str,
        *,
        latency_ms: float | None = None,
    ) -> None:
        with self._lock:
            totals = self._data["totals"]
            item = self._endpoint(endpoint)
            totals["operations"] += 1
            item["operations"] += 1
            if outcome == "cache_hit":
                totals["cache_hits"] += 1
                item["cache_hits"] += 1
                totals["successes"] += 1
                item["successes"] += 1
            elif outcome == "stale_hit":
                totals["stale_hits"] += 1
                item["stale_hits"] += 1
                totals["successes"] += 1
                item["successes"] += 1
            elif outcome == "upstream_success":
                totals["upstream_operations"] += 1
                item["upstream_operations"] += 1
                totals["successes"] += 1
                item["successes"] += 1
            elif outcome == "upstream_error":
                totals["upstream_operations"] += 1
                item["upstream_operations"] += 1
                totals["errors"] += 1
                item["errors"] += 1
            else:
                raise ValueError(f"unknown meter outcome: {outcome}")
            if latency_ms is not None:
                item["last_latency_ms"] = round(float(latency_ms), 3)
            self._persist()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._data))


_METERS: dict[tuple[str, str, str], ApiUsageMeter] = {}
_METERS_LOCK = threading.Lock()


def _shared_meter(service: str, path: Path, run_id: str) -> ApiUsageMeter:
    key = (service, str(path.resolve()), run_id)
    with _METERS_LOCK:
        meter = _METERS.get(key)
        if meter is None:
            meter = ApiUsageMeter(service, path, run_id)
            _METERS[key] = meter
        return meter


class DataGateway:
    """One ingress for cache policy, freshness, bounded retries and API metering."""

    def __init__(
        self,
        service: str,
        *,
        runtime_dir: str | Path | None = None,
        http: ResilientHttp | None = None,
        run_id: str | None = None,
    ):
        root = Path(runtime_dir or os.getenv("FABRIC_RUNTIME_DIR", ".runtime/project_fabric"))
        self.service = service
        self.runtime_dir = root
        self.cache = JsonTTLCache(root / "cache")
        self.run_id = run_id or new_run_id(service)
        self.meter = _shared_meter(service, root / "api_usage.json", self.run_id)
        self.http = http or ResilientHttp(retries=2)

    @staticmethod
    def _cache_key(endpoint: str, params: dict[str, Any] | None) -> str:
        safe = json.dumps(params or {}, sort_keys=True, default=str, separators=(",", ":"))
        digest = hashlib.sha256(safe.encode("utf-8")).hexdigest()
        return f"{endpoint}:{digest}"

    def get_or_load(
        self,
        *,
        endpoint: str,
        params: dict[str, Any] | None,
        loader: Callable[[], Any],
        policy: CachePolicy = _LIVE,
        validate: Callable[[Any], bool] | None = None,
        count_loader_attempt: bool = True,
    ) -> Any:
        key = self._cache_key(endpoint, params)

        if policy.ttl_seconds:
            cached = self.cache.get(key, policy.ttl_seconds)
            if cached is not None:
                self.meter.record(endpoint, "cache_hit")
                return cached

        started = time.monotonic()
        if count_loader_attempt:
            self.meter.record_attempt(endpoint)
        try:
            value = loader()
            if validate is not None and not validate(value):
                raise ValueError(f"{endpoint} returned an invalid payload")
        except Exception:
            latency = (time.monotonic() - started) * 1000
            self.meter.record(endpoint, "upstream_error", latency_ms=latency)
            if policy.stale_if_error_seconds:
                stale = self.cache.get(key, policy.stale_if_error_seconds)
                if stale is not None:
                    self.meter.record(endpoint, "stale_hit")
                    emit(
                        "data_gateway.stale_fallback",
                        service=self.service,
                        endpoint=endpoint,
                        max_age_seconds=policy.stale_if_error_seconds,
                    )
                    return stale
            raise

        latency = (time.monotonic() - started) * 1000
        if policy.ttl_seconds or policy.stale_if_error_seconds:
            self.cache.set(key, value)
        self.meter.record(endpoint, "upstream_success", latency_ms=latency)
        return value

    def get_json(
        self,
        *,
        endpoint: str,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        timeout: int | float = 30,
        policy: CachePolicy = _LIVE,
        validate: Callable[[Any], bool] | None = None,
    ) -> Any:
        def loader() -> Any:
            return self.http.request_json(
                "GET",
                url,
                headers=headers or {},
                params=params or {},
                timeout=timeout,
                on_attempt=lambda _attempt: self.meter.record_attempt(endpoint),
            )

        return self.get_or_load(
            endpoint=endpoint,
            params=params,
            loader=loader,
            policy=policy,
            validate=validate,
            count_loader_attempt=False,
        )

    def usage(self) -> dict[str, Any]:
        return self.meter.snapshot()
