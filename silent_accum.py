"""Cache-only bottom-area and visible-broker accumulation screener.

The feature combines completed EOD price/value history from Neon with the
visible top-broker rows already stored in Bot 1's SQLite cache.  Broker codes
identify intermediaries, so the result describes repeated visible activity
rather than investor identity or inventory.
"""
from __future__ import annotations

import math
import os
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from bandar_momentum import _id_number, _number, _price, _rupiah, _ticker
from broker_ytd_matrix import BROKER_PATTERN, TICKER_PATTERN
from cache_commands import _expected_eod_date, load_cached_market
from project_fabric.postgres_backend import configured_database_url

try:
    import psycopg
except ImportError:  # pragma: no cover - optional outside GitHub runtime.
    psycopg = None


WIB = ZoneInfo("Asia/Jakarta")
LOOKBACK_SESSIONS = 5
HISTORY_SESSIONS = 61
MAX_LOW_DISTANCE_PCT = float(os.getenv("SILENT_ACCUM_MAX_LOW_DISTANCE_PCT", "10"))
MAX_QUIET_RATIO = float(os.getenv("SILENT_ACCUM_MAX_QUIET_RATIO", "0.75"))
MIN_MEDIAN_VALUE_IDR = float(os.getenv("SILENT_ACCUM_MIN_VALUE", "500000000"))
MIN_BROKER_BUY_DAYS = int(os.getenv("SILENT_ACCUM_MIN_BROKER_BUY_DAYS", "3"))


def _finite_nonnegative(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(parsed) or parsed < 0:
        return None
    return parsed


def _load_price_history(
    tickers: list[str],
    asof: str,
    database_url: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    url = (database_url if database_url is not None else configured_database_url()).strip()
    if not url or psycopg is None:
        raise ValueError("Neon histori harga belum terkonfigurasi")
    if not tickers:
        return {}
    with psycopg.connect(url, connect_timeout=12, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """WITH ranked AS (
                       SELECT ticker, trade_date, open_price, high_price,
                              low_price, close_price, turnover_idr,
                              row_number() OVER (
                                PARTITION BY ticker ORDER BY trade_date DESC
                              ) AS row_number
                       FROM project_fabric.stock_history_daily
                       WHERE ticker = ANY(%s) AND trade_date <= %s
                     )
                     SELECT ticker, trade_date, open_price, high_price,
                            low_price, close_price, turnover_idr
                     FROM ranked
                     WHERE row_number <= %s
                     ORDER BY ticker, trade_date""",
                (tickers, asof, HISTORY_SESSIONS),
            )
            columns = [item.name for item in cursor.description]
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["ticker"]).strip().upper()].append(row)
    return dict(grouped)


