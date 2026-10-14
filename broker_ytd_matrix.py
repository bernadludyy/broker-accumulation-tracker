"""Read-only YTD broker x ticker ranking over cached broker_daily rows.

Does not use ZAPI or write data. All results describe *visible broker-summary
activity*, not broker custody/inventory or a complete IDX universe.
"""
from __future__ import annotations

import math
import re
import sqlite3
from collections import defaultdict
from datetime import date
from pathlib import Path


TICKER_PATTERN = re.compile(r"[A-Z0-9]{2,12}\Z")
BROKER_PATTERN = re.compile(r"[A-Z0-9]{2,5}\Z")


def rupiah(value: float) -> str:
    """Compact signed IDR display, with sign on the amount rather than unit."""
    sign = "+" if value > 0 else "-" if value < 0 else ""
    amount = abs(value)
    if amount >= 1_000_000_000_000:
        return f"{sign}Rp {amount / 1_000_000_000_000:,.2f} T"
    if amount >= 1_000_000_000:
        return f"{sign}Rp {amount / 1_000_000_000:,.2f} M"
    if amount >= 1_000_000:
        return f"{sign}Rp {amount / 1_000_000:,.2f} Jt"
    if amount >= 1_000:
        return f"{sign}Rp {amount / 1_000:,.1f} Rb"
    return f"{sign}Rp {amount:,.0f}"


def _weighted_avg_price(row: dict, side: str) -> float | None:
    """Weighted mean price per share from BUY or SELL gross-side turnover.

    This is NOT a cost basis for net shares held: sells may have occurred
    during the window. Broker-summary visibility can also be incomplete.
    IDX has 100 shares per lot. Never return a fictitious price for zero lot.
    """
    if side not in {"BUY", "SELL"}:
        raise ValueError("Side must be BUY or SELL")
    key = side.lower()
    lots = float(row.get(f"{key}_lots", 0) or 0)
    value = float(row.get(f"{key}_value", 0) or 0)
    if not (math.isfinite(lots) and math.isfinite(value)):
        return None
    if lots <= 0 or value <= 0:
        return None
    price = value / (lots * 100)
    return price if math.isfinite(price) and price > 0 else None


def _format_avg_price(row: dict, side: str) -> str:
    avg = _weighted_avg_price(row, side)
    return f"Rp {avg:,.2f}" if avg is not None else "N/A"


def _read_db(db_path: str) -> sqlite3.Connection:
    # Read-only mode prevents accidentally creating a blank 'cache' on missing
    # GitHub Actions artifacts, and guarantees that a query cannot mutate it.
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def _year_bounds(asof: str) -> tuple[str, str]:
    end = date.fromisoformat(asof).isoformat()
    return f"{end[:4]}-01-01", end


def _reference_dates(con: sqlite3.Connection, start: str, end: str) -> set[str]:
    """Observed dates only; *not* an official IDX trading calendar."""
    dates = {
        str(row[0]) for row in con.execute(
            "SELECT DISTINCT trade_date FROM broker_daily "
            "WHERE trade_date BETWEEN ? AND ?", (start, end)
        )
    }
    exists = con.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='table' AND name='stock_summary_snapshots'"
    ).fetchone()
    if exists:
        dates.update(
            str(row[0]) for row in con.execute(
                "SELECT trade_date FROM stock_summary_snapshots "
                "WHERE trade_date BETWEEN ? AND ?", (start, end)
            )
        )
    return dates


def _ticker_coverage(
    con: sqlite3.Connection, tickers: list[str], start: str, end: str,
    reference: set[str],
) -> dict[str, dict]:
    if not tickers:
        return {}
    placeholders = ",".join("?" for _ in tickers)
    query = (
        "SELECT ticker, MIN(trade_date) AS first_day, "
        "MAX(trade_date) AS last_day, COUNT(DISTINCT trade_date) AS day_count "
        "FROM broker_daily WHERE ticker IN (" + placeholders + ") "
        "AND trade_date BETWEEN ? AND ? GROUP BY ticker"
    )
    rows = con.execute(query, (*tickers, start, end)).fetchall()
    data = {}
    for row in rows:
        first, last = str(row["first_day"]), str(row["last_day"])
        # Unknown cannot be declared 'complete': references can lack trading
        # dates, and the upstream broker summary includes only top brokers.
        partial = (
            row["day_count"] < len(reference)
            or last < max(reference, default=last)
            or first > f"{start[:4]}-01-10"
        )
        data[row["ticker"]] = {
            "first": first,
            "last": last,
            "days": int(row["day_count"]),
            "reference_days": len(reference),
            "quality": "PARTIAL" if partial else "UNVERIFIED",
        }
    return data


