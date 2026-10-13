from __future__ import annotations

from datetime import datetime, timedelta
import math
from zoneinfo import ZoneInfo
from typing import Any

import tracker
from project_fabric.postgres_backend import configured_database_url

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None


WIB = ZoneInfo("Asia/Jakarta")
NAMESPACE = "shared-v1"


def _num(row: dict, *keys: str) -> float | None:
    for key in keys:
        value = row.get(key)
        if value in (None, "") or isinstance(value, bool):
            continue
        try:
            parsed = float(str(value).replace(",", ""))
        except (TypeError, ValueError):
            continue
        if math.isfinite(parsed):
            return parsed
    return None


def _ticker(row: dict) -> str:
    return str(
        row.get("ticker")
        or row.get("StockCode")
        or row.get("stockCode")
        or row.get("Code")
        or row.get("code")
        or ""
    ).strip().upper()


def _market_rows_from_payload(payload: Any) -> list[dict]:
    current = payload
    for _ in range(5):
        if isinstance(current, list):
            rows = current
            break
        if not isinstance(current, dict):
            rows = []
            break
        child = current.get("items")
        if not isinstance(child, list):
            child = current.get("data")
        if isinstance(child, list):
            rows = child
            break
        if isinstance(child, dict):
            current = child
            continue
        rows = []
        break

    result = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = _ticker(row)
        if not code or code in seen:
            continue
        seen.add(code)
        result.append(dict(row, ticker=code))
    return result


def _row_date(rows: list[dict]) -> str | None:
    dates = sorted({
        str(
            row.get("Date")
            or row.get("date")
            or row.get("TradingDate")
            or ""
        )[:10]
        for row in rows
        if row.get("Date") or row.get("date") or row.get("TradingDate")
    })
    return dates[-1] if dates else None


def _market_in_progress(now: datetime) -> bool:
    local = now.astimezone(WIB)
    if local.weekday() >= 5:
        return False
    minutes = local.hour * 60 + local.minute
    return 9 * 60 <= minutes <= 15 * 60 + 50


def load_cached_market(*, now: datetime | None = None) -> tuple[list[dict], str, str]:
    """Cache-only market snapshot. Never invokes a ZAPI fetch."""
    now = (now or datetime.now(WIB)).astimezone(WIB)

    if _market_in_progress(now):
        shared = tracker.peek_shared_stock_summary()
        rows = _market_rows_from_payload(shared)
        shared_day = _row_date(rows)
        if rows and shared_day:
            return rows, shared_day, "PROJECT FABRIC SHARED LIVE CACHE"

    candidates: list[tuple[str, int, list[dict], str]] = []

    archived = _load_archived_market(now)
    if archived:
        archived_rows, archived_day = archived
        candidates.append((archived_day, 3, archived_rows, "NEON DAILY EOD SNAPSHOT"))

    local_day = tracker.latest_stock_summary_snapshot_date()
    if local_day:
        local_rows = tracker.load_stock_summary_snapshot(local_day)
        if local_rows:
            # Prefer the completed daily snapshot when dates tie.
            candidates.append(
                (local_day, 2, local_rows, "LATEST DAILY DATABASE SNAPSHOT")
            )

    shared = tracker.peek_shared_stock_summary()
    shared_rows = _market_rows_from_payload(shared)
    if shared_rows:
        shared_day = _row_date(shared_rows)
        if shared_day:
            # Restrict to one actual source date; never invent today's date.
            shared_rows = [row for row in shared_rows if _row_date([row]) == shared_day]
            candidates.append(
                (shared_day, 1, shared_rows, "PROJECT FABRIC SHARED CACHE")
            )

    if candidates:
        day, _priority, rows, source = max(
            candidates,
            key=lambda item: (item[0], item[1]),
        )
        return rows, day, source

    raise ValueError("Belum ada stock-summary cache yang dapat dibaca.")


def _load_archived_market(now: datetime) -> tuple[list[dict], str] | None:
    if not configured_database_url() or psycopg is None:
        return None
    target = _expected_eod_date(now)
    try:
        with _connect() as con:
            with con.cursor() as cur:
                cur.execute("""SELECT raw_json, trade_date
                    FROM project_fabric.stock_summary_daily
                    WHERE trade_date = (SELECT max(trade_date)
                        FROM project_fabric.stock_summary_daily WHERE trade_date <= %s)
                    ORDER BY ticker""", (target,))
                result = cur.fetchall()
        if not result:
            return None
        rows = [dict(row, ticker=_ticker(row)) for row, _ in result]
        day = str(result[0][1])[:10]
        if any(_row_date([row]) != day for row in rows):
            return None
        return rows, day
    except Exception as exc:
        print(f"[CACHE-EOD-WARN] Neon daily read: {type(exc).__name__}")
        return None