def _load_broker_accumulation(
    db_path: str,
    asof: str,
    lookback: int = LOOKBACK_SESSIONS,
) -> dict[str, Any]:
    path = Path(db_path)
    if not path.is_file():
        raise ValueError("Cache broker SQLite belum tersedia")
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        dates = [
            str(row[0])
            for row in connection.execute(
                "SELECT DISTINCT trade_date FROM broker_daily "
                "WHERE trade_date<=? ORDER BY trade_date DESC LIMIT ?",
                (asof, lookback),
            ).fetchall()[::-1]
        ]
        if not dates:
            return {"dates": [], "by_ticker": {}, "invalid_rows": 0}
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(broker_daily)")
        }
        optional = [name for name in ("lots",) if name in columns]
        selected = ",".join(
            ["trade_date", "ticker", "broker", "side", "value", *optional]
        )
        rows = connection.execute(
            f"SELECT {selected} FROM broker_daily "
            "WHERE trade_date BETWEEN ? AND ?",
            (dates[0], dates[-1]),
        ).fetchall()

    staged: dict[tuple[str, str, str], dict[str, float]] = defaultdict(
        lambda: {"buy_value": 0.0, "sell_value": 0.0, "buy_lots": 0.0}
    )
    invalid_keys: set[tuple[str, str, str]] = set()
    invalid_rows = 0
    for row in rows:
        day = str(row["trade_date"] or "")
        ticker = str(row["ticker"] or "").strip().upper()
        broker = str(row["broker"] or "").strip().upper()
        side = str(row["side"] or "").strip().upper()
        value = _finite_nonnegative(row["value"])
        lots = _finite_nonnegative(row["lots"]) if "lots" in optional else None
        key = (ticker, broker, day)
        invalid = (
            day not in dates
            or not TICKER_PATTERN.fullmatch(ticker)
            or not BROKER_PATTERN.fullmatch(broker)
            or side not in {"BUY", "SELL"}
            or value is None
            or ("lots" in optional and (lots is None or lots != int(lots)))
        )
        if invalid:
            invalid_rows += 1
            invalid_keys.add(key)
            continue
        bucket = staged[key]
        bucket["buy_value" if side == "BUY" else "sell_value"] += value
        if side == "BUY" and lots is not None:
            bucket["buy_lots"] += lots

    pairs: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {
            "net_value": 0.0,
            "buy_days": 0,
            "sell_days": 0,
            "observed_days": 0,
            "buy_value": 0.0,
            "buy_lots": 0.0,
        }
    )
    for (ticker, broker, day), values in staged.items():
        if (ticker, broker, day) in invalid_keys:
            continue
        net = values["buy_value"] - values["sell_value"]
        pair = pairs[(ticker, broker)]
        pair["net_value"] += net
        pair["buy_days"] += int(net > 0)
        pair["sell_days"] += int(net < 0)
        pair["observed_days"] += 1
        pair["buy_value"] += values["buy_value"]
        pair["buy_lots"] += values["buy_lots"]

    by_ticker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (ticker, broker), pair in pairs.items():
        if pair["net_value"] <= 0 or pair["buy_days"] < MIN_BROKER_BUY_DAYS:
            continue
        average = None
        if pair["buy_value"] > 0 and pair["buy_lots"] > 0:
            average = pair["buy_value"] / (pair["buy_lots"] * 100)
        by_ticker[ticker].append(
            {
                "broker": broker,
                "net_value": pair["net_value"],
                "buy_days": pair["buy_days"],
                "sell_days": pair["sell_days"],
                "observed_days": pair["observed_days"],
                "avg_buy_price": average,
            }
        )
    for brokers in by_ticker.values():
        brokers.sort(key=lambda row: (-row["buy_days"], -row["net_value"], row["broker"]))
    return {"dates": dates, "by_ticker": dict(by_ticker), "invalid_rows": invalid_rows}


def _price_snapshot(
    history: list[dict[str, Any]],
    market_row: dict[str, Any],
    market_day: str,
) -> dict[str, float] | None:
    by_day: dict[str, dict[str, float]] = {}
    for row in history:
        day = str(row.get("trade_date") or row.get("date") or "")[:10]
        close = _number(row, "close_price", "close", "Close")
        low = _number(row, "low_price", "low", "Low")
        high = _number(row, "high_price", "high", "High")
        value = _number(row, "turnover_idr", "value", "Value")
        if day and close and low and high and value is not None and value >= 0:
            by_day[day] = {"close": close, "low": low, "high": high, "value": value}

    close = _number(market_row, "Close", "close", "Last", "last")
    low = _number(market_row, "Low", "low")
    high = _number(market_row, "High", "high")
    value = _number(
        market_row, "Value", "value", "TradingValue", "TransactionValue", "turnover_idr"
    )
    if close and low and high and value is not None and value >= 0:
        by_day[market_day] = {"close": close, "low": low, "high": high, "value": value}

    points = [by_day[day] for day in sorted(by_day)]
    if len(points) < HISTORY_SESSIONS or market_day not in by_day:
        return None
    points = points[-HISTORY_SESSIONS:]
    prior = points[:-1]
    recent = points[-5:]
    baseline = points[-25:-5]
    if len(prior) < 60 or len(baseline) < 20 or any(row["value"] <= 0 for row in points[-25:]):
        return None

    support = min(row["low"] for row in prior[-60:])
    current = points[-1]
    if support <= 0:
        return None
    median_5 = median(row["value"] for row in recent)
    median_previous_20 = median(row["value"] for row in baseline)
    if median_previous_20 <= 0:
        return None
    confirmation = max(row["high"] for row in prior[-10:])
    return {
        "close": current["close"],
        "low": current["low"],
        "support": support,
        "confirmation": confirmation,
        "distance_low_pct": (current["close"] / support - 1.0) * 100.0,
        "quiet_ratio": median_5 / median_previous_20,
        "median_value_5d": median_5,
        "median_value_20d": median_previous_20,
        "return_1d_pct": (current["close"] / points[-2]["close"] - 1.0) * 100.0,
    }


