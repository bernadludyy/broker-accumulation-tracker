"""On-demand Bot 1 market combinations in LIVE and EOD modes.

Stock-summary provides the broad, cache-friendly prefilter. Only the strongest
shortlist is checked against live orderbook data, preventing an all-IDX
orderbook fan-out. EOD mode is cache-only and deliberately omits orderbook
confirmation. Thresholds are hypotheses, not backtested entry signals.
"""
from __future__ import annotations

import math
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

import tracker
from bot2_entry import load_shared_live_market
from cache_commands import _expected_eod_date, load_cached_market


WIB = ZoneInfo("Asia/Jakarta")
TOP_RANK = max(1, int(os.getenv("MARKET_COMBO_TOP_RANK", "50")))
PREFILTER_LIMIT = max(
    1, min(10, int(os.getenv("MARKET_COMBO_PREFILTER_LIMIT", "10")))
)
BID_MIN_PCT = float(os.getenv("MARKET_COMBO_BID_MIN_PCT", "60"))
COMBO1_ATS_PERCENTILE = min(
    0.99, max(0.50, float(os.getenv("MARKET_COMBO1_ATS_PERCENTILE", "0.80")))
)
COMBO1_MIN_FREQUENCY = max(
    1, int(os.getenv("MARKET_COMBO1_MIN_FREQUENCY", "100"))
)
COMBO1_MIN_VALUE_IDR = max(
    0.0, float(os.getenv("MARKET_COMBO1_MIN_VALUE_IDR", "1000000000"))
)


def _num(row: dict, *keys: str) -> float | None:
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
        or row.get("Code")
        or row.get("code")
        or ""
    ).strip().upper()


def _unwrap(payload: Any) -> Any:
    current = payload
    for _ in range(4):
        if isinstance(current, dict) and isinstance(current.get("data"), dict):
            # Accept both ZAPI's project wrapper and a direct {data: book}
            # response, but stop once the actual depth keys are present.
            if "bids" not in current and "asks" not in current:
                current = current["data"]
                continue
        break
    return current


def analyze_orderbook(payload: Any) -> dict[str, float | None]:
    """Normalize top-five bid/offer depth without inventing missing data."""
    data = _unwrap(payload)
    if not isinstance(data, dict):
        return {}
    bids = data.get("bids") if isinstance(data.get("bids"), list) else []
    asks = data.get("asks") if isinstance(data.get("asks"), list) else []
    bid_lots = sum(
        _num(level, "lots", "Lot", "volume", "Volume") or 0.0
        for level in bids[:5]
        if isinstance(level, dict)
    )
    ask_lots = sum(
        _num(level, "lots", "Lot", "volume", "Volume") or 0.0
        for level in asks[:5]
        if isinstance(level, dict)
    )
    total = bid_lots + ask_lots
    bid_pct = _num(data, "bidPercent", "bid_percent")
    ask_pct = _num(data, "askPercent", "ask_percent")
    if bid_pct is None and total > 0:
        bid_pct = 100.0 * bid_lots / total
    if ask_pct is None and bid_pct is not None:
        ask_pct = 100.0 - bid_pct
    best_bid = _num(data, "bestBid", "best_bid")
    best_ask = _num(data, "bestAsk", "best_ask")
    spread_pct = None
    if best_bid and best_ask and best_bid > 0 and best_ask >= best_bid:
        spread_pct = 100.0 * (best_ask - best_bid) / ((best_bid + best_ask) / 2.0)
    return {
        "bid_percent": bid_pct,
        "ask_percent": ask_pct,
        "top5_bid_lots": bid_lots,
        "top5_ask_lots": ask_lots,
        "bid_offer_ratio": (bid_lots / ask_lots if ask_lots > 0 else None),
        "spread_pct": spread_pct,
    }