def _expected_eod_date(now: datetime) -> str:
    local = now.astimezone(WIB)
    day = local.date()
    if (local.hour, local.minute) < (17, 0):
        day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day.isoformat()


def _market_freshness(day: str, now: datetime | None = None) -> str:
    target = _expected_eod_date(now or datetime.now(WIB))
    if day < target:
        return f"⚠️ EOD BELUM TERBARU — target {target}, tersedia {day}. Bukan data target."
    return ""


def _connect():
    url = configured_database_url()
    if not url:
        raise ValueError("Project Fabric database belum terhubung.")
    if psycopg is None:
        raise ValueError("Driver PostgreSQL tidak tersedia.")
    return psycopg.connect(url, connect_timeout=5, autocommit=True)


def _compact_rupiah(value: Any, *, signed: bool = False) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "N/A"
    sign = ("+" if number > 0 else "-" if number < 0 else "") if signed else ("-" if number < 0 else "")
    amount = abs(number)

    def dec(raw: float) -> str:
        return f"{raw:.2f}".replace(".", ",")

    if amount >= 1_000_000_000_000:
        return f"Rp {sign}{dec(amount / 1_000_000_000_000)} Triliun"
    if amount >= 1_000_000_000:
        return f"Rp {sign}{dec(amount / 1_000_000_000)} Miliar"
    if amount >= 1_000_000:
        return f"Rp {sign}{dec(amount / 1_000_000)} Juta"
    if amount >= 1_000:
        return f"Rp {sign}{dec(amount / 1_000)} Ribu"
    return f"Rp {sign}{amount:,.0f}"


def _pct(value: Any, *, signed: bool = False) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "N/A"
    sign = "+" if signed and number > 0 else ""
    return f"{sign}{number:.2f}%".replace(".", ",")


def _close(row: dict) -> float | None:
    return _num(row, "Close", "close")


def _market_cap(row: dict) -> float | None:
    close = _close(row)
    listed = _num(row, "ListedShares", "listedShares")
    if close is None or close <= 0 or listed is None or listed <= 0:
        return None
    return close * listed


def _net_foreign(row: dict) -> tuple[float | None, bool]:
    direct = _num(row, "netForeignValue", "NetForeignValue")
    buy_value = _num(row, "foreignBuyValue", "ForeignBuyValue")
    sell_value = _num(row, "foreignSellValue", "ForeignSellValue")
    if buy_value is not None and sell_value is not None:
        return buy_value - sell_value, False
    if direct is not None:
        return direct, False
    buy_shares = _num(row, "ForeignBuy", "foreignBuy", "ForeignBuyShares")
    sell_shares = _num(row, "ForeignSell", "foreignSell", "ForeignSellShares")
    close = _close(row)
    if (
        buy_shares is not None
        and sell_shares is not None
        and close is not None
        and close > 0
    ):
        return (buy_shares - sell_shares) * close, True
    return None, False


def _correction(row: dict) -> float | None:
    close = _close(row)
    opening = _num(row, "OpenPrice", "Open", "open")
    if close is None or opening is None or close <= 0 or opening <= 0:
        return None
    return (close / opening - 1.0) * 100.0


def format_top_value(limit: int = 20) -> str:
    rows, day, source = load_cached_market()
    data = []
    for row in rows:
        value = _num(row, "Value", "value", "TransactionValue", "transactionValue")
        if value is None or value <= 0:
            continue
        data.append((value, row))
    data.sort(key=lambda item: (-item[0], _ticker(item[1])))
    selected = data[:max(1, min(int(limit), 50))]

    lines = [
        f"💰 TOP TRANSACTION VALUE — TOP {len(selected)}",
        f"Data: {day}",
        f"Sumber: {source}",
        _market_freshness(day),
        f"Universe cache: {len(rows)} saham",
        "",
    ]
    for i, (value, row) in enumerate(selected, 1):
        frequency = _num(row, "Frequency", "frequency")
        close = _close(row)
        lines += [
            f"{i}. {_ticker(row)}",
            f"→ Value : {_compact_rupiah(value)}",
            f"→ Freq  : {frequency:,.0f}" if frequency is not None else "→ Freq  : N/A",
            f"→ Close : {close:,.0f}" if close is not None else "→ Close : N/A",
            "",
        ]
    return "\n".join(lines)