def _score(snapshot: dict[str, float], broker: dict[str, Any]) -> float:
    distance = max(0.0, snapshot["distance_low_pct"])
    bottom = 30.0 * max(0.0, 1.0 - distance / MAX_LOW_DISTANCE_PCT)
    quiet = 10.0 + 10.0 * max(
        0.0, 1.0 - snapshot["quiet_ratio"] / MAX_QUIET_RATIO
    )
    buy_day_points = 15.0 + 5.0 * max(0, broker["buy_days"] - MIN_BROKER_BUY_DAYS)
    intensity = 100.0 * broker["net_value"] / max(
        snapshot["median_value_5d"] * LOOKBACK_SESSIONS, 1.0
    )
    broker_points = min(35.0, buy_day_points + min(10.0, intensity * 2.0))
    support = 10.0 if snapshot["close"] >= snapshot["support"] else 5.0
    rebound = 5.0 if snapshot["return_1d_pct"] >= 0 else 0.0
    return round(min(100.0, bottom + quiet + broker_points + support + rebound), 1)


def analyze_silent_accumulation(
    market_rows: list[dict[str, Any]],
    histories: dict[str, list[dict[str, Any]]],
    broker_by_ticker: dict[str, list[dict[str, Any]]],
    *,
    market_day: str,
    ticker: str | None = None,
) -> dict[str, Any]:
    """Run the deterministic screening pipeline with external I/O injected."""
    requested = ticker.strip().upper().removesuffix(".JK") if ticker else None
    market: dict[str, dict[str, Any]] = {}
    for row in market_rows:
        if not isinstance(row, dict):
            continue
        code = _ticker(row)
        if code and code not in market and (requested is None or code == requested):
            market[code] = row

    diagnostics = []
    for code, market_row in market.items():
        snapshot = _price_snapshot(histories.get(code, []), market_row, market_day)
        brokers = broker_by_ticker.get(code, [])
        reasons = []
        if snapshot is None:
            reasons.append("histori harga/value 61 sesi belum lengkap")
            diagnostics.append({"ticker": code, "eligible": False, "reasons": reasons})
            continue
        if not brokers:
            reasons.append("belum ada broker net buy ≥3 dari 5 sesi")
        if not 0 <= snapshot["distance_low_pct"] <= MAX_LOW_DISTANCE_PCT:
            reasons.append(f"harga lebih dari {MAX_LOW_DISTANCE_PCT:.0f}% dari low 60 sesi")
        if snapshot["quiet_ratio"] > MAX_QUIET_RATIO:
            reasons.append("value 5 sesi belum lebih sepi dari baseline")
        if snapshot["median_value_5d"] < MIN_MEDIAN_VALUE_IDR:
            reasons.append("median value 5 sesi terlalu kecil")
        if snapshot["close"] < snapshot["support"] * 0.98:
            reasons.append("penutupan sudah menembus area invalidasi")

        broker = brokers[0] if brokers else None
        eligible = not reasons and broker is not None
        status = "BELUM LOLOS"
        score = None
        if broker:
            score = _score(snapshot, broker)
            if eligible and snapshot["close"] > snapshot["confirmation"]:
                status = "REBOUND TERKONFIRMASI"
            elif eligible and broker["buy_days"] >= 4:
                status = "AKUMULASI TERKONFIRMASI"
            elif eligible:
                status = "AKUMULASI AWAL"
        diagnostics.append(
            {
                "ticker": code,
                "eligible": eligible,
                "reasons": reasons,
                "status": status,
                "score": score,
                "snapshot": snapshot,
                "broker": broker,
                "broker_count": len(brokers),
            }
        )

    rows = [row for row in diagnostics if row["eligible"]]
    rows.sort(
        key=lambda row: (
            -float(row["score"] or 0),
            row["snapshot"]["distance_low_pct"],
            row["ticker"],
        )
    )
    return {
        "market_count": len(market),
        "history_valid_count": sum("snapshot" in row for row in diagnostics),
        "broker_coverage": sum(bool(row.get("broker")) for row in diagnostics),
        "diagnostics": diagnostics,
        "rows": rows,
        "requested": requested,
    }


def _percent_ratio(value: float) -> str:
    return f"{_id_number(value * 100.0, 0)}%"


def _format_candidate(row: dict[str, Any], index: int | None = None) -> list[str]:
    prefix = f"{index}. " if index is not None else ""
    snapshot = row["snapshot"]
    broker = row.get("broker")
    score = f"{row['score']:.1f}/100" if row.get("score") is not None else "N/A"
    lines = [
        f"{prefix}{row['ticker']} | Skor {score} | {row['status']}",
        f"Harga {_price(snapshot['close'])} | Support {_price(snapshot['support'])} "
        f"| Jarak low {snapshot['distance_low_pct']:+.2f}%",
        f"Value 5D {_rupiah(snapshot['median_value_5d'])} | "
        f"{_percent_ratio(snapshot['quiet_ratio'])} dari baseline 20D",
    ]
    if broker:
        lines.append(
            f"Broker {broker['broker']} | NB {broker['buy_days']}/5 sesi "
            f"| Net {_rupiah(broker['net_value'])} | Avg Buy {_price(broker['avg_buy_price'])}"
        )
    lines.append(
        f"Konfirmasi close > {_price(snapshot['confirmation'])} | "
        f"Invalidasi close < {_price(snapshot['support'] * 0.98)}"
    )
    return lines


