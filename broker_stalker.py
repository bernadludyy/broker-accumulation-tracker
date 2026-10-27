"""Cache-only multi-broker accumulation discovery for Telegram Bot 1.

The upstream broker summary exposes only visible top brokers.  Consequently,
this module describes visible activity, not broker inventory, custody, or the
nationality of the underlying investors.
"""
from __future__ import annotations

import math
import re
import sqlite3
from collections import defaultdict
from math import ceil

from broker_ytd_matrix import _read_db, rupiah


ALLOWED_LOOKBACKS = {5, 10, 20}
BROKER_PATTERN = re.compile(r"[A-Z0-9]{2,5}\Z")
TICKER_PATTERN = re.compile(r"[A-Z0-9]{2,12}\Z")


def _finite_nonnegative(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _broker_dates(con: sqlite3.Connection, asof: str, lookback: int) -> list[str]:
    return [
        str(row[0])
        for row in con.execute(
            "SELECT DISTINCT trade_date FROM broker_daily "
            "WHERE trade_date<=? ORDER BY trade_date DESC LIMIT ?",
            (asof, lookback),
        ).fetchall()[::-1]
    ]


def analyze_broker_stalker(
    db_path: str,
    asof: str,
    *,
    lookback: int = 5,
) -> dict:
    """Rank stocks accumulated by multiple visible brokers.

    A stock is eligible when at least two brokers have positive net value and
    net-buy on at least half of the selected sessions. This persistence rule
    avoids counting every broker that appears once on the BUY side.
    Malformed legacy broker-day rows are quarantined as a whole so a remaining
    BUY or SELL side cannot fabricate accumulation.
    """
    if lookback not in ALLOWED_LOOKBACKS:
        raise ValueError("Lookback harus 5, 10, atau 20 sesi")

    with _read_db(db_path) as con:
        dates = _broker_dates(con, asof, lookback)
        if not dates:
            return {
                "asof": asof,
                "lookback": lookback,
                "dates": [],
                "rows": [],
                "active_brokers": 0,
                "stock_count": 0,
                "historical_stock_count": 0,
                "multi_broker_count": 0,
                "latest_net_buy_count": 0,
                "latest_positive_net_value": 0.0,
                "total_positive_net_value": 0.0,
                "invalid_rows": 0,
                "invalid_dates": [],
            }

        query = (
            "SELECT trade_date,ticker,broker,side,value,lots FROM broker_daily "
            "WHERE trade_date BETWEEN ? AND ?"
        )
        raw_rows = con.execute(query, (dates[0], dates[-1])).fetchall()
        historical_stock_count = int(
            con.execute("SELECT COUNT(DISTINCT ticker) FROM broker_daily").fetchone()[0]
        )

    staged: dict[tuple[str, str, str], dict[str, float]] = defaultdict(
        lambda: {"buy_value": 0.0, "sell_value": 0.0}
    )
    invalid_keys: set[tuple[str, str, str]] = set()
    invalid_dates: set[str] = set()
    invalid_rows = 0

    for row in raw_rows:
        day = str(row["trade_date"] or "")
        ticker = str(row["ticker"] or "").strip().upper()
        broker = str(row["broker"] or "").strip().upper()
        side = str(row["side"] or "").upper()
        value = _finite_nonnegative(row["value"])
        lots = _finite_nonnegative(row["lots"])
        key = (ticker, broker, day)
        invalid = (
            day not in dates
            or not TICKER_PATTERN.fullmatch(ticker)
            or not BROKER_PATTERN.fullmatch(broker)
            or side not in {"BUY", "SELL"}
            or value is None
            or lots is None
            or lots != int(lots)
        )
        if invalid:
            invalid_rows += 1
            invalid_dates.add(day)
            invalid_keys.add(key)
            continue
        staged[key]["buy_value" if side == "BUY" else "sell_value"] += value

    by_pair: dict[tuple[str, str], dict[str, object]] = defaultdict(
        lambda: {"net_value": 0.0, "positive_days": 0, "seen_days": set()}
    )
    daily_by_ticker: dict[tuple[str, str], float] = defaultdict(float)
    active_brokers: set[str] = set()
    all_tickers: set[str] = set()
    for (ticker, broker, day), values in staged.items():
        if (ticker, broker, day) in invalid_keys:
            continue
        net = values["buy_value"] - values["sell_value"]
        pair = by_pair[(ticker, broker)]
        pair["net_value"] = float(pair["net_value"]) + net
        pair["positive_days"] = int(pair["positive_days"]) + int(net > 0)
        pair["seen_days"].add(day)
        daily_by_ticker[(ticker, day)] += net
        active_brokers.add(broker)
        all_tickers.add(ticker)

    per_ticker: dict[str, dict[str, object]] = defaultdict(
        lambda: {
            "positive_brokers": [],
            "positive_net_value": 0.0,
            "visible_net_value": 0.0,
        }
    )
    for (ticker, broker), pair in by_pair.items():
        net = float(pair["net_value"])
        stock = per_ticker[ticker]
        stock["visible_net_value"] = float(stock["visible_net_value"]) + net
        if net > 0:
            stock["positive_brokers"].append(
                {
                    "broker": broker,
                    "net_value": net,
                    "positive_days": int(pair["positive_days"]),
                    "seen_days": len(pair["seen_days"]),
                }
            )
            stock["positive_net_value"] = (
                float(stock["positive_net_value"]) + net
            )

    results = []
    minimum_positive_days = ceil(len(dates) / 2)
    for ticker, stock in per_ticker.items():
        brokers = sorted(
            (
                broker
                for broker in stock["positive_brokers"]
                if broker["positive_days"] >= minimum_positive_days
            ),
            key=lambda row: (-row["net_value"], row["broker"]),
        )
        if len(brokers) < 2:
            continue
        results.append(
            {
                "ticker": ticker,
                "broker_count": len(brokers),
                "brokers": brokers,
                "positive_net_value": sum(
                    broker["net_value"] for broker in brokers
                ),
                "visible_net_value": float(stock["visible_net_value"]),
                "latest_net_value": daily_by_ticker.get((ticker, dates[-1]), 0.0),
            }
        )

    results.sort(
        key=lambda row: (
            -row["broker_count"],
            -row["positive_net_value"],
            row["ticker"],
        )
    )
    return {
        "asof": asof,
        "lookback": lookback,
        "dates": dates,
        "rows": results,
        "active_brokers": len(active_brokers),
        "stock_count": len(all_tickers),
        "historical_stock_count": historical_stock_count,
        "multi_broker_count": len(results),
        "latest_net_buy_count": sum(
            daily_by_ticker.get((ticker, dates[-1]), 0.0) > 0
            for ticker in all_tickers
        ),
        "latest_positive_net_value": sum(
            max(0.0, daily_by_ticker.get((ticker, dates[-1]), 0.0))
            for ticker in all_tickers
        ),
        "total_positive_net_value": sum(
            row["positive_net_value"] for row in results
        ),
        "invalid_rows": invalid_rows,
        "invalid_dates": sorted(invalid_dates),
    }


def format_broker_stalker(
    db_path: str,
    asof: str,
    *,
    lookback: int = 5,
    limit: int = 6,
) -> str:
    """Format a compact, Telegram-safe Broker Stalker report."""
    if lookback not in ALLOWED_LOOKBACKS or not 1 <= limit <= 10:
        return "Format: BROKER STALKER [5D|10D|20D] [1-10]"
    try:
        report = analyze_broker_stalker(db_path, asof, lookback=lookback)
    except (sqlite3.Error, OSError):
        return "❌ Cache broker SQLite belum siap. Periksa /status atau /refresh ALL."
    except (ValueError, TypeError, OverflowError):
        return "❌ Data cache tidak valid; Broker Stalker ditahan."

    lines = [f"🕵️ BROKER STALKER — {lookback} SESI"]
    if not report["dates"]:
        return "\n".join(lines + ["Data broker belum tersedia sampai tanggal acuan."])

    lines += [
        f"Acuan cache: {report['dates'][0]} s.d. {report['dates'][-1]}",
        f"Paling diburu (maks. {limit} saham):",
    ]
    leaders = report["rows"][:limit]
    if leaders:
        for index, row in enumerate(leaders, 1):
            top_codes = ", ".join(
                broker["broker"] for broker in row["brokers"][:4]
            )
            suffix = ", …" if row["broker_count"] > 4 else ""
            lines.append(
                f"{index}. {row['ticker']} | {row['broker_count']} broker "
                f"| {rupiah(row['positive_net_value'])}\n"
                f"   Broker utama: {top_codes}{suffix}"
            )
    else:
        lines.append("Belum ada saham dengan ≥2 broker yang persisten net buy.")

    status = "DATA OK*" if len(report["dates"]) == lookback else "DATA SEBAGIAN"
    if report["dates"][-1] != asof:
        status = "STALE"
    elif report["stock_count"] < report["historical_stock_count"]:
        status = "CAKUPAN TERBATAS"
    lines += [
        "",
        "📊 RINGKASAN CACHE",
        f"Broker aktif: {report['active_brokers']}",
        f"Saham dianalisis: {report['stock_count']}/{report['historical_stock_count']} ticker cache",
        f"Saham multi-broker: {report['multi_broker_count']}",
        f"Total akumulasi terlihat: {rupiah(report['total_positive_net_value'])}",
        f"Net buy sesi terakhir: {rupiah(report['latest_positive_net_value'])} "
        f"| {report['latest_net_buy_count']} saham",
        "Porsi asing: N/A — kode broker tidak menunjukkan investor asing/lokal.",
        f"Status: {status}",
    ]
    if report["invalid_rows"]:
        lines.append(
            f"DATA QUALITY: {report['invalid_rows']} baris legacy dikecualikan "
            f"({len(report['invalid_dates'])} tanggal)."
        )
    lines += [
        "",
        "⚠️ Nilai adalah penjumlahan net buy positif broker yang terlihat.",
        "Bukan saldo kepemilikan, bukan sinyal entry, dan cakupan hanya ticker cache.",
        "Gunakan Bot 4 untuk konfirmasi momentum/entry.",
    ]
    return "\n".join(lines)
