from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

try:
    import psycopg
    from psycopg.types.json import Jsonb
except ImportError:  # pragma: no cover - exercised only when optional dependency is absent.
    psycopg = None
    Jsonb = None


class SharedDatabaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class SharedDatabaseStatus:
    configured: bool
    reachable: bool
    namespace: str
    backend: str = "postgres"


def _require_driver() -> None:
    if psycopg is None or Jsonb is None:
        raise SharedDatabaseError(
            "psycopg is required when PROJECT_FABRIC_DATABASE_URL is configured"
        )


class PostgresTTLCache:
    """Shared cross-repository TTL cache backed by PostgreSQL/Neon."""

    def __init__(
        self,
        database_url: str,
        *,
        namespace: str = "shared-v1",
        service: str = "unknown",
        connect_timeout: int = 5,
        auto_migrate: bool = True,
    ):
        _require_driver()
        if not database_url or not database_url.strip():
            raise ValueError("database_url is required")
        self.database_url = database_url.strip()
        self.namespace = namespace.strip() or "shared-v1"
        self.service = service.strip() or "unknown"
        self.connect_timeout = max(1, min(int(connect_timeout), 30))
        if auto_migrate:
            self.migrate()

    def _connect(self):
        _require_driver()
        return psycopg.connect(
            self.database_url,
            connect_timeout=self.connect_timeout,
            autocommit=True,
        )

    def migrate(self) -> None:
        statements = (
            "CREATE SCHEMA IF NOT EXISTS project_fabric",
            """
            CREATE TABLE IF NOT EXISTS project_fabric.cache_entries (
                namespace TEXT NOT NULL,
                cache_key TEXT NOT NULL,
                value_json JSONB NOT NULL,
                source_service TEXT NOT NULL,
                saved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (namespace, cache_key)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_project_fabric_cache_saved_at
            ON project_fabric.cache_entries (saved_at)
            """,
            """
            CREATE TABLE IF NOT EXISTS project_fabric.cache_leases (
                namespace TEXT NOT NULL,
                cache_key TEXT NOT NULL,
                owner TEXT NOT NULL,
                lease_until TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (namespace, cache_key)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_project_fabric_lease_until
            ON project_fabric.cache_leases (lease_until)
            """,
            """
            CREATE TABLE IF NOT EXISTS project_fabric.api_usage_runs (
                service TEXT NOT NULL,
                run_id TEXT NOT NULL,
                started_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                totals JSONB NOT NULL,
                endpoints JSONB NOT NULL,
                payload JSONB NOT NULL,
                PRIMARY KEY (service, run_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS project_fabric.bot_runs (
                service TEXT NOT NULL,
                run_id TEXT NOT NULL,
                workflow TEXT,
                event TEXT,
                ref TEXT,
                sha TEXT,
                status TEXT NOT NULL,
                started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                finished_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                summary JSONB NOT NULL DEFAULT '{}'::jsonb,
                metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                PRIMARY KEY (service, run_id)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_project_fabric_bot_runs_started
            ON project_fabric.bot_runs (started_at DESC)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_project_fabric_bot_runs_status
            ON project_fabric.bot_runs (service, status, updated_at DESC)
            """,
            """
            CREATE TABLE IF NOT EXISTS project_fabric.signal_events (
                event_id TEXT PRIMARY KEY,
                service TEXT NOT NULL,
                run_id TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                entity TEXT,
                score DOUBLE PRECISION,
                status TEXT NOT NULL DEFAULT 'observed',
                occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                payload JSONB NOT NULL DEFAULT '{}'::jsonb
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_project_fabric_signal_recent
            ON project_fabric.signal_events (occurred_at DESC)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_project_fabric_signal_service
            ON project_fabric.signal_events (service, occurred_at DESC)
            """,
            """
            CREATE TABLE IF NOT EXISTS project_fabric.delivery_events (
                event_id TEXT PRIMARY KEY,
                service TEXT NOT NULL,
                run_id TEXT NOT NULL,
                signal_event_id TEXT,
                channel TEXT NOT NULL,
                destination_hash TEXT,
                status TEXT NOT NULL,
                occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                metadata JSONB NOT NULL DEFAULT '{}'::jsonb
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_project_fabric_delivery_recent
            ON project_fabric.delivery_events (occurred_at DESC)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_project_fabric_delivery_service
            ON project_fabric.delivery_events (service, occurred_at DESC)
            """,
            """
            CREATE TABLE IF NOT EXISTS project_fabric.health_checks (
                event_id TEXT PRIMARY KEY,
                service TEXT NOT NULL,
                run_id TEXT NOT NULL,
                component TEXT NOT NULL,
                status TEXT NOT NULL,
                checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                latency_ms DOUBLE PRECISION,
                detail JSONB NOT NULL DEFAULT '{}'::jsonb
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_project_fabric_health_recent
            ON project_fabric.health_checks (service, component, checked_at DESC)
            """,
        )
        try:
            with self._connect() as con:
                with con.cursor() as cur:
                    for statement in statements:
                        cur.execute(statement)
        except Exception as exc:
            raise SharedDatabaseError(
                f"Project Fabric database migration failed ({type(exc).__name__})"
            ) from None

    def health_check(self) -> SharedDatabaseStatus:
        try:
            with self._connect() as con:
                with con.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
            return SharedDatabaseStatus(True, True, self.namespace)
        except Exception:
            return SharedDatabaseStatus(True, False, self.namespace)

    def get(self, key: str, ttl_seconds: float) -> Any | None:
        if ttl_seconds <= 0:
            return None
        try:
            with self._connect() as con:
                with con.cursor() as cur:
                    cur.execute(
                        """
                        SELECT value_json
                        FROM project_fabric.cache_entries
                        WHERE namespace = %s
                          AND cache_key = %s
                          AND saved_at >= now() - make_interval(secs => %s)
                        """,
                        (self.namespace, key, float(ttl_seconds)),
                    )
                    row = cur.fetchone()
            return row[0] if row else None
        except Exception as exc:
            raise SharedDatabaseError(
                f"Shared cache read failed ({type(exc).__name__})"
            ) from None

    def set(self, key: str, value: Any) -> None:
        try:
            with self._connect() as con:
                with con.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO project_fabric.cache_entries (
                            namespace, cache_key, value_json, source_service, saved_at
                        ) VALUES (%s, %s, %s, %s, now())
                        ON CONFLICT (namespace, cache_key) DO UPDATE SET
                            value_json = EXCLUDED.value_json,
                            source_service = EXCLUDED.source_service,
                            saved_at = now()
                        """,
                        (self.namespace, key, Jsonb(value), self.service),
                    )
        except Exception as exc:
            raise SharedDatabaseError(
                f"Shared cache write failed ({type(exc).__name__})"
            ) from None

    def acquire_lease(self, key: str, owner: str, lease_seconds: int = 20) -> bool:
        lease_seconds = max(1, min(int(lease_seconds), 120))
        try:
            with self._connect() as con:
                with con.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO project_fabric.cache_leases (
                            namespace, cache_key, owner, lease_until, updated_at
                        )
                        VALUES (
                            %s, %s, %s,
                            now() + make_interval(secs => %s),
                            now()
                        )
                        ON CONFLICT (namespace, cache_key) DO UPDATE SET
                            owner = EXCLUDED.owner,
                            lease_until = EXCLUDED.lease_until,
                            updated_at = now()
                        WHERE project_fabric.cache_leases.lease_until < now()
                           OR project_fabric.cache_leases.owner = EXCLUDED.owner
                        RETURNING owner
                        """,
                        (self.namespace, key, owner, lease_seconds),
                    )
                    row = cur.fetchone()
            return bool(row and row[0] == owner)
        except Exception as exc:
            raise SharedDatabaseError(
                f"Shared cache lease failed ({type(exc).__name__})"
            ) from None

    def release_lease(self, key: str, owner: str) -> None:
        try:
            with self._connect() as con:
                with con.cursor() as cur:
                    cur.execute(
                        """
                        DELETE FROM project_fabric.cache_leases
                        WHERE namespace = %s AND cache_key = %s AND owner = %s
                        """,
                        (self.namespace, key, owner),
                    )
        except Exception as exc:
            raise SharedDatabaseError(
                f"Shared cache lease release failed ({type(exc).__name__})"
            ) from None

    def clear_namespace(self) -> None:
        """Test/admin helper. Never called by normal bot runtime."""
        try:
            with self._connect() as con:
                with con.cursor() as cur:
                    cur.execute(
                        "DELETE FROM project_fabric.cache_leases WHERE namespace = %s",
                        (self.namespace,),
                    )
                    cur.execute(
                        "DELETE FROM project_fabric.cache_entries WHERE namespace = %s",
                        (self.namespace,),
                    )
        except Exception as exc:
            raise SharedDatabaseError(
                f"Shared cache cleanup failed ({type(exc).__name__})"
            ) from None

    def persist_usage(self, payload: dict[str, Any]) -> None:
        service = str(payload.get("service") or self.service)
        run_id = str(payload.get("run_id") or "unknown")
        started_at = payload.get("started_at")
        totals = payload.get("totals") or {}
        endpoints = payload.get("endpoints") or {}
        try:
            with self._connect() as con:
                with con.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO project_fabric.api_usage_runs (
                            service, run_id, started_at, updated_at,
                            totals, endpoints, payload
                        ) VALUES (
                            %s, %s, %s, now(), %s, %s, %s
                        )
                        ON CONFLICT (service, run_id) DO UPDATE SET
                            started_at = EXCLUDED.started_at,
                            updated_at = now(),
                            totals = EXCLUDED.totals,
                            endpoints = EXCLUDED.endpoints,
                            payload = EXCLUDED.payload
                        """,
                        (
                            service,
                            run_id,
                            started_at,
                            Jsonb(totals),
                            Jsonb(endpoints),
                            Jsonb(payload),
                        ),
                    )
        except Exception as exc:
            raise SharedDatabaseError(
                f"API usage persistence failed ({type(exc).__name__})"
            ) from None


def configured_database_url() -> str:
    return os.getenv("PROJECT_FABRIC_DATABASE_URL", "").strip()
