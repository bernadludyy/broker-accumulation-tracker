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
from .universe_intelligence import _items

WIB = ZoneInfo("Asia/Jakarta")
MIN_ROWS = 500
_MIN_PREVIOUS_COVERAGE = 0.97
_MAX_PREVIOUS_ROW_DROP = 30
_CODE = re.compile(r"^[A-Z0-9]{1,15}$")


class EODNotReady(ValueError):
    """Today's completed and sufficiently covered EOD source is not ready."""


def _latest_session_date(archive: MarketArchive, before: date) -> date | None:
    """Newest observed IDX session before ``before``, without calendar guessing.

    A valid history candle is sufficient evidence that the exchange had a
    session even when the all-market stock-summary snapshot is missing.  This
    lets ``/eod refresh last`` repair that missing summary with one bounded
    exact-date request.
    """
    with archive._connect() as cn, cn.cursor() as cursor:
        cursor.execute(
            """SELECT max(trade_date)
                 FROM (
                       SELECT max(trade_date) AS trade_date
                         FROM project_fabric.stock_summary_daily
                        WHERE trade_date < %s
                       UNION ALL
                       SELECT max(trade_date) AS trade_date
                         FROM project_fabric.stock_history_daily
                        WHERE trade_date < %s
                      ) observed_sessions""",
            (before, before),
        )
        row = cursor.fetchone()
    if not row or row[0] is None:
        return None
    return row[0] if isinstance(row[0], date) else date.fromisoformat(str(row[0])[:10])


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
    """Coverage floor that permits small, legitimate IDX-universe changes.

    Daily stock-summary membership is not perfectly constant (suspensions,
    listings and provider eligibility can change it).  Requiring 99% made a
    provider-declared complete 945-row snapshot fail after a 963-row session.
    Keep the gate bounded: accept at most a 3% or 30-row decrease.  The payload
    checks below still require matching total metadata when the provider sends
    it, and always reject mixed dates, duplicates and malformed stock codes.
    """
    if not previous:
        return MIN_ROWS
    return max(
        MIN_ROWS,
        int(previous * _MIN_PREVIOUS_COVERAGE),
        previous - _MAX_PREVIOUS_ROW_DROP,
    )


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


def _source_session_date(payload: Any, minimum: int = MIN_ROWS) -> date:
    """Read one validated trading date from an undated stock-summary payload."""
    rows = _items(payload)
    stamps = {
        str(row.get("Date") or row.get("date") or row.get("TradingDate") or "")[:10]
        for row in rows
    }
    if len(stamps) != 1 or not next(iter(stamps), ""):
        raise EODNotReady("Sumber stock-summary tidak memiliki satu tanggal sesi yang valid")
    try:
        target = date.fromisoformat(next(iter(stamps)))
    except ValueError as exc:
        raise EODNotReady("Tanggal sesi stock-summary tidak valid") from exc
    _validate(payload, target, minimum)
    return target


def _publish_summary_cache(
    gateway: DataGateway, target: date, payload: Any, *, canonical: bool,
) -> None:
    gateway.cache.set(
        DataGateway._cache_key(
            "idx:stock-summary",
            {"date": target.isoformat(), "length": 5000, "start": 0},
        ),
        payload,
    )
    if canonical:
        gateway.cache.set(
            DataGateway._cache_key(
                "idx:stock-summary", {"length": 5000, "start": 0},
            ),
            payload,
        )


