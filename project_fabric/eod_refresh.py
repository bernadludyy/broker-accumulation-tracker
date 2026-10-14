"""On-demand, bounded IDX EOD snapshot refresh for Buddy / SETUP RANK.

Uses Bot 1's existing ZAPI credential and shared Neon warehouse, with no
cross-repository GitHub token. Bot 4's EOD indicator/entry-plan finalizer remains
independent: this module only publishes a completed stock-summary snapshot.
"""
from __future__ import annotations

from datetime import date, datetime
import os
import re
from typing import Any
from zoneinfo import ZoneInfo

from .data_gateway import CachePolicy, DataGateway
from .market_archive import MarketArchive
from .postgres_backend import configured_database_url
from .universe_intelligence import _items

WIB = ZoneInfo("Asia/Jakarta")
MIN_ROWS = 500
_MIN_PREVIOUS_COVERAGE = 0.95
_CODE = re.compile(r"^[A-Z0-9]{1,15}$")


class EODNotReady(ValueError):
    """Today's completed and sufficiently covered EOD source is not ready."""


def _counts(archive: MarketArchive, target: date) -> tuple[int, int]:
    """(today's rows, previous session rows) from durable Neon warehouse."""
    with archive._connect() as cn, cn.cursor() as cursor:
        cursor.execute(
            """SELECT trade_date, count(*) FROM project_fabric.stock_summary_daily
               WHERE trade_date <= %s
               GROUP BY trade_date ORDER BY trade_date DESC LIMIT 2""",
            (target,),
        )
        dates = [(d, int(n)) for d, n in cursor.fetchall()]
    current = next((n for d, n in dates if d == target), 0)
    previous = next((n for d, n in dates if d < target), 0)
    return current, previous


def _minimum(previous: int) -> int:
    return max(MIN_ROWS, int(previous * _MIN_PREVIOUS_COVERAGE)) if previous else MIN_ROWS


def _validate(payload: Any, target: date, minimum: int) -> int:
    """Reject stale, mixed-date, duplicate or truncated exchange snapshots."""
    rows = _items(payload)
    if len(rows) < minimum or len(rows) >= 5000:
        raise EODNotReady(f"Stock-summary tidak lengkap: {len(rows)} saham, minimal {minimum}")
    codes: set[str] = set()
    for row in rows:
        stamp = str(row.get("Date") or row.get("date") or row.get("TradingDate") or "")[:10]
        code = str(row.get("StockCode") or row.get("stockCode") or row.get("ticker")
                   or row.get("Code") or row.get("code") or "").strip().upper()
        if stamp != target.isoformat():
            raise EODNotReady(f"Sumber EOD belum berisi tanggal {target}")
        if not _CODE.fullmatch(code) or code in codes:
            raise EODNotReady("Kode saham tidak valid atau duplikat")
        codes.add(code)

    current = payload
    for _ in range(6):
        if not isinstance(current, dict):
            break
        for key in ("total", "Total", "totalCount", "TotalCount"):
            if key in current and current[key] is not None:
                try:
                    total = int(current[key])
                except (TypeError, ValueError) as exc:
                    raise EODNotReady("Metadata jumlah saham tidak valid") from exc
                if total != len(rows):
                    raise EODNotReady(f"Snapshot parsial: {len(rows)} dari {total} saham")
        current = current.get("data")
    return len(rows)