def format_turnover(limit: int = 20) -> str:
    rows, day, source = load_cached_market()
    data = []
    for row in rows:
        value = _num(row, "Value", "value", "TransactionValue", "transactionValue")
        cap = _market_cap(row)
        if value is None or value <= 0 or cap is None or cap <= 0:
            continue
        turnover = value / cap * 100.0
        data.append((turnover, value, cap, row))
    data.sort(key=lambda item: (-item[0], -item[1], _ticker(item[3])))
    selected = data[:max(1, min(int(limit), 50))]

    lines = [
        f"🔥 TOP TURNOVER VALUE / MARKET CAP — TOP {len(selected)}",
        f"Data: {day}",
        f"Sumber: {source}",
        _market_freshness(day),
        "",
    ]
    for i, (turnover, value, cap, row) in enumerate(selected, 1):
        lines += [
            f"{i}. {_ticker(row)}",
            f"→ Turnover   : {_pct(turnover)}",
            f"→ Value      : {_compact_rupiah(value)}",
            f"→ Market Cap : {_compact_rupiah(cap)}",
            "",
        ]
    return "\n".join(lines)


def format_foreign_dip(limit: int = 20) -> str:
    rows, day, source = load_cached_market()
    data = []
    for row in rows:
        net, estimated = _net_foreign(row)
        correction = _correction(row)
        if (
            net is None
            or net <= 0
            or correction is None
            or not -5.000000001 <= correction <= -0.299999999
        ):
            continue
        data.append((net, correction, estimated, row))
    data.sort(key=lambda item: (-item[0], item[1], _ticker(item[3])))
    selected = data[:max(1, min(int(limit), 50))]

    lines = [
        f"🌍 FOREIGN DIP — TOP {len(selected)}",
        f"Data: {day}",
        f"Sumber: {source}",
        _market_freshness(day),
        "Syarat: Net Foreign > 0 dan koreksi open→close -0,3% s.d. -5%.",
        "",
    ]
    for i, (net, correction, estimated, row) in enumerate(selected, 1):
        star = "*" if estimated else ""
        close = _close(row)
        frequency = _num(row, "Frequency", "frequency")
        lines += [
            f"{i}. {_ticker(row)}",
            f"→ Net Foreign : {_compact_rupiah(net, signed=True)}{star}",
            f"→ Koreksi     : {_pct(correction, signed=True)}",
            f"→ Freq        : {frequency:,.0f}" if frequency is not None else "→ Freq        : N/A",
            f"→ Close       : {close:,.0f}" if close is not None else "→ Close       : N/A",
            "",
        ]
    if any(item[2] for item in selected):
        lines += [
            "* Nilai bertanda * adalah estimasi (Foreign Buy Shares - Foreign Sell Shares) × Close."
        ]
    return "\n".join(lines)


