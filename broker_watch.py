"""Hybrid broker watch: cached EOD broker evidence plus live order-flow proxy.

The live market endpoints do not expose broker identity.  This module therefore
never attributes intraday prints to the selected broker.  It uses cached broker
summary rows only to select candidates, then reports a separately labelled
tradebook pressure snapshot for those tickers.
"""
from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from math import ceil
from typing import Any, Callable

from broker_ytd_matrix import rupiah
from orderflow_tape import book_levels, number, rows, unwrap


BROKER_PATTERN = re.compile(r"[A-Z0-9]{2,5}\Z")
ALLOWED_LOOKBACKS = {5, 10, 20}
DEFAULT_LIMIT = 3
MAX_LIMIT = 5


def _nonnegative(value: Any) -> float | None:
    result = number(value)
    if result is None or result < 0:
        return None
    return result


def select_candidates(
    db_path: str,
    broker_code: str,
    asof: str,
    *,
    lookback: int = 5,
    limit: int = DEFAULT_LIMIT,
) -> dict:
    """Select persistent net-buy tickers from cached visible broker rows."""
    broker = broker_code.strip().upper()
    if not BROKER_PATTERN.fullmatch(broker):
        raise ValueError("Kode broker tidak valid")
    if lookback not in ALLOWED_LOOKBACKS:
        raise ValueError("Lookback harus 5, 10, atau 20 sesi")
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"Limit harus 1-{MAX_LIMIT}")

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        dates = [
            str(row[0])
            for row in con.execute(
                "SELECT DISTINCT trade_date FROM broker_daily "
                "WHERE trade_date<=? ORDER BY trade_date DESC LIMIT ?",
                (asof, lookback),
            ).fetchall()[::-1]
        ]
        if not dates:
            return {"broker": broker, "dates": [], "rows": [], "invalid_rows": 0}
        raw = con.execute(
            "SELECT trade_date,ticker,side,value,lots FROM broker_daily "
            "WHERE broker=? AND trade_date BETWEEN ? AND ?",
            (broker, dates[0], dates[-1]),
        ).fetchall()
    finally:
        con.close()

    daily: dict[tuple[str, str], dict[str, float]] = defaultdict(
        lambda: {"buy_value": 0.0, "sell_value": 0.0,
                 "buy_lots": 0.0, "sell_lots": 0.0}
    )
    invalid_keys: set[tuple[str, str]] = set()
    invalid_rows = 0
    for row in raw:
        day = str(row["trade_date"] or "")
        ticker = str(row["ticker"] or "").strip().upper()
        side = str(row["side"] or "").strip().upper()
        value = _nonnegative(row["value"])
        lots = _nonnegative(row["lots"])
        key = (ticker, day)
        if (
            day not in dates
            or not re.fullmatch(r"[A-Z0-9]{2,12}", ticker)
            or side not in {"BUY", "SELL"}
            or value is None
            or lots is None
            or lots != int(lots)
        ):
            invalid_rows += 1
            invalid_keys.add(key)
            continue
        prefix = "buy" if side == "BUY" else "sell"
        daily[key][f"{prefix}_value"] += value
        daily[key][f"{prefix}_lots"] += lots

    grouped: dict[str, dict[str, float | int]] = defaultdict(
        lambda: {"buy_value": 0.0, "sell_value": 0.0,
                 "buy_lots": 0.0, "sell_lots": 0.0,
                 "positive_days": 0, "seen_days": 0,
                 "latest_net_value": 0.0}
    )
    latest = dates[-1]
    for (ticker, day), values in daily.items():
        if (ticker, day) in invalid_keys:
            continue
        item = grouped[ticker]
        for key in ("buy_value", "sell_value", "buy_lots", "sell_lots"):
            item[key] = float(item[key]) + values[key]
        net = values["buy_value"] - values["sell_value"]
        item["positive_days"] = int(item["positive_days"]) + int(net > 0)
        item["seen_days"] = int(item["seen_days"]) + 1
        if day == latest:
            item["latest_net_value"] = net

    minimum_positive_days = ceil(len(dates) / 2)
    candidates = []
    for ticker, item in grouped.items():
        net_value = float(item["buy_value"]) - float(item["sell_value"])
        net_lots = float(item["buy_lots"]) - float(item["sell_lots"])
        if net_value <= 0 or int(item["positive_days"]) < minimum_positive_days:
            continue
        avg_buy = (
            float(item["buy_value"]) / (float(item["buy_lots"]) * 100)
            if float(item["buy_lots"]) > 0 and float(item["buy_value"]) > 0
            else None
        )
        candidates.append({
            "ticker": ticker,
            "net_value": net_value,
            "net_lots": net_lots,
            "buy_value": float(item["buy_value"]),
            "sell_value": float(item["sell_value"]),
            "avg_buy": avg_buy,
            "positive_days": int(item["positive_days"]),
            "seen_days": int(item["seen_days"]),
            "latest_net_value": float(item["latest_net_value"]),
        })
    candidates.sort(key=lambda item: (-item["net_value"], item["ticker"]))
    return {
        "broker": broker,
        "dates": dates,
        "rows": candidates[:limit],
        "eligible_count": len(candidates),
        "invalid_rows": invalid_rows,
    }


