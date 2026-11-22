"""Broker-flow phase engine for Bot 1.

The module joins price structure, turnover, and *visible* broker-summary rows
into two read-only Telegram views:

* ``GAINER PROGRESS TICKER`` explains the current phase of one stock.
* ``PRE GAINER`` ranks accumulation, absorption, and pre-ignition candidates.

Broker-summary is a partial leaderboard.  A missing broker is therefore treated
as unknown, never as proof of zero activity or investor ownership.
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


WIB = ZoneInfo("Asia/Jakarta")
BROKER_LOOKBACK = max(10, int(os.getenv("GAINER_BROKER_LOOKBACK", "20")))
HISTORY_SESSIONS = max(30, int(os.getenv("GAINER_HISTORY_SESSIONS", "61")))
MIN_PRE_GAINER_SCORE = float(os.getenv("PRE_GAINER_MIN_SCORE", "55"))
PRE_PHASES = {"ACCUMULATION", "ABSORPTION/ROTATION", "PRE-IGNITION"}


def _finite(value: object, *, nonnegative: bool = False) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or (nonnegative and number < 0):
        return None
    return number


def _number(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _finite(row.get(key))
        if value is not None:
            return value
    return None


def _ticker(row: dict[str, Any]) -> str:
    return str(
        row.get("ticker")
        or row.get("StockCode")
        or row.get("stockCode")
        or row.get("code")
        or ""
    ).strip().upper().removesuffix(".JK")


def _ema(values: list[float], span: int) -> float:
    alpha = 2.0 / (span + 1.0)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (1.0 - alpha) * result
    return result


def _rsi14(values: list[float]) -> float:
    if len(values) < 15:
        return 50.0
    gains: list[float] = []
    losses: list[float] = []
    for previous, current in zip(values[-15:-1], values[-14:]):
        change = current - previous
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    average_gain = sum(gains) / 14.0
    average_loss = sum(losses) / 14.0
    if average_loss == 0:
        return 100.0 if average_gain > 0 else 50.0
    relative = average_gain / average_loss
    return 100.0 - 100.0 / (1.0 + relative)


def _price_snapshot(
    history: list[dict[str, Any]],
    market_row: dict[str, Any],
    market_day: str,
) -> dict[str, float] | None:
    by_day: dict[str, dict[str, float]] = {}
    for row in history:
        day = str(row.get("trade_date") or row.get("date") or "")[:10]
        close = _number(row, "close_price", "close", "Close")
        high = _number(row, "high_price", "high", "High")
        low = _number(row, "low_price", "low", "Low")
        value = _number(row, "turnover_idr", "value", "Value")
        if not day or close is None or close <= 0:
            continue
        by_day[day] = {
            "close": close,
            "high": high if high is not None and high > 0 else close,
            "low": low if low is not None and low > 0 else close,
            "value": value if value is not None and value >= 0 else 0.0,
        }

    close = _number(market_row, "Close", "close", "Last", "last")
    if close is None or close <= 0:
        return None
    high = _number(market_row, "High", "high")
    low = _number(market_row, "Low", "low")
    value = _number(
        market_row,
        "Value",
        "value",
        "TradingValue",
        "TransactionValue",
        "turnover_idr",
    )
    by_day[market_day] = {
        "close": close,
        "high": high if high is not None and high > 0 else close,
        "low": low if low is not None and low > 0 else close,
        "value": value if value is not None and value >= 0 else 0.0,
    }

    ordered = [(day, by_day[day]) for day in sorted(by_day)]
    if len(ordered) < 25 or ordered[-1][0] != market_day:
        return None
    ordered = ordered[-HISTORY_SESSIONS:]
    current = ordered[-1][1]
    prior = [row for _, row in ordered[:-1]]
    if len(prior) < 24:
        return None
    closes = [row["close"] for _, row in ordered]
    previous_close = prior[-1]["close"]
    prior_high_20 = max(row["high"] for row in prior[-20:])
    prior_low_60 = min(row["low"] for row in prior[-60:])
    support_10 = min(row["low"] for row in prior[-10:])
    prior_values = [row["value"] for row in prior[-20:] if row["value"] > 0]
    recent_values = [row["value"] for _, row in ordered[-5:] if row["value"] > 0]
    median_value_20 = median(prior_values) if prior_values else 0.0
    median_value_5 = median(recent_values) if recent_values else 0.0
    value_ratio = current["value"] / median_value_20 if median_value_20 > 0 else None
    quiet_ratio = median_value_5 / median_value_20 if median_value_20 > 0 else None
    ema20 = _ema(closes[-30:], 20)
    return {
        "close": current["close"],
        "previous_close": previous_close,
        "return_1d_pct": (current["close"] / previous_close - 1.0) * 100.0,
        "ema20": ema20,
        "ema_gap_pct": (current["close"] / ema20 - 1.0) * 100.0,
        "rsi14": _rsi14(closes),
        "prior_high_20": prior_high_20,
        "distance_high_pct": (current["close"] / prior_high_20 - 1.0) * 100.0,
        "prior_low_60": prior_low_60,
        "distance_low_pct": (current["close"] / prior_low_60 - 1.0) * 100.0,
        "support_10": support_10,
        "value": current["value"],
        "median_value_20": median_value_20,
        "value_ratio": value_ratio,
        "quiet_ratio": quiet_ratio,
        "breakout": float(current["close"] > prior_high_20),
    }


def _load_broker_flows(
    db_path: str,
    asof: str,
    *,
    tickers: set[str] | None = None,
    lookback: int = BROKER_LOOKBACK,
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
                (asof, int(lookback)),
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
        parameters: list[Any] = [dates[0], dates[-1]]
        where = "trade_date BETWEEN ? AND ?"
        normalized = sorted({str(code).strip().upper() for code in (tickers or set())})
        if normalized:
            placeholders = ",".join("?" for _ in normalized)
            where += f" AND ticker IN ({placeholders})"
            parameters.extend(normalized)
        rows = connection.execute(
            f"SELECT {selected} FROM broker_daily WHERE {where}", parameters
        ).fetchall()

    staged: dict[tuple[str, str, str], dict[str, float]] = defaultdict(
        lambda: {
            "buy_value": 0.0,
            "sell_value": 0.0,
            "buy_lots": 0.0,
            "sell_lots": 0.0,
        }
    )
    invalid_keys: set[tuple[str, str, str]] = set()
    invalid_rows = 0
    for row in rows:
        day = str(row["trade_date"] or "")
        code = str(row["ticker"] or "").strip().upper()
        broker = str(row["broker"] or "").strip().upper()
        side = str(row["side"] or "").strip().upper()
        value = _finite(row["value"], nonnegative=True)
        lots = (
            _finite(row["lots"], nonnegative=True) if "lots" in optional else None
        )
        key = (code, broker, day)
        invalid = (
            not code
            or not broker
            or day not in dates
            or side not in {"BUY", "SELL"}
            or value is None
            or (lots is not None and lots != int(lots))
        )
        if invalid:
            invalid_rows += 1
            invalid_keys.add(key)
            continue
        bucket = staged[key]
        bucket[f"{side.lower()}_value"] += value
        if lots is not None:
            bucket[f"{side.lower()}_lots"] += lots

    by_ticker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (code, broker, day), values in staged.items():
        if (code, broker, day) in invalid_keys:
            continue
        by_ticker[code].append({"date": day, "broker": broker, **values})
    for flows in by_ticker.values():
        flows.sort(key=lambda row: (row["date"], row["broker"]))
    return {
        "dates": dates,
        "by_ticker": dict(by_ticker),
        "invalid_rows": invalid_rows,
    }


def _sum_window(
    flows: list[dict[str, Any]], dates: list[str]
) -> dict[str, dict[str, float]]:
    allowed = set(dates)
    result: dict[str, dict[str, float]] = defaultdict(
        lambda: {
            "buy_value": 0.0,
            "sell_value": 0.0,
            "buy_lots": 0.0,
            "sell_lots": 0.0,
            "buy_days": 0.0,
            "sell_days": 0.0,
        }
    )
    for row in flows:
        if row["date"] not in allowed:
            continue
        broker = row["broker"]
        bucket = result[broker]
        for key in ("buy_value", "sell_value", "buy_lots", "sell_lots"):
            bucket[key] += float(row.get(key) or 0.0)
        net = float(row.get("buy_value") or 0.0) - float(row.get("sell_value") or 0.0)
        bucket["buy_days"] += float(net > 0)
        bucket["sell_days"] += float(net < 0)
    for bucket in result.values():
        bucket["net_value"] = bucket["buy_value"] - bucket["sell_value"]
        bucket["net_lots"] = bucket["buy_lots"] - bucket["sell_lots"]
    return dict(result)


def _broker_snapshot(
    flows: list[dict[str, Any]], reference_dates: list[str]
) -> dict[str, Any] | None:
    if not flows or not reference_dates:
        return None
    observed_dates = sorted({row["date"] for row in flows if row["date"] in reference_dates})
    if not observed_dates:
        return None
    latest_day = observed_dates[-1]
    last5 = reference_dates[-5:]
    current = _sum_window(flows, last5)
    # A ticker can exist in the 20-session cache but have no visible broker row
    # in the latest five reference sessions. Treat that as unavailable data,
    # not as zero activity and never call max() on an empty mapping.
    if not current:
        return None
    positive = [
        (broker, values)
        for broker, values in current.items()
        if values["net_value"] > 0
    ]
    positive.sort(key=lambda item: (-item[1]["net_value"], item[0]))
    leader, leader_values = positive[0] if positive else max(
        current.items(), key=lambda item: (item[1]["net_value"], item[0])
    )

    previous_dates = reference_dates[-10:-5]
    previous = _sum_window(flows, previous_dates)
    previous_positive = [
        (broker, values)
        for broker, values in previous.items()
        if values["net_value"] > 0
    ]
    previous_positive.sort(key=lambda item: (-item[1]["net_value"], item[0]))
    previous_leader = previous_positive[0][0] if previous_positive else None

    by_leader_day = {
        row["date"]: row for row in flows if row["broker"] == leader
    }
    latest = by_leader_day.get(latest_day, {})
    latest_buy_lots = float(latest.get("buy_lots") or 0.0)
    prior_positive_lots = [
        float(row.get("buy_lots") or 0.0)
        for row in flows
        if row["broker"] == leader
        and row["date"] < latest_day
        and float(row.get("buy_lots") or 0.0) > 0
    ]
    lot_baseline = median(prior_positive_lots[-10:]) if prior_positive_lots else None
    lot_spike = (
        latest_buy_lots / lot_baseline
        if lot_baseline is not None and lot_baseline > 0
        else None
    )
    latest_rows = [row for row in flows if row["date"] == latest_day]
    visible_buy_lots = sum(float(row.get("buy_lots") or 0.0) for row in latest_rows)
    concentration = (
        latest_buy_lots / visible_buy_lots if visible_buy_lots > 0 else None
    )
    leader_daily = []
    for day in reference_dates[-10:]:
        row = by_leader_day.get(day)
        if row:
            leader_daily.append(
                {
                    "date": day,
                    "buy_lots": float(row.get("buy_lots") or 0.0),
                    "sell_lots": float(row.get("sell_lots") or 0.0),
                    "net_value": float(row.get("buy_value") or 0.0)
                    - float(row.get("sell_value") or 0.0),
                }
            )
    large_lot = (
        (lot_spike is not None and lot_spike >= 2.0)
        or (concentration is not None and concentration >= 0.30)
    )
    return {
        "leader": leader,
        "previous_leader": previous_leader,
        "rotation": bool(previous_leader and previous_leader != leader),
        "latest_day": latest_day,
        "buy_days_5": int(leader_values["buy_days"]),
        "sell_days_5": int(leader_values["sell_days"]),
        "net_value_5": leader_values["net_value"],
        "net_lots_5": leader_values["net_lots"],
        "latest_buy_lots": latest_buy_lots,
        "latest_sell_lots": float(latest.get("sell_lots") or 0.0),
        "lot_baseline": lot_baseline,
        "lot_spike": lot_spike,
        "concentration": concentration,
        "large_lot": large_lot,
        "positive_brokers_5": len(positive),
        "leader_daily": leader_daily,
        "observed_dates": len(observed_dates),
    }


def _phase_and_score(price: dict[str, float], broker: dict[str, Any]) -> tuple[str, float]:
    value_ratio = price.get("value_ratio")
    lot_spike = broker.get("lot_spike")
    concentration = broker.get("concentration")
    buy_days = int(broker.get("buy_days_5") or 0)
    score = 0.0

    distance_high = price["distance_high_pct"]
    if -5.0 <= distance_high <= 2.0:
        score += 20.0
    elif -10.0 <= distance_high < -5.0:
        score += 12.0
    if -2.0 <= price["ema_gap_pct"] <= 10.0:
        score += 12.0
    elif -5.0 <= price["ema_gap_pct"] < -2.0:
        score += 6.0
    if 45.0 <= price["rsi14"] <= 65.0:
        score += 12.0
    elif 35.0 <= price["rsi14"] <= 72.0:
        score += 6.0
    if value_ratio is not None:
        if 0.8 <= value_ratio <= 2.0:
            score += 10.0
        elif value_ratio > 2.0:
            score += 7.0
    if price["distance_low_pct"] <= 15.0:
        score += 8.0

    score += min(20.0, buy_days * 5.0)
    if broker["net_value_5"] > 0:
        score += 6.0
    if lot_spike is not None:
        score += min(8.0, max(0.0, (lot_spike - 1.0) * 4.0))
    if concentration is not None and concentration >= 0.25:
        score += 6.0
    if broker["positive_brokers_5"] >= 3:
        score += 4.0
    score = round(min(100.0, score), 1)

    extended = (
        price["return_1d_pct"] >= 15.0
        or price["ema_gap_pct"] >= 15.0
        or price["rsi14"] >= 78.0
    )
    broker_support = (
        buy_days >= 2
        or (lot_spike is not None and lot_spike >= 1.5)
        or (concentration is not None and concentration >= 0.25)
    )
    ignition = (
        price["return_1d_pct"] >= 5.0
        and (bool(price["breakout"]) or (value_ratio or 0.0) >= 1.5)
        and broker_support
    )
    pre_ignition = (
        -5.0 <= distance_high <= 1.5
        and price["rsi14"] >= 45.0
        and price["ema_gap_pct"] >= -2.0
        and buy_days >= 2
        and price["return_1d_pct"] < 10.0
    )
    absorption = (
        broker["rotation"]
        or (broker["positive_brokers_5"] >= 3 and broker["net_value_5"] > 0)
    )
    quiet_ratio = price.get("quiet_ratio")
    accumulation = (
        price["distance_low_pct"] <= 15.0
        and buy_days >= 2
        and ((quiet_ratio is not None and quiet_ratio <= 1.0) or (concentration or 0.0) >= 0.25)
    )

    if extended:
        phase = "LATE/EXTENDED"
    elif ignition:
        phase = "IGNITION/MARKUP"
    elif pre_ignition:
        phase = "PRE-IGNITION"
    elif absorption:
        phase = "ABSORPTION/ROTATION"
    elif accumulation:
        phase = "ACCUMULATION"
    else:
        phase = "NEUTRAL"
    return phase, score


def analyze_gainer_progress(
    market_rows: list[dict[str, Any]],
    histories: dict[str, list[dict[str, Any]]],
    broker_flows: dict[str, list[dict[str, Any]]],
    broker_dates: list[str],
    *,
    market_day: str,
    ticker: str | None = None,
) -> dict[str, Any]:
    requested = ticker.strip().upper().removesuffix(".JK") if ticker else None
    market: dict[str, dict[str, Any]] = {}
    for raw in market_rows:
        if not isinstance(raw, dict):
            continue
        code = _ticker(raw)
        if code and code not in market and (requested is None or code == requested):
            market[code] = raw

    diagnostics = []
    for code, market_row in market.items():
        price = _price_snapshot(histories.get(code, []), market_row, market_day)
        broker = _broker_snapshot(broker_flows.get(code, []), broker_dates)
        reasons = []
        if price is None:
            reasons.append("histori harga/value minimal 25 sesi belum lengkap")
        if broker is None:
            reasons.append("histori broker terlihat belum tersedia")
        if price is None or broker is None:
            diagnostics.append(
                {"ticker": code, "eligible": False, "reasons": reasons}
            )
            continue
        phase, score = _phase_and_score(price, broker)
        diagnostics.append(
            {
                "ticker": code,
                "eligible": phase in PRE_PHASES and score >= MIN_PRE_GAINER_SCORE,
                "phase": phase,
                "score": score,
                "price": price,
                "broker": broker,
                "reasons": reasons,
            }
        )

    rows = [row for row in diagnostics if row["eligible"]]
    phase_order = {"PRE-IGNITION": 0, "ABSORPTION/ROTATION": 1, "ACCUMULATION": 2}
    rows.sort(
        key=lambda row: (
            phase_order.get(row["phase"], 9),
            -row["score"],
            -float(row["broker"]["net_value_5"]),
            row["ticker"],
        )
    )
    return {
        "market_count": len(market),
        "history_valid_count": sum(bool(row.get("price")) for row in diagnostics),
        "broker_coverage": sum(bool(row.get("broker")) for row in diagnostics),
        "diagnostics": diagnostics,
        "rows": rows,
        "requested": requested,
    }


def _market_in_progress(now: datetime) -> bool:
    local = now.astimezone(WIB)
    if local.weekday() >= 5:
        return False
    minute = local.hour * 60 + local.minute
    return 9 * 60 <= minute <= 15 * 60 + 50


def _load_analysis(
    db_path: str,
    broker_asof: str,
    *,
    mode: str,
    ticker: str | None,
    now: datetime,
) -> tuple[dict[str, Any], str, str, dict[str, Any]]:
    # Lazy imports keep the deterministic engine independently testable.
    from bot2_entry import load_shared_live_market
    from cache_commands import load_cached_market
    from silent_accum import _load_price_history

    if mode == "LIVE":
        market_rows, market_day, source = load_shared_live_market(
            with_source=True, now=now
        )
    else:
        market_rows, market_day, source = load_cached_market(
            now=now, completed_only=True
        )
    requested = ticker.strip().upper().removesuffix(".JK") if ticker else None
    tickers = sorted(
        {
            _ticker(row)
            for row in market_rows
            if _ticker(row) and (requested is None or _ticker(row) == requested)
        }
    )
    histories = _load_price_history(tickers, market_day)
    aligned_broker_asof = min(broker_asof, market_day)
    broker_report = _load_broker_flows(
        db_path, aligned_broker_asof, tickers=set(tickers)
    )
    report = analyze_gainer_progress(
        market_rows,
        histories,
        broker_report["by_ticker"],
        broker_report["dates"],
        market_day=market_day,
        ticker=requested,
    )
    return report, market_day, source, broker_report


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


def _lots(value: float | None) -> str:
    if value is None:
        return "N/A"
    if abs(value) >= 1000:
        return f"{_id_number(value / 1000, 1)}K"
    return _id_number(value, 0)


def _ratio(value: float | None) -> str:
    return f"{value:.2f}x" if value is not None else "N/A"


def _candidate_lines(row: dict[str, Any], index: int | None = None) -> list[str]:
    prefix = f"{index}. " if index is not None else ""
    price = row["price"]
    broker = row["broker"]
    return [
        f"{prefix}{row['ticker']} | {row['phase']} | Skor {row['score']:.1f}",
        f"Harga Rp{_id_number(price['close'], 0)} ({price['return_1d_pct']:+.2f}%) "
        f"| Jarak high20 {price['distance_high_pct']:+.2f}%",
        f"RSI {price['rsi14']:.1f} | EMA20 {price['ema_gap_pct']:+.2f}% "
        f"| Value {_ratio(price['value_ratio'])}",
        f"Broker {broker['leader']} | NB {broker['buy_days_5']}/5 "
        f"| Net {_rupiah(broker['net_value_5'])} | Lot spike {_ratio(broker['lot_spike'])}",
    ]


def format_gainer_progress(
    ticker: str,
    db_path: str,
    broker_asof: str,
    *,
    mode: str = "EOD",
    now: datetime | None = None,
) -> str:
    code = ticker.strip().upper().removesuffix(".JK")
    normalized_mode = mode.strip().upper()
    if not code or normalized_mode not in {"EOD", "LIVE"}:
        return "Format: GAINER PROGRESS TICKER [EOD|LIVE]"
    now = (now or datetime.now(WIB)).astimezone(WIB)
    if normalized_mode == "LIVE" and not _market_in_progress(now):
        return "\n".join(
            (
                "⌛ GAINER PROGRESS LIVE — HANYA JAM BURSA",
                f"Gunakan GAINER PROGRESS {code} EOD di luar jam bursa.",
                "Tidak ada request ZAPI yang digunakan.",
            )
        )
    try:
        report, market_day, source, broker_report = _load_analysis(
            db_path,
            broker_asof,
            mode=normalized_mode,
            ticker=code,
            now=now,
        )
    except Exception as exc:
        print(f"[GAINER-PROGRESS-ERROR] {type(exc).__name__}: {str(exc)[:200]}")
        detail = str(exc) if isinstance(exc, ValueError) else "Data cache/Neon gagal dibaca; coba lagi."
        return f"⏳ GAINER PROGRESS {code} — DATA BELUM VALID\n{detail}"

    lines = [
        f"📈 GAINER PROGRESS — {code} | {normalized_mode} {market_day}",
        f"Sumber pasar: {source}",
        f"Broker terlihat sampai: {broker_report['dates'][-1] if broker_report['dates'] else 'N/A'}",
    ]
    if not report["diagnostics"]:
        lines.append(f"{code} tidak ditemukan pada snapshot pasar.")
        return "\n".join(lines)
    row = report["diagnostics"][0]
    if not row.get("price") or not row.get("broker"):
        lines.append("Belum dapat dihitung: " + "; ".join(row["reasons"]) + ".")
        return "\n".join(lines)

    price = row["price"]
    broker = row["broker"]
    lines += ["", *_candidate_lines(row)]
    concentration = broker.get("concentration")
    lines += [
        f"Lot terbaru {broker['leader']}: beli {_lots(broker['latest_buy_lots'])} "
        f"| jual {_lots(broker['latest_sell_lots'])}",
        f"Konsentrasi lot beli terlihat: "
        + (f"{concentration * 100:.1f}%" if concentration is not None else "N/A"),
        "Lot besar relatif: "
        + ("YA" if broker["large_lot"] else "BELUM TERBUKTI")
        + " - dinilai terhadap histori broker dan total lot beli yang terlihat.",
    ]
    if broker["rotation"]:
        lines.append(
            f"Broker rotation: {broker['previous_leader']} → {broker['leader']}."
        )
    if broker["leader_daily"]:
        lines += ["", f"Jejak {broker['leader']} (maks. 10 sesi terlihat):"]
        for item in broker["leader_daily"]:
            lines.append(
                f"{item['date']} | B {_lots(item['buy_lots'])} | S {_lots(item['sell_lots'])} "
                f"| Net {_rupiah(item['net_value'])}"
            )

    if row["phase"] == "LATE/EXTENDED":
        action = "Harga sudah extended; jangan dikejar. Tunggu retest dan evaluasi distribusi."
    elif row["phase"] == "IGNITION/MARKUP":
        action = "Markup sudah berjalan; validasi close dan hindari entry jika gap/risk terlalu lebar."
    elif row["phase"] == "PRE-IGNITION":
        action = f"Tunggu close > Rp{_id_number(price['prior_high_20'], 0)} dengan value menguat."
    elif row["phase"] in {"ACCUMULATION", "ABSORPTION/ROTATION"}:
        action = "Masuk watchlist; tunggu persistensi broker dan breakout, bukan membeli hanya karena broker muncul."
    else:
        action = "Belum ada fase pre-gainer yang lengkap."
    lines += [
        "",
        f"Konfirmasi: close > Rp{_id_number(price['prior_high_20'], 0)}",
        f"Invalidasi struktur: close < Rp{_id_number(price['support_10'], 0)}",
        f"Pembacaan: {action}",
        "",
        "Broker Summary adalah aktivitas perantara yang terlihat, bukan identitas atau saldo bandar.",
        (
            "ZAPI: 1 snapshot sesi berjalan; broker tetap memakai cache EOD."
            if normalized_mode == "LIVE"
            else "ZAPI: 0 request - harga dan broker memakai cache EOD."
        ),
    ]
    if broker_report["invalid_rows"]:
        lines.append(
            f"DATA QUALITY: {broker_report['invalid_rows']} baris broker tidak valid dikecualikan."
        )
    return "\n".join(lines)


def format_pre_gainer(
    db_path: str,
    broker_asof: str,
    *,
    mode: str = "EOD",
    limit: int = 10,
    now: datetime | None = None,
    title: str = "PRE GAINER",
) -> str:
    normalized_mode = mode.strip().upper()
    display_title = str(title).strip().upper()
    if display_title not in {"PRE GAINER", "GAINER PROGRESS"}:
        display_title = "PRE GAINER"
    if normalized_mode not in {"EOD", "LIVE"} or not 1 <= int(limit) <= 15:
        return f"Format: {display_title} [EOD|LIVE] [1-15]"
    now = (now or datetime.now(WIB)).astimezone(WIB)
    if normalized_mode == "LIVE" and not _market_in_progress(now):
        return "\n".join(
            (
                f"⌛ {display_title} LIVE — HANYA JAM BURSA",
                f"Gunakan {display_title} atau {display_title} EOD di luar jam bursa.",
                "Tidak ada request ZAPI yang digunakan.",
            )
        )
    try:
        report, market_day, source, broker_report = _load_analysis(
            db_path,
            broker_asof,
            mode=normalized_mode,
            ticker=None,
            now=now,
        )
    except Exception as exc:
        print(f"[PRE-GAINER-ERROR] {type(exc).__name__}: {str(exc)[:200]}")
        detail = str(exc) if isinstance(exc, ValueError) else "Data cache/Neon gagal dibaca; coba lagi."
        return f"⏳ {display_title} {normalized_mode} — DATA BELUM VALID\n{detail}"

    lines = [
        f"{'📈' if display_title == 'GAINER PROGRESS' else '🚦'} {display_title} — {normalized_mode} {market_day}",
        f"Sumber pasar: {source}",
        f"Broker terlihat sampai: {broker_report['dates'][-1] if broker_report['dates'] else 'N/A'}",
        "Fase: ACCUMULATION → ABSORPTION/ROTATION → PRE-IGNITION",
        "IGNITION dan LATE/EXTENDED sengaja dikeluarkan agar tidak mengejar harga.",
        (
            f"Pasar {report['market_count']} | Histori valid {report['history_valid_count']} "
            f"| Broker tersedia {report['broker_coverage']} | Lolos {len(report['rows'])}"
        ),
        "",
    ]
    selected = report["rows"][: int(limit)]
    if not selected:
        lines.append("Belum ada saham yang memenuhi seluruh syarat kandidat gainer.")
    for index, row in enumerate(selected, 1):
        if index > 1:
            lines.append("")
        lines.extend(_candidate_lines(row, index))
    lines += [
        "",
        f"Ambang skor: {MIN_PRE_GAINER_SCORE:.0f}/100. Ini watchlist, bukan prediksi pasti top gainer.",
        "Gunakan GAINER PROGRESS TICKER untuk riwayat lot broker dan level konfirmasi.",
        (
            "ZAPI: 1 snapshot sesi berjalan; broker tetap memakai cache EOD."
            if normalized_mode == "LIVE"
            else "ZAPI: 0 request - seluruh scan memakai cache EOD."
        ),
    ]
    if broker_report["invalid_rows"]:
        lines.append(
            f"DATA QUALITY: {broker_report['invalid_rows']} baris broker tidak valid dikecualikan."
        )
    return "\n".join(lines)