def format_breakout_ready(limit: int = 20) -> str:
    safe_limit = max(1, min(int(limit), 50))
    with _connect() as con:
        with con.cursor() as cur:
            cur.execute(
                """
                WITH latest AS (
                    SELECT DISTINCT ON (value_json->>'ticker')
                        value_json,
                        saved_at
                    FROM project_fabric.cache_entries
                    WHERE namespace = %s
                      AND cache_key LIKE 'universe:intelligence:history:v1:%%'
                      AND value_json->>'ticker' IS NOT NULL
                    ORDER BY value_json->>'ticker', saved_at DESC
                ),
                freshest AS (
                    SELECT MAX(value_json->>'history_last_date') AS day
                    FROM latest
                    WHERE (value_json#>>'{breakout_readiness,score}') IS NOT NULL
                )
                SELECT
                    value_json->>'ticker',
                    value_json->>'history_last_date',
                    (value_json#>>'{breakout_readiness,score}')::double precision,
                    value_json#>>'{breakout_readiness,status}',
                    (value_json#>>'{breakout_readiness,distance_to_prior_20d_high_pct}')::double precision,
                    (value_json#>>'{breakout_readiness,range_contraction_ratio}')::double precision,
                    (value_json#>>'{entry_quality,score}')::double precision,
                    (value_json#>>'{distribution_risk,score}')::double precision
                FROM latest, freshest
                WHERE (value_json#>>'{breakout_readiness,score}') IS NOT NULL
                  AND value_json->>'history_last_date' = freshest.day
                ORDER BY
                    (value_json#>>'{breakout_readiness,score}')::double precision DESC,
                    value_json->>'ticker'
                LIMIT %s
                """,
                (NAMESPACE, safe_limit),
            )
            rows = cur.fetchall()

    fresh_day = rows[0][1] if rows else None
    lines = [
        f"🚀 BREAKOUT READY — TOP {len(rows)}",
        f"History EOD: {fresh_day or 'N/A'}",
        "Status: FRESH EOD ✅" if fresh_day else "Status: HISTORY BELUM TERSEDIA ⚠️",
        "Sumber: Universe Intelligence cache (tanpa ZAPI tambahan)",
        "",
    ]
    for i, row in enumerate(rows, 1):
        ticker, day, score, status, distance, contraction, entry_score, dist_risk = row
        lines += [
            f"{i}. {ticker}",
            f"→ Breakout Readiness : {float(score):.1f}/100",
            f"→ Distance 20D High  : {_pct(distance, signed=True) if distance is not None else 'N/A'}",
            f"→ Range Contraction  : {float(contraction):.2f}x" if contraction is not None else "→ Range Contraction  : N/A",
            f"→ Entry Quality      : {float(entry_score):.1f}/100" if entry_score is not None else "→ Entry Quality      : N/A",
            f"→ Distribution Risk  : {float(dist_risk):.1f}/100" if dist_risk is not None else "→ Distribution Risk  : N/A",
            f"→ History            : {day or 'N/A'} | {status or 'N/A'}",
            "",
        ]
    if not rows:
        lines.append("Belum ada cache history intelligence yang cukup.")
    return "\n".join(lines)


def format_dynamic_liquidity(limit: int = 20) -> str:
    safe_limit = max(1, min(int(limit), 50))
    with _connect() as con:
        with con.cursor() as cur:
            cur.execute(
                """
                WITH latest AS (
                    SELECT DISTINCT ON (entity)
                        entity,
                        occurred_at,
                        payload
                    FROM project_fabric.signal_events
                    WHERE service = 'cuankus-momentum'
                      AND signal_type = 'DYNAMIC_LIQUIDITY_MATCH'
                      AND occurred_at >= date_trunc(
                            'day',
                            now() AT TIME ZONE 'Asia/Jakarta'
                          ) AT TIME ZONE 'Asia/Jakarta'
                    ORDER BY entity, occurred_at DESC
                )
                SELECT entity, occurred_at,
                       payload->>'lane',
                       (payload->>'market_cap')::double precision,
                       (payload->>'transaction_value_ma50')::double precision,
                       (payload->>'dynamic_threshold')::double precision,
                       (payload->>'ma50_to_market_cap_pct')::double precision,
                       payload->>'phase',
                       (payload->>'score')::double precision
                FROM latest
                ORDER BY
                    (payload->>'ma50_to_market_cap_pct')::double precision DESC NULLS LAST,
                    entity
                LIMIT %s
                """,
                (safe_limit,),
            )
            rows = cur.fetchall()

    lines = [
        f"💧 DYNAMIC LIQUIDITY MATCH — TOP {len(rows)}",
        "Sumber: Bot 4 observational events di Neon",
        "Tidak memengaruhi Filter 1/2/PTSN.",
        "",
    ]
    for i, row in enumerate(rows, 1):
        ticker, occurred_at, lane, cap, ma50, threshold, ratio_pct, phase, score = row
        local_ts = occurred_at.astimezone(WIB) if occurred_at else None
        lines += [
            f"{i}. {ticker}",
            f"→ Market Cap : {_compact_rupiah(cap)}",
            f"→ Value MA50 : {_compact_rupiah(ma50)}",
            f"→ Min Tier   : {_compact_rupiah(threshold)}",
            f"→ MA50/MCap  : {_pct(ratio_pct)}",
            f"→ Phase      : {phase or 'N/A'} | Score {float(score):.1f}" if score is not None else f"→ Phase      : {phase or 'N/A'}",
            f"→ Lane/Waktu : {lane or 'N/A'} | {local_ts.strftime('%H:%M WIB') if local_ts else 'N/A'}",
            "",
        ]
    if not rows:
        lines.append("Belum ada saham yang tercatat memenuhi dynamic liquidity hari ini.")
    return "\n".join(lines)