def _quote_map(payload: dict) -> dict[str, dict]:
    data = unwrap(payload)
    source = rows(data, "items")
    if not source and isinstance(data, dict) and data.get("code"):
        source = [data]
    result = {}
    for item in source:
        ticker = str(item.get("code", item.get("ticker", ""))).strip().upper()
        if ticker:
            result[ticker] = item
    return result


def _quote_number(item: dict, *names: str) -> float | None:
    for name in names:
        value = number(item.get(name))
        if value is not None:
            return value
    return None


def fetch_live_proxies(
    tickers: list[str],
    *,
    fetcher: Callable[[str, dict], dict] | None = None,
) -> dict:
    """Fetch at most N tradebooks plus one shared quote request."""
    if fetcher is None:
        import tracker
        fetcher = tracker._get
    unique = list(dict.fromkeys(t.upper() for t in tickers))[:MAX_LIMIT]
    quotes: dict[str, dict] = {}
    warnings: list[str] = []
    requests = 0
    if unique:
        requests += 1
        try:
            quotes = _quote_map(fetcher("summary", {"codes": ",".join(unique)}))
        except Exception as exc:
            warnings.append(f"Multi Quote tidak tersedia ({type(exc).__name__}).")

    result = {}
    for ticker in unique:
        requests += 1
        try:
            payload = fetcher("tradebook", {"code": ticker, "tab": "ALL"})
            levels = book_levels(payload)
        except Exception as exc:
            warnings.append(f"{ticker}: Tradebook tidak tersedia ({type(exc).__name__}).")
            levels = []
        buy = sum(level["buy"] for level in levels)
        sell = sum(level["sell"] for level in levels)
        total = buy + sell
        ratio = buy / total if total > 0 else None
        poc = max(levels, key=lambda level: level["buy"] + level["sell"])["price"] if levels else None
        quote = quotes.get(ticker, {})
        last = _quote_number(quote, "lastPrice", "last", "close", "Close")
        opening = _quote_number(quote, "open", "Open", "openPrice")
        move = ((last / opening - 1) * 100) if last and opening and opening > 0 else None
        if ratio is None:
            label = "LIVE BELUM TERSEDIA"
        elif ratio >= 0.60:
            label = "BELI DOMINAN"
        elif ratio <= 0.40:
            label = "JUAL DOMINAN"
        else:
            label = "CAMPURAN"
        result[ticker] = {
            "buy": buy,
            "sell": sell,
            "net": buy - sell,
            "ratio": ratio,
            "poc": poc,
            "last": last,
            "open": opening,
            "move": move,
            "label": label,
        }
    return {"rows": result, "requests": requests, "warnings": warnings}


def _price(value: float | None) -> str:
    return f"Rp {value:,.0f}" if value is not None else "N/A"