def format_silent_accum(
    db_path: str,
    broker_asof: str,
    *,
    ticker: str | None = None,
    limit: int = 10,
    now: datetime | None = None,
) -> str:
    if not 1 <= int(limit) <= 15:
        return "Format: SILENT ACCUM [1-15|TICKER]"
    now = (now or datetime.now(WIB)).astimezone(WIB)
    try:
        market_rows, market_day, source = load_cached_market(now=now, completed_only=True)
        requested = ticker.strip().upper().removesuffix(".JK") if ticker else None
        market_tickers = sorted({_ticker(row) for row in market_rows if _ticker(row)})
        if requested:
            market_tickers = [requested] if requested in market_tickers else []
        histories = _load_price_history(market_tickers, market_day)
        # Keep broker evidence on or before the EOD price snapshot.  This avoids
        # using future broker rows when the completed market cache is stale.
        aligned_broker_asof = min(broker_asof, market_day)
        broker_report = _load_broker_accumulation(db_path, aligned_broker_asof)
        report = analyze_silent_accumulation(
            market_rows,
            histories,
            broker_report["by_ticker"],
            market_day=market_day,
            ticker=requested,
        )
    except Exception as exc:
        print(f"[SILENT-ACCUM-ERROR] {type(exc).__name__}: {str(exc)[:200]}")
        detail = str(exc) if isinstance(exc, ValueError) else "Data cache/Neon gagal dibaca; coba lagi."
        return f"⏳ SILENT ACCUM — DATA BELUM VALID\n{detail}"

    broker_day = broker_report["dates"][-1] if broker_report["dates"] else "N/A"
    lines = [
        f"🤫 SILENT ACCUM — EOD {market_day}",
        f"Sumber pasar: {source}",
        f"Broker cache sampai: {broker_day}",
        f"Filter: dekat low 60D | value 5D ≤{MAX_QUIET_RATIO * 100:.0f}% baseline "
        f"| median ≥{_rupiah(MIN_MEDIAN_VALUE_IDR)} | "
        f"broker NB ≥{MIN_BROKER_BUY_DAYS}/{LOOKBACK_SESSIONS} sesi",
    ]
    target = _expected_eod_date(now)
    if market_day < target:
        lines.append(f"⚠️ EOD BELUM TERBARU — target {target}, tersedia {market_day}.")
    if broker_day != "N/A" and broker_day < market_day:
        lines.append(f"⚠️ Broker belum sejajar dengan harga: {broker_day} vs {market_day}.")

    if requested:
        lines.append("")
        if not report["diagnostics"]:
            lines.append(f"{requested} tidak ditemukan pada snapshot EOD {market_day}.")
        else:
            detail = report["diagnostics"][0]
            if "snapshot" in detail:
                lines.extend(_format_candidate(detail))
            if detail["reasons"]:
                lines.append("Belum lolos karena: " + "; ".join(detail["reasons"]) + ".")
    else:
        lines.append(
            f"Pasar {report['market_count']} | Histori valid {report['history_valid_count']} "
            f"| Ada broker persisten {report['broker_coverage']} | Lolos {len(report['rows'])}"
        )
        lines.append("")
        selected = report["rows"][: int(limit)]
        if not selected:
            lines.append("Belum ada saham yang memenuhi seluruh filter secara bersamaan.")
        for index, row in enumerate(selected, 1):
            if index > 1:
                lines.append("")
            lines.extend(_format_candidate(row, index))

    lines.extend(
        (
            "",
            "ZAPI: 0 request — seluruh analisis memakai cache EOD.",
            "Broker menunjukkan aktivitas melalui perantara yang terlihat, bukan identitas atau saldo investor.",
            "Gunakan /silent_accum TICKER untuk melihat penyebab lolos/gagal.",
        )
    )
    if broker_report["invalid_rows"]:
        lines.append(
            f"DATA QUALITY: {broker_report['invalid_rows']} baris broker tidak valid dikecualikan."
        )
    return "\n".join(lines)
