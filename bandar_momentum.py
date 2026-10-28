"""Bandar + momentum screener for Telegram Bot 1.

The command deliberately separates three data domains:

* the current/EOD stock-summary supplies price and transaction value;
* Neon price history supplies reproducible EMA, RSI and return calculations;
* the Bot 1 SQLite cache supplies visible broker accumulation evidence.

The broker score is not Stockbit's proprietary Bandar Accum/Dist value.  It is
Bot 1's existing dominant-broker persistence score and is labelled accordingly.
No broker data is inferred for tickers that are absent from the SQLite cache.
"""
from __future__ import annotations

import math
import os
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pandas as pd

import tracker
from bot2_entry import load_shared_live_market
from cache_commands import _expected_eod_date, load_cached_market
from project_fabric.postgres_backend import configured_database_url

try:
    import psycopg
except ImportError:  # pragma: no cover - optional outside GitHub runtime.
    psycopg = None


WIB = ZoneInfo("Asia/Jakarta")
MIN_VALUE_IDR = float(os.getenv("BANDAR_MOMENTUM_MIN_VALUE", "3000000000"))
MIN_EMA_GAP_PCT = float(os.getenv("BANDAR_MOMENTUM_MIN_EMA_GAP_PCT", "1.2"))
MIN_RS_RATING = float(os.getenv("BANDAR_MOMENTUM_MIN_RS_RATING", "70"))
MIN_RSI14 = float(os.getenv("BANDAR_MOMENTUM_MIN_RSI14", "50"))
MIN_RETURN_1D_PCT = float(os.getenv("BANDAR_MOMENTUM_MIN_RETURN_1D_PCT", "5"))
MIN_RETURN_1M_PCT = float(os.getenv("BANDAR_MOMENTUM_MIN_RETURN_1M_PCT", "0"))
MIN_BANDAR_SCORE = float(os.getenv("BANDAR_MOMENTUM_MIN_BANDAR_SCORE", "20"))
HISTORY_LIMIT = max(25, int(os.getenv("BANDAR_MOMENTUM_HISTORY_LIMIT", "40")))


def _number(row: dict, *keys: str) -> float | None:
    for key in keys:
        value = row.get(key)
        if value in (None, "") or isinstance(value, bool):
            continue
        try:
            result = float(str(value).replace(",", "").strip())
        except (TypeError, ValueError):
            continue
        if math.isfinite(result):
            return result
    return None


def _ticker(row: dict) -> str:
    return str(
        row.get("ticker")
        or row.get("StockCode")
        or row.get("stockCode")
        or row.get("code")
        or ""
    ).strip().upper()


def _market_in_progress(now: datetime) -> bool:
    local = now.astimezone(WIB)
    if local.weekday() >= 5:
        return False
    minute = local.hour * 60 + local.minute
    return 9 * 60 <= minute <= 15 * 60 + 50


def _broker_tickers(db_path: str, asof: str) -> set[str]:
    path = Path(db_path)
    if not path.is_file():
        raise ValueError("Cache broker SQLite belum tersedia")
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        rows = connection.execute(
            "SELECT DISTINCT ticker FROM broker_daily "
            "WHERE trade_date<=? ORDER BY ticker",
            (asof,),
        ).fetchall()
    return {str(row[0]).strip().upper() for row in rows if row and row[0]}


def _load_price_history(
    tickers: list[str],
    asof: str,
    database_url: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    url = (
        database_url if database_url is not None else configured_database_url()
    ).strip()
    if not url or psycopg is None:
        raise ValueError("Neon histori harga belum terkonfigurasi")
    if not tickers:
        return {}
    with psycopg.connect(
        url, connect_timeout=12, autocommit=True
    ) as connection, connection.cursor() as cursor:
        cursor.execute(
            """WITH ranked AS (
                   SELECT ticker, trade_date, close_price,
                          row_number() OVER (
                            PARTITION BY ticker ORDER BY trade_date DESC
                          ) AS row_number
                   FROM project_fabric.stock_history_daily
                   WHERE ticker = ANY(%s) AND trade_date <= %s
                 )
                 SELECT ticker, trade_date, close_price
                 FROM ranked
                 WHERE row_number <= %s
                 ORDER BY ticker, trade_date""",
            (tickers, asof, HISTORY_LIMIT),
        )
        columns = [item.name for item in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["ticker"]).strip().upper()].append(row)
    return dict(grouped)