def _aggregates(
    con: sqlite3.Connection, filter_column: str, value: str,
    start: str, end: str,
) -> list[dict]:
    # Static, allowlisted identifier prevents SQL injection in the query.
    if filter_column not in {"ticker", "broker"}:
        raise ValueError("Invalid filter column")
    key = "broker" if filter_column == "ticker" else "ticker"
    query = (
        f"SELECT {key} AS code, trade_date, "
        "SUM(CASE WHEN side='BUY' THEN value ELSE 0 END) AS buy_value, "
        "SUM(CASE WHEN side='SELL' THEN value ELSE 0 END) AS sell_value, "
        "SUM(CASE WHEN side='BUY' THEN lots ELSE 0 END) AS buy_lots, "
        "SUM(CASE WHEN side='SELL' THEN lots ELSE 0 END) AS sell_lots "
        "FROM broker_daily "
        f"WHERE {filter_column}=? AND trade_date BETWEEN ? AND ? "
        f"GROUP BY {key}, trade_date"
    )
    by_code = defaultdict(lambda: {
        "buy_value": 0.0, "sell_value": 0.0,
        "buy_lots": 0.0, "sell_lots": 0.0,
        "buy_days": 0, "sell_days": 0, "seen_days": 0,
    })
    for row in con.execute(query, (value, start, end)):
        stat = by_code[str(row["code"])]
        buy, sell = float(row["buy_value"] or 0), float(row["sell_value"] or 0)
        stat["buy_value"] += buy
        stat["sell_value"] += sell
        stat["buy_lots"] += float(row["buy_lots"] or 0)
        stat["sell_lots"] += float(row["sell_lots"] or 0)
        stat["seen_days"] += 1
        stat["buy_days"] += int(buy > sell)
        stat["sell_days"] += int(buy < sell)
    results = []
    for code, stat in by_code.items():
        results.append({
            "code": code,
            **stat,
            "net_value": stat["buy_value"] - stat["sell_value"],
            "net_lots": stat["buy_lots"] - stat["sell_lots"],
        })
    return results


def _base_lines(start: str, end: str, reference: set[str], scope: int) -> list[str]:
    return [
        f"Periode: {start} s.d. {end}",
        f"Cakupan: {scope} ticker dengan histori broker di SQLite",
        f"Referensi cache: {len(reference)} tanggal (bukan kalender resmi IDX)",
    ]


def _warn() -> list[str]:
    return [
        "",
        "⚠️ Nilai = akumulasi broker summary yang TERLIHAT, bukan posisi saham.",
        "Broker yang tidak muncul di snapshot tidak berarti transaksinya nol.",
        "Cakupan terbatas pada histori tersimpan; bukan ranking seluruh IDX.",
        "UNVERIFIED bukan konfirmasi bahwa histori YTD lengkap.",
        "WAvg Buy/Sell = value sisi BUY/SELL ÷ (lot sisi tersebut × 100).",
        "WAvg bukan harga modal net lot atau bukti kepemilikan broker.",
    ]


def format_ticker_top(ticker: str, db_path: str, asof: str, top_n: int = 3) -> str:
    """Rank visible brokers by net BUY/SELL VALUE for one ticker, YTD."""
    ticker = ticker.strip().upper().removesuffix(".JK")
    if not TICKER_PATTERN.fullmatch(ticker):
        return "❌ Ticker tidak valid. Contoh: BROKER TOP BBRI YTD"
    start, end = _year_bounds(asof)
    try:
        with _read_db(db_path) as con:
            ref = _reference_dates(con, start, end)
            scope = con.execute(
                "SELECT COUNT(DISTINCT ticker) FROM broker_daily "
                "WHERE trade_date BETWEEN ? AND ?", (start, end)
            ).fetchone()[0]
            entries = _aggregates(con, "ticker", ticker, start, end)
            coverage = _ticker_coverage(con, [ticker], start, end, ref).get(ticker)
    except sqlite3.Error as exc:
        return f"❌ Cache broker SQLite belum siap ({type(exc).__name__})."

    lines = [f"🏦 BROKER TOP 3 — {ticker} YTD"]
    lines += _base_lines(start, end, ref, scope)
    if not entries or coverage is None:
        lines += [
            "", f"❌ Data broker {ticker} untuk {end[:4]} belum tersimpan.",
            f"Gunakan /status {ticker} atau /refresh {ticker} bila perlu.",
        ]
        return "\n".join(lines)

    lines += [
        f"Data {ticker}: {coverage['first']} s.d. {coverage['last']}",
        f"Coverage: {coverage['days']}/{coverage['reference_days']} "
        f"tanggal cache | {coverage['quality']}",
    ]
    buyers = sorted(
        (x for x in entries if x["net_value"] > 0),
        key=lambda x: (-x["net_value"], x["code"]),
    )[:top_n]
    sellers = sorted(
        (x for x in entries if x["net_value"] < 0),
        key=lambda x: (x["net_value"], x["code"]),
    )[:top_n]
    pos_total = sum(x["net_value"] for x in entries if x["net_value"] > 0)
    buy_total = sum(x["net_value"] for x in buyers)
    sell_total = sum(x["net_value"] for x in sellers)

    lines += ["", "🟢 TOP 3 NET BUY VALUE"]
    lines += [
        f"{i}. {r['code']} | {rupiah(r['net_value'])} "
        f"| {int(r['net_lots']):+,} lot | {r['buy_days']} hari net buy"
        f"\n   WAvg Buy: {_format_avg_price(r, 'BUY')}"
        for i, r in enumerate(buyers, 1)
    ] or ["Belum ada broker net buy positif."]
    lines.append(
        f"Total Top 3: {rupiah(buy_total)}"
        + (f" | {buy_total / pos_total:.1%} dari net buy positif terlihat"
           if pos_total else "")
    )
    lines += ["", "🔴 TOP 3 NET SELL VALUE"]
    lines += [
        f"{i}. {r['code']} | {rupiah(r['net_value'])} "
        f"| {int(r['net_lots']):+,} lot | {r['sell_days']} hari net sell"
        f"\n   WAvg Sell: {_format_avg_price(r, 'SELL')}"
        for i, r in enumerate(sellers, 1)
    ] or ["Belum ada broker net sell negatif."]
    lines.append(f"Total Top 3: {rupiah(sell_total)}")
    return "\n".join(lines + _warn())