def refresh_eod(*, asof: datetime | None = None,
                archive: MarketArchive | None = None,
                gateway: DataGateway | None = None) -> dict[str, Any]:
    """Idempotent manual refresh after 17:00 WIB, at most one fresh ZAPI request."""
    now = (asof or datetime.now(WIB)).astimezone(WIB)
    target = now.date()
    if now.weekday() >= 5:
        return {"status": "CLOSED", "date": target.isoformat(), "reason": "Akhir pekan: tidak ada sesi IHSG"}
    if (now.hour, now.minute) < (17, 0):
        return {"status": "TOO_EARLY", "date": target.isoformat(), "reason": "Gunakan setelah 17.00 WIB"}

    archive = archive or MarketArchive(service="broker-accumulation-tracker-eod")
    if not archive.ready:
        raise RuntimeError("Koneksi Neon EOD belum dikonfigurasi")
    existing, previous = _counts(archive, target)
    minimum = _minimum(previous)
    if existing >= minimum:
        return {"status": "CURRENT", "date": target.isoformat(), "rows": existing, "zapi_calls": 0}

    token = os.getenv("ZPI_API_KEY", "").strip()
    if not token:
        raise RuntimeError("ZPI_API_KEY tidak tersedia")
    gateway = gateway or DataGateway(service="broker-accumulation-tracker-eod")
    params = {"date": target.isoformat(), "length": 5000, "start": 0}
    payload = gateway.get_json(
        endpoint="idx:stock-summary",
        url=os.getenv("ZPI_IDX_BASE_URL", "https://api.zapi.ink/v1/finance:idx").rstrip("/") + "/stock-summary",
        headers={"x-api-key": token, "accept": "application/json"},
        params=params, timeout=30, policy=CachePolicy(),
        validate=lambda data: _validate(data, target, minimum) >= minimum,
    )
    source_rows = _validate(payload, target, minimum)
    archive.save_summary(payload, completed_through=target.isoformat(), asof=now)
    stored, _ = _counts(archive, target)
    if stored < source_rows:
        raise RuntimeError(f"Arsip Neon belum lengkap: {stored}/{source_rows}")

    # Only a validated post-close snapshot may replace the shared raw view.
    gateway.cache.set(DataGateway._cache_key("idx:stock-summary", params), payload)
    gateway.cache.set(
        DataGateway._cache_key("idx:stock-summary", {"length": 5000, "start": 0}), payload
    )
    return {"status": "UPDATED", "date": target.isoformat(), "rows": stored,
            "zapi_calls": gateway.usage()["totals"]["upstream_attempts"]}


def eod_status(*, asof: datetime | None = None,
               archive: MarketArchive | None = None) -> dict[str, Any]:
    """Read-only warehouse status, without ZAPI or scheduling work."""
    now = (asof or datetime.now(WIB)).astimezone(WIB)
    archive = archive or MarketArchive(service="broker-accumulation-tracker-eod")
    if not archive.ready:
        raise RuntimeError("Koneksi Neon EOD belum dikonfigurasi")
    current, prev = _counts(archive, now.date())
    return {"status": "CURRENT" if current >= _minimum(prev) else "STALE",
            "date": now.date().isoformat(), "rows": current, "zapi_calls": 0}


def format_eod_reply(result: dict[str, Any]) -> str:
    status = result["status"]
    date_str = result.get("date") or "N/A"
    if status in {"TOO_EARLY", "CLOSED"}:
        return f"🕓 EOD {date_str} belum diproses\n{result.get('reason', '')}\nZAPI: 0"
    if status == "STALE":
        return (f"⚠️ EOD {date_str} belum tersedia di Neon\n"
                f"Jumlah sementara: {result.get('rows', 0)}\n"
                "Ketik /eod refresh setelah 17.00 WIB.")
    if status in {"CURRENT", "UPDATED"}:
        note = "Baru diperbarui" if status == "UPDATED" else "Sudah tersedia (tidak perlu fetch ulang)"
        return (f"✅ EOD {date_str} — {note}\n"
                f"Stock Summary Neon: {result['rows']} saham\n"
                f"ZAPI: {result.get('zapi_calls', 0)} panggilan\n"
                "Ketik SETUP RANK untuk ranking terbaru.\n"
                "Catatan: indikator/entry-plan EOD Bot 4 difinalisasi terpisah.")
    raise ValueError(f"Unknown EOD status: {status}")
