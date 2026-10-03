from __future__ import annotations

import argparse
import hashlib
import json
import os
import uuid
from typing import Any

try:
    import psycopg
    from psycopg.types.json import Jsonb
except ImportError:  # pragma: no cover
    psycopg = None
    Jsonb = None

from .core import new_run_id, now_jakarta
from .postgres_backend import SharedDatabaseError, configured_database_url


_REDACT_KEYS = (
    "token",
    "secret",
    "password",
    "authorization",
    "api_key",
    "apikey",
    "database_url",
)


def _require_driver() -> None:
    if psycopg is None or Jsonb is None:
        raise SharedDatabaseError(
            "psycopg is required when PROJECT_FABRIC_DATABASE_URL is configured"
        )


def _scrub(value: Any, *, depth: int = 0) -> Any:
    if depth > 6:
        return "<truncated>"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            lowered = key_text.lower()
            if any(marker in lowered for marker in _REDACT_KEYS):
                out[key_text] = "<redacted>"
            else:
                out[key_text] = _scrub(item, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_scrub(item, depth=depth + 1) for item in list(value)[:100]]
    if isinstance(value, str):
        return value if len(value) <= 1000 else value[:997] + "..."
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:1000]


def _destination_hash(value: str | None) -> str | None:
    if not value:
        return None
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()
    return digest[:16]


def _default_run_id(service: str) -> str:
    inherited = os.getenv("GITHUB_RUN_ID") or os.getenv("FABRIC_RUN_ID")
    if inherited:
        return f"{service}-{inherited}"
    return new_run_id(service)