def _normalize_market(rows: list[dict]) -> list[dict]:
    result = []
    seen = set()
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        ticker = _ticker(raw)
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        volume = _num(raw, "Volume", "volume", "VolumeShares", "volumeShares")
        value = _num(raw, "Value", "value", "TradingValue", "tradingValue")
        frequency = _num(raw, "Frequency", "frequency")
        if (
            volume is None
            or value is None
            or frequency is None
            or volume <= 0
            or value <= 0
            or frequency <= 0
            or frequency != int(frequency)
        ):
            continue
        close = _num(raw, "Close", "close", "Last", "last", "LastPrice")
        result.append(
            {
                "ticker": ticker,
                "volume": volume,
                "value": value,
                "frequency": int(frequency),
                "close": close,
                "market_cap": _num(raw, "MarketCap", "marketCap", "market_cap"),
                "avg_trade_price": value / volume,
                "ats_value": value / frequency,
                "ats_lots": volume / frequency / 100.0,
            }
        )
    for rank, row in enumerate(
        sorted(result, key=lambda x: (-x["volume"], x["ticker"])), 1
    ):
        row["volume_rank"] = rank
    for rank, row in enumerate(
        sorted(result, key=lambda x: (-x["value"], x["ticker"])), 1
    ):
        row["value_rank"] = rank
    for rank, row in enumerate(
        sorted(result, key=lambda x: (-x["frequency"], x["ticker"])), 1
    ):
        row["frequency_rank"] = rank
    for rank, row in enumerate(
        sorted(result, key=lambda x: (-x["ats_value"], x["ticker"])), 1
    ):
        row["ats_rank"] = rank
    return result


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise ValueError("Data percentile kosong")
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _fetch_books(
    candidates: list[dict],
    fetcher: Callable[[str], Any],
) -> tuple[list[dict], list[str]]:
    checked, errors = [], []
    if not candidates:
        return checked, errors
    workers = min(3, len(candidates))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(fetcher, row["ticker"]): row for row in candidates
        }
        for future in as_completed(futures):
            row = futures[future]
            try:
                book = analyze_orderbook(future.result())
                if book.get("bid_percent") is None:
                    errors.append(row["ticker"])
                    continue
                checked.append({**row, **book})
            except Exception:
                errors.append(row["ticker"])
    return checked, sorted(errors)


def _prefilter_market(kind: int, rows: list[dict]) -> dict:
    """Build the shared stock-summary shortlist without touching orderbook."""
    if kind not in {1, 2}:
        raise ValueError("Kombinasi harus 1 atau 2")
    market = _normalize_market(rows)
    if not market:
        raise ValueError("Volume/value/frequency stock-summary tidak lengkap")

    ats_threshold = None
    if kind == 2:
        eligible = [
            row for row in market
            if row["value_rank"] <= TOP_RANK
            and row["frequency_rank"] <= TOP_RANK
        ]
        eligible.sort(
            key=lambda x: (
                x["value_rank"] + x["frequency_rank"],
                x["value_rank"],
                x["ticker"],
            )
        )
    else:
        ats_population = [
            row["ats_value"] for row in market
            if row["frequency"] >= COMBO1_MIN_FREQUENCY
            and row["value"] >= COMBO1_MIN_VALUE_IDR
        ]
        if not ats_population:
            raise ValueError("Populasi ATS likuid belum tersedia")
        ats_threshold = _percentile(ats_population, COMBO1_ATS_PERCENTILE)
        eligible = [
            row for row in market
            if row["frequency"] >= COMBO1_MIN_FREQUENCY
            and row["value"] >= COMBO1_MIN_VALUE_IDR
            and row["ats_value"] >= ats_threshold
        ]
        eligible.sort(key=lambda x: (-x["ats_value"], x["ticker"]))

    return {
        "kind": kind,
        "market_count": len(market),
        "eligible_count": len(eligible),
        "ats_threshold": ats_threshold,
        "eligible": eligible,
    }


