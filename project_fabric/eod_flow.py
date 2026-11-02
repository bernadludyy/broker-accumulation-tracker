"""Read-only EOD flow proxy for the Bot 1 Telegram command.

The stock-summary archive does not contain tick-by-tick aggressor-side trades.
Consequently this module deliberately calls its result an EOD proxy: foreign
buy/sell shares, price structure, and the closing bid/offer queue snapshot.
"""
from __future__ import annotations

from datetime import date, datetime
import json
import math
import os
from pathlib import Path
import re
import sqlite3
from typing import Any

from .postgres_backend import configured_database_url

try:
    import psycopg
except ImportError:  # pragma: no cover - optional outside GitHub runtime.
    psycopg = None


_CODE = re.compile(r"^[A-Z0-9]{2,12}$")
_BASE_DIR = Path(__file__).resolve().parent.parent


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _date_text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value or "")[:10]


def _raw(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _raw_number(row: dict[str, Any], *keys: str) -> float | None:
    payload = _raw(row.get("raw_json"))
    for key in keys:
        value = _number(payload.get(key))
        if value is not None:
            return value
    return None


def _load_rows(ticker: str, limit: int = 6) -> list[dict[str, Any]]:
    url = configured_database_url()
    if not url or psycopg is None:
        raise RuntimeError("Koneksi Neon EOD belum dikonfigurasi")
    with psycopg.connect(url, connect_timeout=6) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT trade_date, ticker, open_price, high_price, low_price,
                          close_price, volume_shares, raw_json,
                          (SELECT max(trade_date)
                             FROM project_fabric.stock_summary_daily) AS warehouse_date
                     FROM project_fabric.stock_summary_daily
                    WHERE ticker = %s
                    ORDER BY trade_date DESC
                    LIMIT %s""",
                (ticker, max(2, min(int(limit), 20))),
            )
            names = [column.name for column in cursor.description]
            return [dict(zip(names, values)) for values in cursor.fetchall()]


def _broker_last_date(ticker: str, db_path: str | Path | None = None) -> str | None:
    configured = db_path or os.getenv("DB_PATH", "broker_tracker.db")
    path = Path(configured)
    if not path.is_absolute():
        path = _BASE_DIR / path
    if not path.is_file():
        return None
    try:
        with sqlite3.connect(path) as connection:
            row = connection.execute(
                "SELECT max(trade_date) FROM broker_daily WHERE ticker = ?",
                (ticker,),
            ).fetchone()
        return _date_text(row[0]) if row and row[0] else None
    except sqlite3.Error:
        return None


def analyze_eod_flow(
    rows: list[dict[str, Any]], *, broker_last_date: str | None = None
) -> dict[str, Any]:
    """Calculate a transparent flow proxy from newest-first or oldest-first rows."""
    if len(rows) < 2:
        raise ValueError("Minimal dua sesi EOD diperlukan")
    ordered = sorted(rows, key=lambda row: _date_text(row.get("trade_date")))
    latest, previous = ordered[-1], ordered[-2]
    sessions = ordered[-5:]

    close = _number(latest.get("close_price"))
    previous_close = _number(previous.get("close_price"))
    previous_high = _number(previous.get("high_price"))
    high = _number(latest.get("high_price"))
    low = _number(latest.get("low_price"))
    if None in (close, previous_close, previous_high, high, low):
        raise ValueError("OHLC EOD tidak lengkap")

    day_change = ((close / previous_close) - 1.0) * 100.0 if previous_close else None
    base_close = _number(ordered[-6].get("close_price")) if len(ordered) >= 6 else None
    period_change = ((close / base_close) - 1.0) * 100.0 if base_close else None
    price_range = high - low
    close_location = ((close - low) / price_range) if price_range > 0 else 0.5

    foreign_nets: list[tuple[float, float]] = []
    for row in sessions:
        buy = _raw_number(row, "ForeignBuy", "foreignBuy")
        sell = _raw_number(row, "ForeignSell", "foreignSell")
        session_close = _number(row.get("close_price"))
        if buy is not None and sell is not None and session_close is not None:
            foreign_nets.append((buy - sell, session_close))
    latest_foreign_net = foreign_nets[-1][0] if len(foreign_nets) == len(sessions) else None
    foreign_net_5d = sum(item[0] for item in foreign_nets) if foreign_nets else None
    foreign_value_1d = latest_foreign_net * close if latest_foreign_net is not None else None
    foreign_value_5d = sum(net * session_close for net, session_close in foreign_nets) if foreign_nets else None
    net_buy_days = sum(1 for net, _ in foreign_nets if net > 0)

    bid = _raw_number(latest, "BidVolume", "bidVolume")
    offer = _raw_number(latest, "OfferVolume", "offerVolume")
    queue_delta = bid - offer if bid is not None and offer is not None else None
    queue_imbalance = (
        queue_delta / (bid + offer)
        if queue_delta is not None and bid is not None and offer is not None and bid + offer > 0
        else None
    )
    bid_offer_ratio = bid / offer if bid is not None and offer not in (None, 0) else None

    reclaimed = bool(close > previous_high)
    if (
        day_change is not None and day_change > 0
        and reclaimed and close_location >= 0.60
        and latest_foreign_net is not None and latest_foreign_net > 0
    ):
        status = "REBOUND CONFIRMED"
        reading = "Harga merebut high sesi sebelumnya dengan foreign net buy; tekanan jual mereda pada data EOD."
    elif (
        day_change is not None and day_change <= 0
        and foreign_net_5d is not None and foreign_net_5d > 0
        and close_location >= 0.40
    ):
        status = "ABSORPTION WATCH"
        reading = "Harga belum pulih, tetapi foreign 5 sesi positif; tunggu reclaim harga sebagai konfirmasi."
    elif (
        day_change is not None and day_change < 0
        and latest_foreign_net is not None and latest_foreign_net <= 0
        and (close_location < 0.40 or (queue_imbalance is not None and queue_imbalance < 0))
    ):
        status = "SELLING PRESSURE"
        reading = "Harga dan foreign flow masih lemah; belum ada konfirmasi rebound EOD."
    elif day_change is not None and day_change > 0:
        status = "FOLLOW-THROUGH WATCH"
        reading = "Harga memantul, tetapi reclaim/flow belum cukup untuk konfirmasi penuh."
    else:
        status = "NEUTRAL / CONSOLIDATION"
        reading = "Sinyal EOD bercampur; tunggu struktur harga dan flow berikutnya."

    latest_date = _date_text(latest.get("trade_date"))
    warehouse_date = _date_text(latest.get("warehouse_date")) or latest_date
    return {
        "ticker": str(latest.get("ticker") or "").upper(),
        "date": latest_date,
        "warehouse_date": warehouse_date,
        "fresh": latest_date == warehouse_date,
        "sessions": len(sessions),
        "close": close,
        "high": high,
        "low": low,
        "previous_high": previous_high,
        "day_change_pct": day_change,
        "period_change_pct": period_change,
        "close_location": close_location,
        "reclaimed_previous_high": reclaimed,
        "foreign_net_1d": latest_foreign_net,
        "foreign_net_5d": foreign_net_5d,
        "foreign_value_1d_est": foreign_value_1d,
        "foreign_value_5d_est": foreign_value_5d,
        "foreign_days_available": len(foreign_nets),
        "net_buy_days": net_buy_days,
        "bid": bid,
        "offer": offer,
        "queue_delta": queue_delta,
        "queue_imbalance": queue_imbalance,
        "bid_offer_ratio": bid_offer_ratio,
        "broker_last_date": broker_last_date,
        "broker_current": broker_last_date == latest_date,
        "status": status,
        "reading": reading,
    }


def _signed(value: float | None, suffix: str = "") -> str:
    if value is None:
        return "N/A"
    return f"{value:+,.2f}{suffix}"


def _shares(value: float | None) -> str:
    if value is None:
        return "N/A"
    absolute = abs(value)
    if absolute >= 1_000_000_000:
        text = f"{absolute / 1_000_000_000:.2f}B"
    elif absolute >= 1_000_000:
        text = f"{absolute / 1_000_000:.1f}M"
    elif absolute >= 1_000:
        text = f"{absolute / 1_000:.1f}K"
    else:
        text = f"{absolute:,.0f}"
    return ("+" if value > 0 else "-" if value < 0 else "") + text


def _rupiah(value: float | None) -> str:
    if value is None:
        return "N/A"
    absolute = abs(value)
    if absolute >= 1_000_000_000:
        text = f"Rp{absolute / 1_000_000_000:.2f}B"
    elif absolute >= 1_000_000:
        text = f"Rp{absolute / 1_000_000:.1f}M"
    else:
        text = f"Rp{absolute:,.0f}"
    return ("+" if value > 0 else "-" if value < 0 else "") + text


def format_eod_flow(ticker: str, *, rows: list[dict[str, Any]] | None = None,
                    broker_last_date: str | None = None) -> str:
    ticker = ticker.strip().upper()
    if not _CODE.fullmatch(ticker):
        raise ValueError("Kode ticker tidak valid")
    loaded = rows if rows is not None else _load_rows(ticker)
    if not loaded:
        return f"⚠️ EOD FLOW — {ticker}\n\nData ticker tidak ditemukan di arsip Neon. Jalankan /eod refresh."
    if broker_last_date is None:
        broker_last_date = _broker_last_date(ticker)
    result = analyze_eod_flow(loaded, broker_last_date=broker_last_date)

    freshness = "CURRENT" if result["fresh"] else f"STALE (warehouse {result['warehouse_date']})"
    broker = (
        f"CURRENT ({result['broker_last_date']}) — cek {ticker} BROKER 5D"
        if result["broker_current"]
        else f"DATA STALE (terakhir {result['broker_last_date'] or 'N/A'}) — jalankan /refresh {ticker}"
    )
    ratio = f"{result['bid_offer_ratio']:.2f}x" if result["bid_offer_ratio"] is not None else "N/A"
    imbalance = (
        _signed(result["queue_imbalance"] * 100.0, "%")
        if result["queue_imbalance"] is not None else "N/A"
    )
    return "\n".join([
        f"📊 EOD FLOW — {ticker}",
        f"Data: {result['date']} • {freshness} • {result['sessions']} sesi flow",
        f"Status: {result['status']}",
        "",
        "💹 HARGA",
        f"Close {result['close']:,.0f} | 1D {_signed(result['day_change_pct'], '%')} | 5D {_signed(result['period_change_pct'], '%')}",
        f"Range {result['low']:,.0f}–{result['high']:,.0f} | close location {result['close_location'] * 100:.1f}%",
        f"Reclaim high sebelumnya {result['previous_high']:,.0f}: {'YA' if result['reclaimed_previous_high'] else 'BELUM'}",
        "",
        "🌍 DELTA FOREIGN EOD (proxy)",
        f"1D: {_shares(result['foreign_net_1d'])} saham | estimasi {_rupiah(result['foreign_value_1d_est'])}",
        f"5D: {_shares(result['foreign_net_5d'])} saham | estimasi {_rupiah(result['foreign_value_5d_est'])}",
        f"Net buy: {result['net_buy_days']}/{result['foreign_days_available']} sesi tersedia",
        "",
        "📚 SNAPSHOT ANTREAN PENUTUPAN",
        f"Bid {_shares(result['bid'])} | Offer {_shares(result['offer'])} (unit sumber)",
        f"Delta antrean {_shares(result['queue_delta'])} unit | imbalance {imbalance} | B/O {ratio}",
        "",
        f"🏦 Broker: {broker}",
        f"🧭 Bacaan: {result['reading']}",
        "",
        "Catatan: ini proxy EOD, bukan aggressor trade delta live. Antrean dapat berubah/dicabut dan bukan prediksi harga sesi berikutnya.",
    ])