def refresh_eod(*, asof: datetime | None = None,
                archive: MarketArchive | None = None,
                gateway: DataGateway | None = None,
                latest: bool = False) -> dict[str, Any]:
    """Idempotent EOD refresh with at most one ZAPI operation.

    The default mode refreshes today's completed session after 17:00 WIB.
    ``latest=True`` asks the undated stock-summary source for its newest session,
    then compares that date with Neon.  No weekday/holiday calendar is guessed.
    """
    now = (asof or datetime.now(WIB)).astimezone(WIB)
    target = now.date()
    if not latest:
        if now.weekday() >= 5:
            return {"status": "CLOSED", "date": target.isoformat(), "reason": "Akhir pekan: tidak ada sesi IHSG"}
        if (now.hour, now.minute) < (17, 0):
            return {"status": "TOO_EARLY", "date": target.isoformat(), "reason": "Gunakan setelah 17.00 WIB"}

    archive = archive or MarketArchive(service="broker-accumulation-tracker-eod")
    if not archive.ready:
        raise RuntimeError("Koneksi Neon EOD belum dikonfigurasi")

    payload: Any | None = None
    gateway_used = gateway
    params: dict[str, Any] | None = None
    if latest:
        token = os.getenv("ZPI_API_KEY", "").strip()
        if not token:
            raise RuntimeError("ZPI_API_KEY tidak tersedia")
        gateway_used = gateway or DataGateway(service="broker-accumulation-tracker-eod")
        params = {"length": 5000, "start": 0}
        payload = gateway_used.get_json(
            endpoint="idx:stock-summary",
            url=os.getenv("ZPI_IDX_BASE_URL", "https://api.zapi.ink/v1/finance:idx").rstrip("/") + "/stock-summary",
            headers={"x-api-key": token, "accept": "application/json"},
            params=params, timeout=30, policy=CachePolicy(),
            validate=lambda data: bool(_source_session_date(data)),
        )
        source_date = _source_session_date(payload)
        observed = _latest_session_date(archive, now.date())
        if source_date >= now.date():
            # During an active session the undated endpoint can represent today.
            # LAST must remain the newest completed date already observed in Neon.
            if observed is None:
                raise EODNotReady("Sumber terbaru masih sesi berjalan dan Neon belum memiliki sesi sebelumnya")
            target = observed
            payload = None
        elif observed is not None and observed > source_date:
            # Never regress to a provider payload older than a valid Neon candle.
            target = observed
            payload = None
        else:
            target = source_date

    existing, previous = _counts(archive, target)
    minimum = _minimum(previous)
    if existing >= minimum:
        if latest and payload is not None and gateway_used is not None:
            _validate(payload, target, minimum)
            _publish_summary_cache(
                gateway_used, target, payload, canonical=target < now.date(),
            )
        return {"status": "CURRENT", "date": target.isoformat(), "rows": existing,
                "previous_rows": previous,
                "zapi_calls": (gateway_used.usage()["totals"]["upstream_attempts"]
                               if latest and gateway_used is not None else 0),
                "selection": "LAST_SESSION" if latest else "TODAY"}

    if payload is None:
        if latest:
            raise EODNotReady(
                f"Sesi {target} teramati di Neon tetapi summary belum lengkap; "
                "sumber latest tidak menyediakan payload tanggal yang sama"
            )
        token = os.getenv("ZPI_API_KEY", "").strip()
        if not token:
            raise RuntimeError("ZPI_API_KEY tidak tersedia")
        gateway_used = gateway or DataGateway(service="broker-accumulation-tracker-eod")
        params = {"date": target.isoformat(), "length": 5000, "start": 0}
        payload = gateway_used.get_json(
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

    # Exact-date cache is safe for both modes.  Never let LAST_SESSION replace
    # the canonical live view with a historical snapshot during today's trade.
    assert gateway_used is not None and params is not None
    _publish_summary_cache(
        gateway_used, target, payload, canonical=target == now.date() or latest,
    )
    return {"status": "UPDATED", "date": target.isoformat(), "rows": stored,
            "previous_rows": previous,
            "zapi_calls": gateway_used.usage()["totals"]["upstream_attempts"],
            "selection": "LAST_SESSION" if latest else "TODAY"}


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
    selection = result.get("selection")
    mode_note = ("\nMode: LAST SESSION (tanggal dibaca dari sumber ZAPI/Neon)"
                 if selection == "LAST_SESSION" else "")
    if status in {"TOO_EARLY", "CLOSED"}:
        return f"🕓 EOD {date_str} belum diproses\n{result.get('reason', '')}\nZAPI: 0"
    if status == "STALE":
        return (f"⚠️ EOD {date_str} belum tersedia di Neon\n"
                f"Jumlah sementara: {result.get('rows', 0)}\n"
                "Ketik /eod refresh setelah 17.00 WIB.")
    if status in {"CURRENT", "UPDATED"}:
        if status == "UPDATED":
            note = "Baru diperbarui"
        elif selection == "LAST_SESSION":
            note = "Sesi terbaru terverifikasi"
        else:
            note = "Sudah tersedia (tidak perlu fetch ulang)"
        previous = int(result.get("previous_rows") or 0)
        coverage_note = ""
        if previous and result["rows"] < previous:
            coverage = result["rows"] / previous * 100
            coverage_note = (f"\nCoverage tervalidasi: {result['rows']}/{previous} "
                             f"({coverage:.1f}%)")
        return (f"✅ EOD {date_str} — {note}\n"
                f"Stock Summary Neon: {result['rows']} saham\n"
                f"ZAPI: {result.get('zapi_calls', 0)} panggilan{mode_note}"
                f"{coverage_note}\n"
                "Ketik SETUP RANK untuk ranking terbaru.\n"
                "Catatan: indikator/entry-plan EOD Bot 4 difinalisasi terpisah.")
    raise ValueError(f"Unknown EOD status: {status}")