def analyze_market_combo(
    kind: int,
    rows: list[dict],
    *,
    fetcher: Callable[[str], Any] | None = None,
) -> dict:
    """Evaluate a LIVE combo using at most ten orderbook requests."""
    fetcher = fetcher or tracker.fetch_orderbook
    prefilter = _prefilter_market(kind, rows)
    eligible = prefilter["eligible"]

    shortlist = eligible[:PREFILTER_LIMIT]
    checked, errors = _fetch_books(shortlist, fetcher)
    selected = [
        row for row in checked
        if row["bid_percent"] is not None
        and row["bid_percent"] >= BID_MIN_PCT
    ]
    if kind == 2:
        selected.sort(
            key=lambda x: (
                -x["bid_percent"],
                x["value_rank"] + x["frequency_rank"],
                x["ticker"],
            )
        )
    else:
        selected.sort(
            key=lambda x: (-x["bid_percent"], -x["ats_value"], x["ticker"])
        )
    return {
        "kind": kind,
        "market_count": prefilter["market_count"],
        "eligible_count": prefilter["eligible_count"],
        "checked_count": len(shortlist),
        "valid_books": len(checked),
        "errors": errors,
        "ats_threshold": prefilter["ats_threshold"],
        "rows": selected,
    }


def analyze_market_combo_eod(kind: int, rows: list[dict]) -> dict:
    """Evaluate the cache-only EOD watchlist without an orderbook request."""
    prefilter = _prefilter_market(kind, rows)
    return {
        "kind": kind,
        "market_count": prefilter["market_count"],
        "eligible_count": prefilter["eligible_count"],
        "ats_threshold": prefilter["ats_threshold"],
        "rows": prefilter["eligible"],
    }


def _id_number(value: float, digits: int = 2) -> str:
    """Format a number with Indonesian decimal and thousands separators."""
    text = f"{float(value):,.{digits}f}"
    return text.translate(str.maketrans({",": ".", ".": ","}))


def _compact(value: float, unit: str = "", *, currency: bool = False) -> str:
    amount = abs(float(value))
    sign = "-" if value < 0 else ""
    for threshold, suffix in (
        (1e12, "triliun"),
        (1e9, "miliar"),
        (1e6, "juta"),
        (1e3, "ribu"),
    ):
        if amount >= threshold:
            prefix = "Rp" if currency else ""
            return f"{prefix}{sign}{_id_number(amount / threshold)} {suffix}{unit}"
    prefix = "Rp" if currency else ""
    return f"{prefix}{sign}{_id_number(amount, 0)}{unit}"


def _whole(value: float) -> str:
    return _id_number(value, 0)


def _price(value: float | None) -> str:
    if value is None or not math.isfinite(float(value)) or float(value) <= 0:
        return "N/A"
    number = float(value)
    digits = 0 if number.is_integer() else 2
    return f"Rp{_id_number(number, digits)}"


def _ratio_text(value: float | None, ask_lots: float | None) -> str:
    if value is not None:
        return f"{_id_number(value)}x"
    if ask_lots == 0:
        return "offer top-5 kosong"
    return "N/A"


def _market_in_progress(now: datetime) -> bool:
    local = now.astimezone(WIB)
    if local.weekday() >= 5:
        return False
    minute = local.hour * 60 + local.minute
    return 9 * 60 <= minute <= 15 * 60 + 50