def format_broker_top(
    broker: str, db_path: str, asof: str, mode: str = "BUY", top_n: int = 3,
) -> str:
    """Rank cached stocks by one broker's net VALUE across the YTD window."""
    broker = broker.strip().upper()
    mode = mode.strip().upper()
    if not BROKER_PATTERN.fullmatch(broker) or mode not in {"BUY", "SELL"}:
        return "❌ Kode broker atau mode tidak valid. Format: CC NI TOP YTD atau CC NI TOP SELL YTD"
    start, end = _year_bounds(asof)
    try:
        with _read_db(db_path) as con:
            ref = _reference_dates(con, start, end)
            scope = con.execute(
                "SELECT COUNT(DISTINCT ticker) FROM broker_daily "
                "WHERE trade_date BETWEEN ? AND ?", (start, end)
            ).fetchone()[0]
            entries = _aggregates(con, "broker", broker, start, end)
            if mode == "BUY":
                eligible = sorted(
                    (x for x in entries if x["net_value"] > 0),
                    key=lambda x: (-x["net_value"], x["code"]),
                )
            else:
                eligible = sorted(
                    (x for x in entries if x["net_value"] < 0),
                    key=lambda x: (x["net_value"], x["code"]),
                )
            leaders = eligible[:top_n]
            coverage = _ticker_coverage(
                con, [r["code"] for r in leaders], start, end, ref
            )
    except sqlite3.Error as exc:
        return f"❌ Cache broker SQLite belum siap ({type(exc).__name__})."

    lines = [
        f"🏦 CC {broker} — TOP 3 NET {mode} SAHAM YTD",
        *_base_lines(start, end, ref, scope),
        f"Saham {mode} terdeteksi: {len(eligible)}",
    ]
    if not leaders:
        lines += ["", f"Tidak ada Net {mode} {broker} pada histori YTD tersimpan."]
        return "\n".join(lines + _warn())

    lines.append("")
    for i, row in enumerate(leaders, 1):
        c = coverage.get(row["code"], {})
        lines += [
            f"{i}. {row['code']} | {rupiah(row['net_value'])} "
            f"| {int(row['net_lots']):+,} lot",
            f"   WAvg {mode.title()}: {_format_avg_price(row, mode)}",
            f"   Broker: {row['buy_days' if mode == 'BUY' else 'sell_days']} "
            f"hari net {mode} | Cache saham: {c.get('days', 0)}"
            f"/{c.get('reference_days', len(ref))} "
            f"| {c.get('quality', 'UNKNOWN')} | Last {c.get('last', '-')}",
        ]
    total = sum(row["net_value"] for row in leaders)
    all_eligible = sum(row["net_value"] for row in eligible)
    lines += [
        "",
        f"Total Net {mode} Top 3: {rupiah(total)}",
        f"Porsi Top 3 dari total Net {mode} terdeteksi: "
        f"{abs(total / all_eligible):.1%}" if all_eligible else "Porsi: -",
    ]
    return "\n".join(lines + _warn())