def _technical_snapshot(
    history: list[dict[str, Any]],
    market_row: dict[str, Any],
    market_day: str,
) -> dict[str, float] | None:
    points: list[tuple[str, float]] = []
    for row in history:
        day = str(row.get("trade_date") or row.get("date") or "")[:10]
        close = _number(row, "close_price", "close", "Close")
        if day and close is not None and close > 0:
            points.append((day, close))

    close = _number(market_row, "Close", "close", "Last", "last")
    if close is None or close <= 0:
        return None
    # The live endpoint may retain yesterday's embedded Date.  The loader's
    # explicit market_day is authoritative for the active session.
    points.append((market_day, close))

    by_day = {day: value for day, value in sorted(points)}
    ordered = sorted(by_day.items())
    if len(ordered) < 22 or ordered[-1][0] != market_day:
        return None
    frame = pd.DataFrame(ordered, columns=["date", "close"])
    frame["ema20"] = frame["close"].ewm(span=20, adjust=False).mean()
    delta = frame["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    relative = avg_gain / avg_loss.replace(0, float("nan"))
    frame["rsi14"] = 100 - (100 / (1 + relative))
    frame.loc[(avg_loss == 0) & (avg_gain > 0), "rsi14"] = 100
    frame["rsi14"] = frame["rsi14"].fillna(50)

    latest = frame.iloc[-1]
    previous_close = float(frame.iloc[-2]["close"])
    month_base = float(frame.iloc[-22]["close"])
    ema20 = float(latest["ema20"])
    return {
        "close": close,
        "ema20": ema20,
        "ema_gap_pct": (close / ema20 - 1.0) * 100.0,
        "rsi14": float(latest["rsi14"]),
        "return_1d_pct": (close / previous_close - 1.0) * 100.0,
        "return_1m_pct": (close / month_base - 1.0) * 100.0,
    }


def _add_rs_ratings(rows: list[dict[str, Any]]) -> None:
    """Assign a deterministic 1-99 percentile across valid IDX histories."""
    ordered = sorted(rows, key=lambda row: (row["return_1m_pct"], row["ticker"]))
    count = len(ordered)
    for index, row in enumerate(ordered):
        row["rs_rating"] = (
            99.0 if count == 1 else 1.0 + (98.0 * index / (count - 1))
        )


def _default_broker_lookup(
    ticker: str,
    broker_asof: str,
    db_path: str,
) -> dict[str, Any] | None:
    frame = tracker.broker_dominance_score(
        ticker=ticker,
        end_date=broker_asof,
        top_n=5,
        db_path=db_path,
    )
    if frame is None or frame.empty:
        return None
    row = frame.iloc[0]
    return {
        "score": float(row.get("dominance_score") or 0.0),
        "broker": str(row.get("broker") or "N/A"),
        "horizon_count": int(row.get("horizon_count") or 0),
        "net_lots_20d": float(row.get("net_lots_20d") or 0.0),
        "avg_buy_20d": (
            float(row["avg_buy_20d"])
            if row.get("avg_buy_20d") is not None
            and math.isfinite(float(row["avg_buy_20d"]))
            else None
        ),
    }


def analyze_bandar_momentum(
    market_rows: list[dict[str, Any]],
    histories: dict[str, list[dict[str, Any]]],
    *,
    market_day: str,
    broker_tickers: set[str],
    broker_lookup: Callable[[str], dict[str, Any] | None],
) -> dict[str, Any]:
    """Run the pure screening pipeline; external I/O is injected by callers."""
    market: dict[str, dict[str, Any]] = {}
    for raw in market_rows:
        if not isinstance(raw, dict):
            continue
        code = _ticker(raw)
        if code and code not in market:
            market[code] = raw

    technical_rows: list[dict[str, Any]] = []
    for code, market_row in market.items():
        snapshot = _technical_snapshot(histories.get(code, []), market_row, market_day)
        value = _number(
            market_row,
            "Value",
            "value",
            "TradingValue",
            "TransactionValue",
            "turnover_idr",
        )
        if snapshot is None or value is None or value < 0:
            continue
        technical_rows.append({"ticker": code, "value": value, **snapshot})

    _add_rs_ratings(technical_rows)
    prefiltered = [
        row
        for row in technical_rows
        if row["value"] >= MIN_VALUE_IDR
        and row["ema_gap_pct"] >= MIN_EMA_GAP_PCT
        and row["rs_rating"] >= MIN_RS_RATING
        and row["rsi14"] > MIN_RSI14
        and row["return_1d_pct"] > MIN_RETURN_1D_PCT
        and row["return_1m_pct"] > MIN_RETURN_1M_PCT
    ]

    selected: list[dict[str, Any]] = []
    broker_errors = 0
    for row in prefiltered:
        if row["ticker"] not in broker_tickers:
            continue
        try:
            broker = broker_lookup(row["ticker"])
        except Exception:
            broker_errors += 1
            continue
        if not broker or float(broker.get("score") or 0.0) < MIN_BANDAR_SCORE:
            continue
        selected.append({**row, "bandar": broker})

    selected.sort(
        key=lambda row: (
            -float(row["bandar"]["score"]),
            -row["rs_rating"],
            -row["value"],
            row["ticker"],
        )
    )
    return {
        "market_count": len(market),
        "technical_count": len(technical_rows),
        "broker_coverage": len(broker_tickers & set(market)),
        "prefilter_count": len(prefiltered),
        "broker_errors": broker_errors,
        "rows": selected,
    }


def _id_number(value: float, digits: int = 1) -> str:
    text = f"{float(value):,.{digits}f}"
    return text.translate(str.maketrans({",": ".", ".": ","}))


def _rupiah(value: float) -> str:
    amount = abs(float(value))
    sign = "-" if value < 0 else ""
    for threshold, suffix in ((1e12, "triliun"), (1e9, "miliar"), (1e6, "juta")):
        if amount >= threshold:
            return f"Rp{sign}{_id_number(amount / threshold, 2)} {suffix}"
    return f"Rp{sign}{_id_number(amount, 0)}"


def _price(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"Rp{_id_number(value, 0)}"


def format_bandar_momentum(
    db_path: str,
    broker_asof: str,
    *,
    mode: str = "EOD",
    limit: int = 10,
    now: datetime | None = None,
) -> str:
    normalized_mode = str(mode).strip().upper()
    if normalized_mode not in {"EOD", "LIVE"} or not 1 <= int(limit) <= 15:
        return "Format: BANDAR MOMENTUM [EOD|LIVE] [1-15]"
    now = (now or datetime.now(WIB)).astimezone(WIB)
    if normalized_mode == "LIVE" and not _market_in_progress(now):
        return "\n".join((
            "⌛ BANDAR MOMENTUM LIVE — HANYA JAM BURSA",
            "Gunakan BANDAR MOMENTUM atau BANDAR MOMENTUM EOD di luar jam bursa.",
            "Tidak ada request ZAPI yang digunakan.",
        ))

    try:
        if normalized_mode == "LIVE":
            market_rows, market_day, source = load_shared_live_market(
                with_source=True, now=now
            )
        else:
            market_rows, market_day, source = load_cached_market(
                now=now, completed_only=True
            )
        market_tickers = sorted({_ticker(row) for row in market_rows if _ticker(row)})
        histories = _load_price_history(market_tickers, market_day)
        broker_universe = _broker_tickers(db_path, broker_asof)
        report = analyze_bandar_momentum(
            market_rows,
            histories,
            market_day=market_day,
            broker_tickers=broker_universe,
            broker_lookup=lambda ticker: _default_broker_lookup(
                ticker, broker_asof, db_path
            ),
        )
    except Exception as exc:
        print(
            f"[BANDAR-MOMENTUM-ERROR] {type(exc).__name__}: {str(exc)[:200]}"
        )
        detail = str(exc) if isinstance(exc, ValueError) else "Data cache/Neon gagal dibaca; coba lagi."
        return f"⏳ BANDAR MOMENTUM {normalized_mode} — DATA BELUM VALID\n{detail}"

    lines = [
        f"🔥 BANDAR MOMENTUM — {normalized_mode} {market_day}",
        f"Sumber pasar: {source}",
        f"Broker cache sampai: {broker_asof}",
        (
            "Filter: BrokerScore ≥20 | Value ≥Rp3 miliar | "
            "Harga ≥EMA20 +1,2% | RS ≥70 | RSI >50 | 1D >5% | 1M >0%"
        ),
        (
            f"IHSG {report['market_count']} | Histori valid {report['technical_count']} "
            f"| Cache broker {report['broker_coverage']} | "
            f"Lolos teknikal {report['prefilter_count']} | "
            f"Lolos akhir {len(report['rows'])}"
        ),
    ]
    target = _expected_eod_date(now)
    if normalized_mode == "EOD" and market_day < target:
        lines.append(f"⚠️ EOD BELUM TERBARU — target {target}, tersedia {market_day}.")
    if broker_asof < market_day:
        lines.append(
            f"⚠️ Broker memakai penutupan {broker_asof}; harga/value memakai {market_day}."
        )

    rows = report["rows"][: int(limit)]
    lines.append("")
    if not rows:
        lines.append("Belum ada saham yang memenuhi seluruh filter secara bersamaan.")
    for index, row in enumerate(rows, 1):
        broker = row["bandar"]
        avg_buy = broker.get("avg_buy_20d")
        price_vs_avg = (
            (row["close"] / avg_buy - 1.0) * 100.0
            if avg_buy is not None and avg_buy > 0
            else None
        )
        lines.extend((
            f"{index}. {row['ticker']} | Harga {_price(row['close'])} "
            f"({row['return_1d_pct']:+.2f}%) | Value {_rupiah(row['value'])}",
            f"   BrokerScore {broker['score']:.1f} | Dominan {broker['broker']} "
            f"| Horizon {broker['horizon_count']}/3",
            f"   Avg Buy 20D {_price(avg_buy)}"
            + (
                f" | Harga vs avg {price_vs_avg:+.2f}%"
                if price_vs_avg is not None else ""
            ),
            f"   EMA20 {row['ema_gap_pct']:+.2f}% | RS {row['rs_rating']:.0f}/99 "
            f"| RSI {row['rsi14']:.1f} | Return 1M {row['return_1m_pct']:+.2f}%",
        ))

    lines.extend((
        "",
        (
            "ZAPI: 1 snapshot stock-summary sesi berjalan."
            if normalized_mode == "LIVE"
            else "ZAPI: 0 request — pasar dari cache EOD."
        ),
        "BrokerScore = persistensi broker pembeli yang terlihat; bukan formula Stockbit.",
        "⚠️ WATCHLIST, bukan sinyal entry. Hindari mengejar harga yang terlalu jauh dari avg buyer.",
    ))
    if report["broker_errors"]:
        lines.append(
            f"Data broker gagal dihitung untuk {report['broker_errors']} kandidat; kandidat tersebut dilewati."
        )
    return "\n".join(lines)