def _format_market_combo_live(
    kind: int,
    limit: int = 5,
    *,
    now: datetime | None = None,
) -> str:
    if kind not in {1, 2} or not 1 <= limit <= 10:
        return "Format: MARKET COMBO [1|2] [LIVE|EOD] [1-10]"
    now = (now or datetime.now(WIB)).astimezone(WIB)
    if not _market_in_progress(now):
        return (
            f"⌛ MARKET COMBO {kind} — HANYA JAM BURSA\n"
            "Orderbook live tidak diperiksa di luar Senin–Jumat 09:00–15:50 WIB.\n"
            "Tidak ada request orderbook yang digunakan."
        )
    try:
        rows, day, source = load_shared_live_market(with_source=True)
        report = analyze_market_combo(kind, rows)
    except Exception as exc:
        print(f"[MARKET-COMBO-ERROR] {type(exc).__name__}: {str(exc)[:200]}")
        detail = str(exc) if isinstance(exc, ValueError) else "Data market/orderbook gagal; coba lagi."
        return f"⏳ MARKET COMBO {kind} — DATA BELUM VALID\n{detail}"

    title = (
        "⚡ MARKET COMBO 2 LIVE — NILAI + TRANSAKSI + BID"
        if kind == 2
        else "📈 MARKET COMBO 1 LIVE — RATA-RATA TRANSAKSI BESAR + BID"
    )
    lines = [
        title,
        f"Data pasar: {day} | {source}",
        f"Orderbook diperiksa: {report['checked_count']} saham "
        f"| terbaca {report['valid_books']}",
        f"Syarat bid minimum: {_id_number(BID_MIN_PCT, 0)}%",
    ]
    if kind == 2:
        lines.append(
            f"Filter awal: {TOP_RANK} nilai transaksi terbesar + "
            f"{TOP_RANK} transaksi terbanyak"
        )
    else:
        lines.append(
            "Filter awal: rata-rata nilai per transaksi minimal "
            f"{_compact(report['ats_threshold'], currency=True)} "
            f"({(1 - COMBO1_ATS_PERCENTILE) * 100:.0f}% tertinggi)"
        )
        lines.append("Ukuran perusahaan: tidak dibatasi")

    selected = report["rows"][:limit]
    lines.append("")
    if not selected:
        lines.append("Belum ada saham yang memenuhi syarat bid live.")
    for index, row in enumerate(selected, 1):
        ratio = _ratio_text(row.get("bid_offer_ratio"), row.get("top5_ask_lots"))
        spread = (
            f"{_id_number(row['spread_pct'])}%"
            if row.get("spread_pct") is not None else "N/A"
        )
        lines.append(
            f"{index}. {row['ticker']} | Bid {_id_number(row['bid_percent'], 1)}% "
            f"| Rasio Bid/Offer {ratio} | Spread {spread}"
        )
        if kind == 2:
            lines.append(
                f"   Nilai #{row['value_rank']}: "
                f"{_compact(row['value'], currency=True)}"
            )
            lines.append(
                f"   Transaksi #{row['frequency_rank']}: "
                f"{_whole(row['frequency'])} kali"
            )
            lines.append(
                f"   Harga terakhir: {_price(row.get('close'))} | "
                f"Rata-rata harga: {_price(row.get('avg_trade_price'))}"
            )
            lines.append(
                f"   Volume pendukung: {_compact(row['volume'], ' lembar')}"
            )
        else:
            lines.append(
                "   Rata-rata nilai/transaksi: "
                f"{_compact(row['ats_value'], currency=True)}"
            )
            lines.append(
                "   Rata-rata volume/transaksi: "
                f"≈{_id_number(row['ats_lots'], 1)} lot "
                f"| Total {_whole(row['frequency'])} transaksi"
            )
    lines += [
        "",
        f"Waktu orderbook: {now.strftime('%d-%m-%Y %H:%M:%S WIB')}",
        f"ZAPI orderbook maksimal: {report['checked_count']} request/command.",
    ]
    if report["errors"]:
        lines.append(
            "Orderbook gagal/tidak lengkap: " + ", ".join(report["errors"][:10])
        )
    lines += [
        "⚠️ WATCH ONLY — antrean bid dapat berubah atau dibatalkan.",
        "Bukan sinyal entry; konfirmasi momentum dan buyer flow melalui Bot 4.",
    ]
    if kind == 2:
        lines.append(
            "Harga rata-rata = nilai transaksi ÷ volume; bukan harga dominan beli."
        )
    return "\n".join(lines)


