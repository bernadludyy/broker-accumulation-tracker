"""Shared, read-only IDX Universe intelligence features (schema v1).

Derived ONLY from stock-summary and completed OHLCV history already requested by
the existing collector/scanners. These are descriptive proxies, not trade
instructions, prediction probabilities, or broker-identification evidence.
No upstream calls, new schedules, or Telegram deliveries are made here.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from typing import Any

WIB = timezone(timedelta(hours=7))
VERSION = 1
SUMMARY_ENDPOINT = "universe:intelligence:summary:v1"
HISTORY_ENDPOINT = "universe:intelligence:history:v1"
SUMMARY_PARAMS = {"scope": "IDX"}
SUMMARY_TTL_SECONDS = 900
HISTORY_TTL_SECONDS = 259200


def _number(row: dict, *names: str) -> float | None:
    for name in names:
        val = row.get(name)
        if val in (None, "") or isinstance(val, bool):
            continue
        try:
            result = float(str(val).replace(",", ""))
        except (ValueError, TypeError):
            continue
        if math.isfinite(result):
            return result
    return None


def _clamp(x: float) -> float:
    return round(max(0.0, min(100.0, x)), 1)


def _ticker(row: dict) -> str:
    return str(
        row.get("StockCode") or row.get("stockCode") or row.get("ticker")
        or row.get("Code") or row.get("code") or ""
    ).strip().upper()


def _items(payload: Any) -> list[dict]:
    for _ in range(4):
        if isinstance(payload, list):
            return [x for x in payload if isinstance(x, dict)]
        if not isinstance(payload, dict):
            return []
        child = payload.get("items")
        if not isinstance(child, list):
            child = payload.get("data")
        if isinstance(child, (list, dict)):
            payload = child
            continue
        return []
    return []


def _cache_key(endpoint: str, params: dict) -> str:
    # Exact Project Fabric DataGateway._cache_key convention.
    safe = json.dumps(params, sort_keys=True, default=str, separators=(",", ":"))
    return f"{endpoint}:{hashlib.sha256(safe.encode('utf-8')).hexdigest()}"


def institutional_flow(row: dict) -> dict:
    """Foreign-flow context. Broker concentration requires independent broker data."""
    price = _number(row, "Close", "close")
    total_value = _number(row, "Value", "value")
    buy_value = _number(row, "foreignBuyValue", "ForeignBuyValue")
    sell_value = _number(row, "foreignSellValue", "ForeignSellValue")
    source = "reported_foreign_value"
    if buy_value is None or sell_value is None:
        buy_shares = _number(row, "ForeignBuy", "foreignBuy", "ForeignBuyShares")
        sell_shares = _number(row, "ForeignSell", "foreignSell", "ForeignSellShares")
        if price is None or price <= 0 or buy_shares is None or sell_shares is None:
            return {"score": None, "status": "UNAVAILABLE", "source": None,
                    "net_foreign_value_idr": None, "broker_concentration": None}
        buy_value, sell_value = buy_shares * price, sell_shares * price
        source = "estimated_from_foreign_shares_x_close"
    if buy_value < 0 or sell_value < 0:
        return {"score": None, "status": "UNAVAILABLE", "source": None,
                "net_foreign_value_idr": None, "broker_concentration": None}
    net = buy_value - sell_value
    total_foreign = buy_value + sell_value
    if total_value is None or total_value <= 0 or total_foreign <= 0:
        score = None
    else:
        ratio = net / total_value * 100.0
        dominance = buy_value / total_foreign * 100.0
        score = _clamp(
            (max(-10.0, min(10.0, ratio)) + 10.0) / 20.0 * 60.0
            + (max(30.0, min(70.0, dominance)) - 30.0) / 40.0 * 40.0
        )
    status = ("UNAVAILABLE" if score is None else
              "POSITIVE" if score >= 60 else
              "NEGATIVE" if score < 40 else "MIXED")
    return {"score": score, "status": status, "source": source,
            "net_foreign_value_idr": round(net, 2),
            "broker_concentration": None}


def summary_snapshot(payload: Any, observed_at: str | None = None) -> dict:
    """Index all available IDX tickers in one Neon JSON document."""
    rows = _items(payload)
    signals: dict[str, dict] = {}
    for row in rows:
        ticker = _ticker(row)
        if not ticker or ticker in signals:
            continue
        flow = institutional_flow(row)
        signals[ticker] = {
            "institutional_flow": flow,
            "accumulation_persistence": {"score": None, "status": "NEED_HISTORY"},
            "breakout_readiness": {"score": None, "status": "NEED_HISTORY"},
            "distribution_risk": {"score": None, "status": "NEED_HISTORY"},
            "entry_quality": {"score": None, "status": "NEED_HISTORY"},
        }
    if not signals:
        raise ValueError("IDX stock-summary cannot publish an empty intelligence snapshot")
    return {"schema_version": VERSION,
            "observed_at": observed_at or datetime.now(WIB).isoformat(timespec="seconds"),
            "source": "idx:stock-summary", "count": len(signals), "tickers": signals}


def _history(payload: Any, asof: datetime | None = None) -> list[dict]:
    today = (asof or datetime.now(WIB)).astimezone(WIB).date().isoformat()
    dates: dict[str, dict] = {}
    for row in _items(payload):
        stamp = str(row.get("date") or row.get("Date") or "")[:10]
        if len(stamp) != 10 or stamp >= today:
            continue
        op = _number(row, "open", "Open")
        hi = _number(row, "high", "High")
        lo = _number(row, "low", "Low")
        close = _number(row, "close", "Close")
        vol = _number(row, "volume", "Volume")
        if None in (hi, lo, close, vol) or lo <= 0 or hi < lo or not lo <= close <= hi or vol < 0:
            continue
        dates[stamp] = {"date": stamp, "open": op, "high": hi, "low": lo,
                        "close": close, "volume": vol}
    return [dates[k] for k in sorted(dates)]


def _missing(status: str = "NEED_HISTORY") -> dict:
    return {"score": None, "status": status}


def history_features(ticker: str, payload: Any, asof: datetime | None = None) -> dict:
    """Five independently inspectable features; no look-ahead/current partial candle."""
    rows = _history(payload, asof)
    result = {
        "schema_version": VERSION,
        "ticker": ticker.strip().upper(),
        "source": "idx:stock-history:completed-sessions",
        "history_sessions": len(rows),
        "history_last_date": rows[-1]["date"] if rows else None,
        "observed_at": (asof or datetime.now(WIB)).isoformat(timespec="seconds"),
        "accumulation_persistence": _missing(),
        "breakout_readiness": _missing(),
        "institutional_flow": _missing("NEED_STOCK_SUMMARY"),
        "distribution_risk": _missing(),
        "entry_quality": _missing(),
    }
    if len(rows) < 30:
        return result
    last = rows[-1]
    prev = rows[-21:-1]
    recent = rows[-5:]
    prev5 = rows[-10:-5]
    avg_vol = sum(x["volume"] for x in prev) / len(prev)
    if avg_vol <= 0:
        return result

    # 1. Accumulation persistence: close/volume proxy, NOT broker accumulation.
    last10 = rows[-10:]
    up_volume = sum(x["volume"] for x, y in zip(last10[1:], last10[:-1])
                    if x["close"] > y["close"])
    down_volume = sum(x["volume"] for x, y in zip(last10[1:], last10[:-1])
                      if x["close"] < y["close"])
    ratio = up_volume / (up_volume + down_volume) if up_volume + down_volume else None
    rising_lows = sum(1 for x, y in zip(last10[1:], last10[:-1])
                      if x["low"] >= y["low"])
    if ratio is not None:
        persistence = _clamp(65.0 * ratio + 35.0 * rising_lows / 9.0)
        result["accumulation_persistence"] = {
            "score": persistence, "status": "PRICE_VOLUME_PROXY",
            "up_volume_ratio": round(ratio, 4), "rising_low_intervals": rising_lows,
            "broker_confirmed": False,
        }

    # 2. Breakout readiness: contracting ranges + closeness to prior resistance.
    recent_range = max(x["high"] for x in recent) - min(x["low"] for x in recent)
    previous_range = max(x["high"] for x in prev5) - min(x["low"] for x in prev5)
    prior_high = max(x["high"] for x in prev)
    if previous_range > 0 and prior_high > 0:
        contraction = recent_range / previous_range
        distance_pct = (prior_high - last["close"]) / prior_high * 100.0
        proximity = _clamp(100.0 - max(0.0, distance_pct) * 20.0)
        squeeze = _clamp((1.5 - contraction) / 1.3 * 100.0)
        result["breakout_readiness"] = {
            "score": _clamp(0.55 * proximity + 0.45 * squeeze),
            "status": "OBSERVED",
            "range_contraction_ratio": round(contraction, 3),
            "distance_to_prior_20d_high_pct": round(distance_pct, 3),
            "breakout_confirmed": last["close"] > prior_high,
        }

    # 3. Institutional foreign flow is taken from current stock-summary, never
    # inferred from OHLCV or incorrectly labeled broker accumulation.

    # 4. Distribution risk: high volume + weak finish and negative OBV proxy.
    bar_range = last["high"] - last["low"]
    close_position = ((last["close"] - last["low"]) / bar_range
                      if bar_range > 0 else None)
    high_volume = last["volume"] / avg_vol
    delta_obv = sum(
        x["volume"] * (1 if x["close"] > y["close"] else
                       -1 if x["close"] < y["close"] else 0)
        for x, y in zip(recent[1:], recent[:-1])
    )
    five_volume = sum(x["volume"] for x in recent)
    obv_negative = max(0.0, -delta_obv / five_volume) if five_volume > 0 else 0.0
    if close_position is not None:
        weakness = (1.0 - close_position)
        score = _clamp(50.0 * min(1.0, max(0.0, high_volume - 1.0)) * weakness
                       + 50.0 * obv_negative)
        result["distribution_risk"] = {
            "score": score,
            "status": "HIGH" if score >= 70 else "WATCH" if score >= 40 else "LOW",
            "volume_ratio": round(high_volume, 3),
            "close_position": round(close_position, 3),
            "negative_obv_ratio": round(obv_negative, 3),
        }

    # 5. Entry quality: avoid overextended entries, verify proximity/support.
    tr_values = []
    for a, b in zip(rows[-15:], rows[-16:-1]):
        tr_values.append(max(a["high"] - a["low"],
                             abs(a["high"] - b["close"]),
                             abs(a["low"] - b["close"])))
    atr = sum(tr_values) / len(tr_values) if tr_values else None
    mean20 = sum(x["close"] for x in prev) / len(prev)
    if atr is not None and atr > 0 and mean20 > 0:
        extension_atr = (last["close"] - mean20) / atr
        stop_reference = min(x["low"] for x in recent)
        risk_pct = ((last["close"] - stop_reference) / last["close"] * 100.0
                    if last["close"] > stop_reference else None)
        entry_score = _clamp(100.0 - max(0.0, extension_atr - 0.75) * 35.0
                             - (max(0.0, risk_pct - 5.0) * 5.0
                                if risk_pct is not None else 0.0))
        result["entry_quality"] = {
            "score": entry_score, "status": "OBSERVED",
            "extension_atr": round(extension_atr, 3),
            "recent_support_idr": stop_reference,
            "risk_to_support_pct": round(risk_pct, 3) if risk_pct is not None else None,
            "vwap": None, "reward_risk_ratio": None,
        }
    return result


def publish_summary(cache: Any, payload: Any) -> dict:
    snapshot = summary_snapshot(payload)
    cache.set(_cache_key(SUMMARY_ENDPOINT, SUMMARY_PARAMS), snapshot)
    return snapshot


def publish_history(cache: Any, ticker: str, payload: Any) -> dict:
    calculated = history_features(ticker, payload)
    # Do not overwrite good historical features with malformed/empty payloads.
    if calculated["history_sessions"] >= 30:
        cache.set(_cache_key(HISTORY_ENDPOINT, {"ticker": ticker.strip().upper()}), calculated)
    return calculated


def read_ticker(cache: Any, ticker: str) -> dict:
    """Bot 1/Bot 2 read-only consumer; never requests ZAPI or writes cache."""
    code = ticker.strip().upper()
    summary = cache.get(_cache_key(SUMMARY_ENDPOINT, SUMMARY_PARAMS), SUMMARY_TTL_SECONDS)
    history = cache.get(_cache_key(HISTORY_ENDPOINT, {"ticker": code}), HISTORY_TTL_SECONDS)
    keys = ("accumulation_persistence", "breakout_readiness",
            "institutional_flow", "distribution_risk", "entry_quality")
    scores = {key: _missing("UNAVAILABLE") for key in keys}
    if isinstance(history, dict) and history.get("schema_version") == VERSION:
        scores.update({key: history[key] for key in keys if isinstance(history.get(key), dict)})
    if (isinstance(summary, dict) and summary.get("schema_version") == VERSION
            and isinstance(summary.get("tickers"), dict)):
        current = summary["tickers"].get(code)
        if isinstance(current, dict) and isinstance(current.get("institutional_flow"), dict):
            scores["institutional_flow"] = current["institutional_flow"]
    return {"schema_version": VERSION, "ticker": code, "scores": scores,
            "summary_observed_at": summary.get("observed_at") if isinstance(summary, dict) else None,
            "history_last_date": history.get("history_last_date") if isinstance(history, dict) else None,
            "available": any(x.get("score") is not None for x in scores.values())}