def _pct(value: float | None) -> str:
    return f"{value:+.2f}%" if value is not None else "N/A"


def format_broker_watch(
    db_path: str,
    broker_code: str,
    asof: str,
    *,
    lookback: int = 5,
    limit: int = DEFAULT_LIMIT,
    fetcher: Callable[[str, dict], dict] | None = None,
) -> str:
    baseline = select_candidates(
        db_path, broker_code, asof, lookback=lookback, limit=limit
    )
    broker = baseline["broker"]
    dates = baseline["dates"]
    candidates = baseline["rows"]
    if not dates:
        return f"⚠️ BROKER WATCH {broker}: cache broker belum memiliki data."
    if not candidates:
        stale = (
            f"⚠️ Cache broker terakhir {dates[-1]}, bukan tanggal acuan {asof}.\n"
            if dates[-1] != asof else ""
        )
        return (
            f"🏦 BROKER WATCH — {broker}\n"
            f"Baseline EOD {dates[-1]} | {len(dates)} sesi\n"
            f"{stale}"
            "Tidak ada saham yang memenuhi net buy dan persistensi minimum.\n"
            "Tidak ada request live yang dijalankan."
        )

    live = fetch_live_proxies([item["ticker"] for item in candidates], fetcher=fetcher)
    lines = [
        f"🏦 BROKER WATCH — {broker}",
        "PROXY LIVE • identitas broker intraday TIDAK terkonfirmasi",
        f"Baseline broker EOD: {dates[0]} s.d. {dates[-1]} | {len(dates)} sesi",
        f"Kandidat persisten: {baseline['eligible_count']} | diperiksa live: {len(candidates)}",
        "",
    ]
    if dates[-1] != asof:
        lines += [
            f"⚠️ Cache broker terakhir {dates[-1]}, bukan tanggal acuan {asof}.",
            "Kandidat dapat berubah setelah refresh broker EOD.",
            "",
        ]
    for index, item in enumerate(candidates, 1):
        proxy = live["rows"].get(item["ticker"], {})
        ratio = proxy.get("ratio")
        if ratio is None:
            interpretation = "BELUM ADA KONFIRMASI LIVE"
            pressure = "N/A"
        elif ratio >= 0.60:
            interpretation = "DIDUKUNG ORDER FLOW (PROXY)"
            pressure = f"{ratio * 100:.1f}% beli agresif"
        elif ratio <= 0.40:
            interpretation = "TEKANAN JUAL — TUNGGU EOD"
            pressure = f"{ratio * 100:.1f}% beli agresif"
        else:
            interpretation = "FLOW CAMPURAN — TUNGGU"
            pressure = f"{ratio * 100:.1f}% beli agresif"
        lines += [
            f"{index}. {item['ticker']} | {interpretation}",
            f"   {broker} EOD: {rupiah(item['net_value'])} | net buy {item['positive_days']}/{len(dates)} sesi",
            f"   Avg buy terlihat: {_price(item['avg_buy'])}",
            f"   Live: {proxy.get('label', 'N/A')} | {pressure} | delta {rupiah(proxy.get('net', 0)) if ratio is not None else 'N/A'}",
            f"   Harga: {_price(proxy.get('last'))} | open→last {_pct(proxy.get('move'))} | POC {_price(proxy.get('poc'))}",
            "",
        ]
    if baseline["invalid_rows"]:
        lines.append(f"⚠️ {baseline['invalid_rows']} baris broker rusak dilewati.")
    lines.extend(live["warnings"])
    lines += [
        "BATAS DATA",
        "Live memakai Tradebook/order-flow saham kandidat, bukan transaksi broker yang dipilih.",
        "Broker sebenarnya baru dikonfirmasi setelah Broker Summary EOD tersedia.",
        f"Operasi ZAPI: maks. {len(candidates) + 1}; aktual {live['requests']}. 0 AI, on-demand.",
        f"Bantuan: BROKER WATCH {broker} [5D|10D|20D] [1-{MAX_LIMIT}]",
    ]
    return "\n".join(lines)