def _format_market_combo_eod(
    kind: int,
    limit: int = 5,
    *,
    now: datetime | None = None,
) -> str:
    if kind not in {1, 2} or not 1 <= limit <= 10:
        return "Format: MARKET COMBO [1|2] [LIVE|EOD] [1-10]"
    now = (now or datetime.now(WIB)).astimezone(WIB)
    try:
        rows, day, source = load_cached_market(now=now, completed_only=True)
        report = analyze_market_combo_eod(kind, rows)
    except Exception as exc:
        print(f"[MARKET-COMBO-EOD-ERROR] {type(exc).__name__}: {str(exc)[:200]}")
        detail = str(exc) if isinstance(exc, ValueError) else "Cache EOD gagal dibaca; coba lagi."
        return f"⏳ MARKET COMBO {kind} EOD — DATA BELUM VALID\n{detail}"

    title = (
        "🌙 MARKET COMBO 2 EOD — NILAI + JUMLAH TRANSAKSI"
        if kind == 2
        else "🌙 MARKET COMBO 1 EOD — RATA-RATA TRANSAKSI BESAR"
    )
    lines = [
        title,
        f"Data penutupan: {day} | {source}",
        f"Saham dengan data lengkap: {report['market_count']}",
        f"Lolos filter awal: {report['eligible_count']} saham",
    ]
    if kind == 2:
        lines.append(
            f"Filter awal: {TOP_RANK} nilai transaksi terbesar + "
            f"{TOP_RANK} transaksi terbanyak"
        )
    else:
        lines.append(
            "Filter awal: rata-rata nilai per transaksi minimal "
            f"{_compact(report['ats_threshold'], currency=True)} "
            f"({(1 - COMBO1_ATS_PERCENTILE) * 100:.0f}% tertinggi)"
        )
        lines.append("Ukuran perusahaan: tidak dibatasi")

    expected = _expected_eod_date(now)
    if day < expected:
        lines.append(
            f"⚠️ EOD BELUM TERBARU — target {expected}, tersedia {day}."
        )

    selected = report["rows"][:limit]
    lines.append("")
    if not selected:
        lines.append("Belum ada kandidat yang lolos prefilter EOD.")
    for index, row in enumerate(selected, 1):
        if kind == 2:
            lines.append(f"{index}. {row['ticker']}")
            lines.append(
                f"   Nilai: peringkat #{row['value_rank']} | "
                f"{_compact(row['value'], currency=True)}"
            )
            lines.append(
                f"   Transaksi: peringkat #{row['frequency_rank']} | "
                f"{_whole(row['frequency'])} kali"
            )
            lines.append(
                f"   Harga penutupan: {_price(row.get('close'))} | "
                f"Rata-rata harga: {_price(row.get('avg_trade_price'))}"
            )
            lines.append(
                f"   Volume pendukung: {_compact(row['volume'], ' lembar')}"
            )
        else:
            lines.append(f"{index}. {row['ticker']}")
            lines.append(
                "   Rata-rata nilai/transaksi: "
                f"{_compact(row['ats_value'], currency=True)}"
            )
            lines.append(
                "   Rata-rata volume/transaksi: "
                f"≈{_id_number(row['ats_lots'], 1)} lot"
            )
            lines.append(
                f"   Total: {_whole(row['frequency'])} transaksi | "
                f"{_compact(row['value'], currency=True)}"
            )

    lines += [
        "",
        "ZAPI/orderbook: 0 request — seluruh data dari cache EOD.",
        "⚠️ WATCHLIST SESI BERIKUTNYA, bukan sinyal entry.",
        f"Saat bursa buka, konfirmasi dengan MARKET COMBO {kind} LIVE.",
        "Konfirmasi momentum dan buyer flow melalui Bot 4.",
    ]
    if kind == 2:
        lines.append(
            "Harga rata-rata = nilai transaksi ÷ volume; bukan harga dominan beli."
        )
    return "\n".join(lines)


def format_market_combo(
    kind: int,
    limit: int = 5,
    *,
    mode: str = "LIVE",
    now: datetime | None = None,
) -> str:
    """Format one mode; legacy commands without a mode remain LIVE."""
    normalized_mode = str(mode).strip().upper()
    if normalized_mode == "LIVE":
        return _format_market_combo_live(kind, limit=limit, now=now)
    if normalized_mode == "EOD":
        return _format_market_combo_eod(kind, limit=limit, now=now)
    return "Format: MARKET COMBO [1|2] [LIVE|EOD] [1-10]"