def format_cache_status() -> str:
    now = datetime.now(WIB)
    local_day = tracker.latest_stock_summary_snapshot_date()
    local_rows = tracker.load_stock_summary_snapshot(local_day) if local_day else None

    shared_rows = _market_rows_from_payload(tracker.peek_shared_stock_summary())
    shared_day = _row_date(shared_rows)

    cache_saved = None
    cache_source = None
    intel_saved = None
    intel_count = None
    dynamic_count = None
    eod_status = None
    eod_trade_date = None
    eod_finalized = None
    eod_zapi_calls = None
    eod_saved = None

    try:
        with _connect() as con:
            with con.cursor() as cur:
                cur.execute(
                    """
                    SELECT saved_at, source_service
                    FROM project_fabric.cache_entries
                    WHERE namespace=%s
                      AND cache_key LIKE 'idx:stock-summary:%%'
                    ORDER BY saved_at DESC
                    LIMIT 1
                    """,
                    (NAMESPACE,),
                )
                row = cur.fetchone()
                if row:
                    cache_saved, cache_source = row

                cur.execute(
                    """
                    SELECT saved_at,
                           COALESCE((value_json->>'count')::integer, 0)
                    FROM project_fabric.cache_entries
                    WHERE namespace=%s
                      AND cache_key LIKE 'universe:intelligence:summary:v1:%%'
                    ORDER BY saved_at DESC
                    LIMIT 1
                    """,
                    (NAMESPACE,),
                )
                row = cur.fetchone()
                if row:
                    intel_saved, intel_count = row

                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM project_fabric.signal_events
                    WHERE service='cuankus-momentum'
                      AND signal_type='DYNAMIC_LIQUIDITY_MATCH'
                      AND occurred_at >= date_trunc(
                            'day',
                            now() AT TIME ZONE 'Asia/Jakarta'
                          ) AT TIME ZONE 'Asia/Jakarta'
                    """
                )
                row = cur.fetchone()
                dynamic_count = int(row[0]) if row else 0

                cur.execute(
                    """
                    SELECT
                        value_json->>'status',
                        value_json->>'trade_date',
                        COALESCE((value_json->>'finalized')::integer, 0),
                        COALESCE((value_json->>'zapi_calls')::integer, 0),
                        saved_at
                    FROM project_fabric.cache_entries
                    WHERE namespace=%s
                      AND cache_key LIKE 'universe:intelligence:eod-status:v1:%%'
                    ORDER BY saved_at DESC
                    LIMIT 1
                    """,
                    (NAMESPACE,),
                )
                row = cur.fetchone()
                if row:
                    (
                        eod_status,
                        eod_trade_date,
                        eod_finalized,
                        eod_zapi_calls,
                        eod_saved,
                    ) = row
    except Exception as exc:
        print(f"[CACHE-STATUS-WARN] {type(exc).__name__}: {str(exc)[:160]}")

    def ts(value):
        return value.astimezone(WIB).strftime("%Y-%m-%d %H:%M WIB") if value else "N/A"

    lines = [
        "🗄 CACHE STATUS",
        f"Sekarang: {now.strftime('%Y-%m-%d %H:%M WIB')}",
        "",
        "📊 DAILY SNAPSHOT",
        f"→ Tanggal : {local_day or 'N/A'}",
        f"→ Rows    : {len(local_rows) if local_rows else 0}",
        "",
        "⚡ SHARED STOCK-SUMMARY",
        f"→ Row Date    : {shared_day or 'N/A'}",
        f"→ Rows        : {len(shared_rows)}",
        f"→ Last Save   : {ts(cache_saved)}",
        f"→ Producer    : {cache_source or 'N/A'}",
        "",
        "🧠 UNIVERSE INTELLIGENCE",
        f"→ Last Save : {ts(intel_saved)}",
        f"→ Tickers   : {intel_count if intel_count is not None else 'N/A'}",
        "",
        "🌙 EOD FINALIZER 17:00 WIB",
        f"→ Status    : {eod_status or 'N/A'}",
        f"→ Trade Day : {eod_trade_date or 'N/A'}",
        f"→ Finalized : {eod_finalized if eod_finalized is not None else 'N/A'} ticker",
        f"→ ZAPI Call : {eod_zapi_calls if eod_zapi_calls is not None else 'N/A'}",
        f"→ Last Run  : {ts(eod_saved)}",
        "",
        "💧 DYNAMIC LIQUIDITY",
        f"→ Event hari ini : {dynamic_count if dynamic_count is not None else 'N/A'}",
        "",
        "Semua status di atas hanya membaca cache/database; tidak memanggil ZAPI.",
    ]
    return "\n".join(lines)