class ObservabilityClient:
    """Phase 4 durable run, signal, delivery and health observability."""

    def __init__(
        self,
        service: str,
        *,
        database_url: str | None = None,
        run_id: str | None = None,
        strict: bool | None = None,
        connect_timeout: int = 5,
        auto_migrate: bool = True,
    ):
        self.service = service.strip() or "unknown"
        self.run_id = run_id or _default_run_id(self.service)
        self.database_url = (
            database_url if database_url is not None else configured_database_url()
        ).strip()
        if strict is None:
            strict = os.getenv("PROJECT_FABRIC_DB_REQUIRED", "").strip().lower() in {
                "1", "true", "yes", "on"
            }
        self.strict = bool(strict)
        self.connect_timeout = max(1, min(int(connect_timeout), 30))
        if self.database_url:
            _require_driver()
            if auto_migrate:
                self.migrate()
        elif self.strict:
            raise SharedDatabaseError("PROJECT_FABRIC_DATABASE_URL is required")

    @property
    def enabled(self) -> bool:
        return bool(self.database_url)

    def _connect(self):
        _require_driver()
        return psycopg.connect(
            self.database_url,
            connect_timeout=self.connect_timeout,
            autocommit=True,
        )

    def _handle(self, message: str, exc: Exception):
        if self.strict:
            raise SharedDatabaseError(
                f"{message} ({type(exc).__name__})"
            ) from None
        print(
            json.dumps(
                {
                    "project_fabric_observability": "degraded",
                    "service": self.service,
                    "operation": message,
                    "error_type": type(exc).__name__,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return None

    def migrate(self) -> None:
        if not self.enabled:
            return
        statements = (
            "CREATE SCHEMA IF NOT EXISTS project_fabric",
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
            self._handle("phase4 migration failed", exc)

    def start_run(
        self,
        *,
        workflow: str | None = None,
        event: str | None = None,
        ref: str | None = None,
        sha: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        if not self.enabled:
            return self.run_id
        try:
            with self._connect() as con:
                with con.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO project_fabric.bot_runs (
                            service, run_id, workflow, event, ref, sha, status,
                            started_at, updated_at, metadata
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, 'running',
                            now(), now(), %s
                        )
                        ON CONFLICT (service, run_id) DO UPDATE SET
                            workflow = EXCLUDED.workflow,
                            event = EXCLUDED.event,
                            ref = EXCLUDED.ref,
                            sha = EXCLUDED.sha,
                            status = 'running',
                            started_at = LEAST(
                                project_fabric.bot_runs.started_at,
                                EXCLUDED.started_at
                            ),
                            finished_at = NULL,
                            updated_at = now(),
                            metadata = EXCLUDED.metadata
                        """,
                        (
                            self.service,
                            self.run_id,
                            workflow,
                            event,
                            ref,
                            sha,
                            Jsonb(_scrub(metadata or {})),
                        ),
                    )
        except Exception as exc:
            self._handle("run start failed", exc)
        return self.run_id

    def finish_run(
        self,
        status: str,
        *,
        summary: dict[str, Any] | None = None,
    ) -> None:
        if not self.enabled:
            return
        normalized = str(status or "unknown").strip().lower()
        try:
            with self._connect() as con:
                with con.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO project_fabric.bot_runs (
                            service, run_id, status, started_at, finished_at,
                            updated_at, summary
                        ) VALUES (
                            %s, %s, %s, now(), now(), now(), %s
                        )
                        ON CONFLICT (service, run_id) DO UPDATE SET
                            status = EXCLUDED.status,
                            finished_at = now(),
                            updated_at = now(),
                            summary = EXCLUDED.summary
                        """,
                        (
                            self.service,
                            self.run_id,
                            normalized,
                            Jsonb(_scrub(summary or {})),
                        ),
                    )
        except Exception as exc:
            self._handle("run finish failed", exc)

    def record_signal(
        self,
        *,
        signal_type: str,
        entity: str | None = None,
        score: float | int | None = None,
        status: str = "observed",
        payload: dict[str, Any] | None = None,
        event_id: str | None = None,
    ) -> str | None:
        if not self.enabled:
            return None
        signal_id = event_id or uuid.uuid4().hex
        numeric_score = None if score is None else float(score)
        try:
            with self._connect() as con:
                with con.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO project_fabric.signal_events (
                            event_id, service, run_id, signal_type,
                            entity, score, status, payload
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (event_id) DO NOTHING
                        """,
                        (
                            signal_id,
                            self.service,
                            self.run_id,
                            str(signal_type),
                            None if entity is None else str(entity),
                            numeric_score,
                            str(status),
                            Jsonb(_scrub(payload or {})),
                        ),
                    )
            return signal_id
        except Exception as exc:
            self._handle("signal persistence failed", exc)
            return None

    def record_delivery(
        self,
        *,
        channel: str,
        status: str,
        signal_event_id: str | None = None,
        destination: str | None = None,
        metadata: dict[str, Any] | None = None,
        event_id: str | None = None,
    ) -> str | None:
        if not self.enabled:
            return None
        delivery_id = event_id or uuid.uuid4().hex
        try:
            with self._connect() as con:
                with con.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO project_fabric.delivery_events (
                            event_id, service, run_id, signal_event_id,
                            channel, destination_hash, status, metadata
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (event_id) DO NOTHING
                        """,
                        (
                            delivery_id,
                            self.service,
                            self.run_id,
                            signal_event_id,
                            str(channel),
                            _destination_hash(destination),
                            str(status),
                            Jsonb(_scrub(metadata or {})),
                        ),
                    )
            return delivery_id
        except Exception as exc:
            self._handle("delivery persistence failed", exc)
            return None

    def record_health(
        self,
        *,
        component: str,
        status: str,
        latency_ms: float | int | None = None,
        detail: dict[str, Any] | None = None,
        event_id: str | None = None,
    ) -> str | None:
        if not self.enabled:
            return None
        health_id = event_id or uuid.uuid4().hex
        latency = None if latency_ms is None else float(latency_ms)
        try:
            with self._connect() as con:
                with con.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO project_fabric.health_checks (
                            event_id, service, run_id, component,
                            status, latency_ms, detail
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (event_id) DO NOTHING
                        """,
                        (
                            health_id,
                            self.service,
                            self.run_id,
                            str(component),
                            str(status),
                            latency,
                            Jsonb(_scrub(detail or {})),
                        ),
                    )
            return health_id
        except Exception as exc:
            self._handle("health persistence failed", exc)
            return None


def _client(args: argparse.Namespace) -> ObservabilityClient:
    return ObservabilityClient(
        args.service,
        run_id=args.run_id or None,
        strict=bool(args.require),
    )


def _main() -> int:
    parser = argparse.ArgumentParser(description="Project Fabric Phase 4 observability")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--service", required=True)
        p.add_argument("--run-id", default="")
        p.add_argument("--require", action="store_true")

    start = sub.add_parser("run-start")
    common(start)
    start.add_argument("--workflow", default=os.getenv("GITHUB_WORKFLOW", ""))
    start.add_argument("--event", default=os.getenv("GITHUB_EVENT_NAME", ""))
    start.add_argument("--ref", default=os.getenv("GITHUB_REF", ""))
    start.add_argument("--sha", default=os.getenv("GITHUB_SHA", ""))

    finish = sub.add_parser("run-finish")
    common(finish)
    finish.add_argument("--status", required=True)

    health = sub.add_parser("health")
    common(health)
    health.add_argument("--component", required=True)
    health.add_argument("--status", required=True)

    migrate = sub.add_parser("migrate")
    common(migrate)

    args = parser.parse_args()
    client = _client(args)

    if args.command == "run-start":
        client.start_run(
            workflow=args.workflow or None,
            event=args.event or None,
            ref=args.ref or None,
            sha=args.sha or None,
        )
    elif args.command == "run-finish":
        client.finish_run(args.status)
    elif args.command == "health":
        client.record_health(component=args.component, status=args.status)
    elif args.command == "migrate":
        client.migrate()

    print(
        json.dumps(
            {
                "phase4": "ok",
                "service": client.service,
                "run_id": client.run_id,
                "database": "configured" if client.enabled else "disabled",
                "command": args.command,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
