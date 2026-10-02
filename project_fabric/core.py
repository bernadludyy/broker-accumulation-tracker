from __future__ import annotations

import hashlib
import json
import os
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, time as clock_time
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

import requests

JAKARTA = ZoneInfo("Asia/Jakarta")
_TRUE = {"1", "true", "yes", "y", "on"}
_FALSE = {"0", "false", "no", "n", "off"}
_RETRYABLE = {429, 500, 502, 503, 504}
_SENSITIVE = ("token", "secret", "password", "authorization", "api_key", "apikey")


class UpstreamError(RuntimeError):
    pass


def env_text(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value is None else value.strip()


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(f"{name} must be true/false, 1/0, yes/no, or on/off")


def env_int(name: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    raw = os.getenv(name)
    value = default if raw is None or not raw.strip() else int(raw)
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return value


def as_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        if isinstance(value, str):
            value = value.replace(",", "").strip()
        return float(value)
    except (TypeError, ValueError):
        return default


def now_jakarta() -> datetime:
    return datetime.now(JAKARTA)


def new_run_id(prefix: str = "run") -> str:
    inherited = os.getenv("GITHUB_RUN_ID") or os.getenv("FABRIC_RUN_ID")
    return f"{prefix}-{inherited}" if inherited else f"{prefix}-{uuid.uuid4().hex[:12]}"


def _parse_clock(value: str) -> clock_time:
    hour, minute = value.strip().split(":", 1)
    return clock_time(hour=int(hour), minute=int(minute))


def in_time_windows(now: datetime, windows: str | list[str] | tuple[str, ...]) -> bool:
    if isinstance(windows, str):
        windows = [part.strip() for part in windows.split(",") if part.strip()]
    local = now.astimezone(JAKARTA) if now.tzinfo else now.replace(tzinfo=JAKARTA)
    current = local.time().replace(second=0, microsecond=0)
    for window in windows:
        start_text, end_text = [part.strip() for part in window.split("-", 1)]
        start, end = _parse_clock(start_text), _parse_clock(end_text)
        if start <= end and start <= current <= end:
            return True
        if start > end and (current >= start or current <= end):
            return True
    return False


class JsonTTLCache:
    """Atomic file cache for upstream API payloads."""
    def __init__(self, root: str | Path = ".runtime/cache"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.json"

    def get(self, key: str, ttl_seconds: float) -> Any | None:
        path = self._path(key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - float(payload["saved_at"]) > ttl_seconds:
                return None
            return payload["value"]
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None

    def set(self, key: str, value: Any) -> None:
        path = self._path(key)
        temp = path.with_suffix(".tmp")
        temp.write_text(
            json.dumps({"saved_at": time.time(), "value": value}, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temp.replace(path)


class ResilientHttp:
    """Bounded retry client. Automatic retries are idempotent-only by default."""
    def __init__(
        self,
        *,
        timeout: tuple[float, float] = (5.0, 30.0),
        retries: int = 2,
        backoff: float = 1.0,
        session: requests.Session | None = None,
    ):
        if retries < 0 or retries > 5:
            raise ValueError("retries must be between 0 and 5")
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.session = session or requests.Session()

    def request_json(
        self,
        method: str,
        url: str,
        *,
        retry_methods: Iterable[str] = ("GET", "HEAD"),
        on_attempt: Callable[[int], None] | None = None,
        **kwargs: Any,
    ) -> Any:
        method = method.upper()
        retryable_methods = {item.upper() for item in retry_methods}
        kwargs.setdefault("timeout", self.timeout)

        for attempt in range(self.retries + 1):
            if on_attempt is not None:
                on_attempt(attempt + 1)
            try:
                response = self.session.request(method, url, **kwargs)
            except (requests.Timeout, requests.ConnectionError):
                if method not in retryable_methods or attempt >= self.retries:
                    raise
                time.sleep(self.backoff * (2 ** attempt) + random.random() * 0.2)
                continue

            if response.status_code in _RETRYABLE and method in retryable_methods and attempt < self.retries:
                fallback = self.backoff * (2 ** attempt)
                try:
                    delay = float(response.headers.get("Retry-After", fallback))
                except (TypeError, ValueError):
                    delay = fallback
                response.close()
                time.sleep(max(0.0, min(60.0, delay)) + random.random() * 0.2)
                continue

            try:
                response.raise_for_status()
                payload = response.json()
            except requests.HTTPError as exc:
                raise UpstreamError(f"{method} {url} failed with HTTP {response.status_code}") from exc
            except ValueError as exc:
                raise UpstreamError(f"{method} {url} returned non-JSON content") from exc

            if isinstance(payload, dict) and (payload.get("success") is False or payload.get("ok") is False):
                raise UpstreamError(f"{method} {url} returned an unsuccessful payload")
            return payload

        raise UpstreamError(f"{method} {url} failed")


def emit(event: str, **fields: Any) -> None:
    clean: dict[str, Any] = {}
    for key, value in fields.items():
        clean[key] = "***" if any(part in key.lower() for part in _SENSITIVE) else value
    payload = {
        "ts": now_jakarta().isoformat(timespec="seconds"),
        "event": event,
        **clean,
    }
    print(json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":")), flush=True)


@dataclass
class RunMetrics:
    service: str
    run_id: str
    started: float = field(default_factory=time.monotonic)
    api_calls: int = 0
    cache_hits: int = 0
    alerts: int = 0
    errors: int = 0

    def finish(self, status: str = "ok", **extra: Any) -> None:
        emit(
            "run.finish",
            service=self.service,
            run_id=self.run_id,
            status=status,
            elapsed_seconds=round(time.monotonic() - self.started, 3),
            api_calls=self.api_calls,
            cache_hits=self.cache_hits,
            alerts=self.alerts,
            errors=self.errors,
            **extra,
        )
