from __future__ import annotations

import argparse
import sqlite3
from typing import Any

import pandas as pd

import tracker


def load_stock_history(ticker: str, length: int = 100) -> pd.DataFrame:
    ticker = ticker.strip().upper()
    payload = tracker.fetch_stock_history(ticker, length)
    if not isinstance(payload, dict):
        raise RuntimeError(f"Response stock-history {ticker} tidak valid.")
    items = payload.get("items") or []
    if not items:
        raise RuntimeError(f"Tidak ada stock-history untuk {ticker}.")

    df = pd.DataFrame(items)
    required = {"date", "open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise RuntimeError(f"Field stock-history tidak lengkap: {sorted(missing)}")

    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for col in ["open", "high", "low", "close", "volume", "value", "frequency"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["date", "open", "high", "low", "close"])

    # The provider may include exchange dates with no trade as zero-range rows
    # (high=low=volume=0 while close carries the previous price). They are not
    # candles and must not invalidate otherwise complete trading history.
    valid_hlcv = (
        (df["high"] > 0)
        & (df["low"] > 0)
        & (df["close"] > 0)
        & (df["volume"] >= 0)
        & (df["high"] >= df[["low", "close"]].max(axis=1))
        & (df["low"] <= df[["high", "close"]].min(axis=1))
    )
    df = (
        df.loc[valid_hlcv]
        .sort_values("date")
        .drop_duplicates(subset=["date"], keep="last")
        .reset_index(drop=True)
    )
    if len(df) < 20:
        raise RuntimeError(f"History {ticker} hanya {len(df)} hari. Minimal 20 hari diperlukan.")
    return df


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()

    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    df["tr"] = tr
    df["atr14"] = tr.rolling(14, min_periods=1).mean()

    df["avg_volume20"] = df["volume"].rolling(20, min_periods=1).mean()
    df["volume_ratio"] = (
        df["volume"] / df["avg_volume20"].replace(0, float("nan"))
    ).fillna(0.0)

    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    df["rsi14"] = 100 - (100 / (1 + rs))
    df.loc[(avg_loss == 0) & (avg_gain > 0), "rsi14"] = 100
    df["rsi14"] = df["rsi14"].fillna(50)

    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["macd"] = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]

    direction = (delta > 0).astype(int) - (delta < 0).astype(int)
    df["obv"] = (direction * df["volume"].fillna(0)).cumsum()
    df["obv_ema20"] = df["obv"].ewm(span=20, adjust=False).mean()
    return df



def calculate_fibonacci_golden_ratio(
    df: pd.DataFrame,
    lookback: int = 50,
) -> dict[str, Any]:
    """Calculate retracement levels and the 0.618-0.650 Golden Pocket.

    The impulse direction is inferred from which extreme occurred first inside
    the selected lookback window:
    - swing low before swing high -> bullish impulse
    - swing high before swing low -> bearish impulse

    This is informational only; it does not change the entry score.
    """
    if df is None or df.empty:
        raise ValueError("History Fibonacci kosong.")

    required = {"date", "high", "low", "close"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"Field Fibonacci tidak lengkap: {sorted(missing)}"
        )

    lookback = max(20, min(int(lookback), len(df)))
    window = (
        df.tail(lookback)
        .dropna(subset=["date", "high", "low", "close"])
        .reset_index(drop=True)
    )

    if len(window) < 2:
        raise ValueError("History Fibonacci tidak cukup.")

    low_pos = int(window["low"].astype(float).idxmin())
    high_pos = int(window["high"].astype(float).idxmax())

    swing_low = float(window.loc[low_pos, "low"])
    swing_high = float(window.loc[high_pos, "high"])

    if swing_high <= swing_low:
        raise ValueError("Swing high/low Fibonacci tidak valid.")

    low_date = pd.Timestamp(window.loc[low_pos, "date"]).date().isoformat()
    high_date = pd.Timestamp(window.loc[high_pos, "date"]).date().isoformat()
    latest = window.iloc[-1]
    close = float(latest["close"])
    latest_date = pd.Timestamp(latest["date"]).date().isoformat()

    direction = "BULLISH" if low_pos < high_pos else "BEARISH"
    price_range = swing_high - swing_low

    ratios = (0.382, 0.500, 0.618, 0.650, 0.786)
    levels: dict[float, float] = {}

    for ratio in ratios:
        if direction == "BULLISH":
            levels[ratio] = swing_high - price_range * ratio
        else:
            levels[ratio] = swing_low + price_range * ratio

    golden_low = min(levels[0.618], levels[0.650])
    golden_high = max(levels[0.618], levels[0.650])

    if golden_low <= close <= golden_high:
        position = "IN GOLDEN POCKET"
        distance_pct = 0.0
    elif close > golden_high:
        position = "ABOVE GOLDEN POCKET"
        distance_pct = (
            (close - golden_high) / golden_high * 100
            if golden_high > 0
            else 0.0
        )
    else:
        position = "BELOW GOLDEN POCKET"
        distance_pct = (
            (golden_low - close) / golden_low * 100
            if golden_low > 0
            else 0.0
        )

    return {
        "lookback": lookback,
        "direction": direction,
        "swing_low": swing_low,
        "swing_low_date": low_date,
        "swing_high": swing_high,
        "swing_high_date": high_date,
        "latest_date": latest_date,
        "latest_price": close,
        "fib_382": levels[0.382],
        "fib_500": levels[0.500],
        "fib_618": levels[0.618],
        "fib_650": levels[0.650],
        "fib_786": levels[0.786],
        "golden_low": golden_low,
        "golden_high": golden_high,
        "position": position,
        "distance_pct": distance_pct,
    }


def format_fibonacci_report(
    ticker: str,
    lookback: int = 50,
) -> str:
    ticker = ticker.strip().upper()
    requested = max(20, min(int(lookback), 100))
    history = load_stock_history(
        ticker,
        max(20, requested),
    )
    fib = calculate_fibonacci_golden_ratio(
        history,
        requested,
    )

    direction_note = (
        "retracement support"
        if fib["direction"] == "BULLISH"
        else "retracement resistance"
    )

    return "\n".join(
        [
            f"🌀 FIBONACCI GOLDEN RATIO — {ticker}",
            f"Data: {fib['latest_date']}",
            f"Lookback: {fib['lookback']} trading days",
            f"Impulse: {fib['direction']}",
            (
                f"Swing Low: {_price(fib['swing_low'])} "
                f"({fib['swing_low_date']})"
            ),
            (
                f"Swing High: {_price(fib['swing_high'])} "
                f"({fib['swing_high_date']})"
            ),
            "",
            f"> Fib 0.382: {_price(fib['fib_382'])}",
            f"> Fib 0.500: {_price(fib['fib_500'])}",
            f"> Fib 0.618: {_price(fib['fib_618'])}",
            f"> Fib 0.650: {_price(fib['fib_650'])}",
            f"> Fib 0.786: {_price(fib['fib_786'])}",
            "",
            (
                f"Golden Pocket 0.618–0.650: "
                f"{_price(fib['golden_low'])} – "
                f"{_price(fib['golden_high'])}"
            ),
            f"Latest Price: {_price(fib['latest_price'])}",
            f"Position: {fib['position']}",
            (
                f"Distance to Golden Pocket: "
                f"{fib['distance_pct']:.2f}%"
            ),
            f"Role: {direction_note}",
        ]
    )

def calculate_levels(df: pd.DataFrame) -> dict[str, Any]:
    latest = df.iloc[-1]
    close = float(latest["close"])
    ema20 = float(latest["ema20"])
    ema50 = float(latest["ema50"])
    atr = float(latest["atr14"])

    last10 = df.tail(10)
    last20 = df.tail(20)
    last50 = df.tail(min(50, len(df)))
    low10, low20, low50 = map(float, [last10["low"].min(), last20["low"].min(), last50["low"].min()])
    high10, high20, high50 = map(float, [last10["high"].max(), last20["high"].max(), last50["high"].max()])

    previous20 = df.iloc[:-1].tail(20)
    breakout_level = float(previous20["high"].max()) if not previous20.empty else high20

    raw_supports = [
        ("EMA20", ema20),
        ("EMA50", ema50),
        ("LOW10", low10),
        ("LOW20", low20),
        ("LOW50", low50),
    ]
    support_pool: list[dict[str, Any]] = []
    for source, value in raw_supports:
        if value <= 0 or value > close:
            continue
        duplicate = any(
            abs(value - x["value"]) < max(1.0, atr * 0.15)
            for x in support_pool
        )
        if not duplicate:
            support_pool.append({"source": source, "value": float(value)})
    support_pool.sort(key=lambda x: x["value"], reverse=True)

    if support_pool:
        support1 = float(support_pool[0]["value"])
        support1_source = str(support_pool[0]["source"])
    else:
        support1, support1_source = low10, "LOW10"

    min_gap = max(1.0, atr * 0.75)
    support2 = None
    support2_source = None
    for item in support_pool[1:]:
        candidate = float(item["value"])
        if support1 - candidate >= min_gap:
            support2 = candidate
            support2_source = str(item["source"])
            break

    if support2 is None:
        deeper = [x for x in [low20, low50] if x < support1 and support1 - x >= min_gap]
        if deeper:
            support2 = max(deeper)
            support2_source = "DEEP STRUCTURE"

    if support2 is None:
        support2 = max(1.0, support1 - max(atr, support1 * 0.03))
        support2_source = "ATR FALLBACK"

    resistance_candidates: list[float] = []
    for value in [breakout_level, high10, high20, high50]:
        value = float(value)
        if value <= close:
            continue
        duplicate = any(
            abs(value - existing) < max(1.0, atr * 0.15)
            for existing in resistance_candidates
        )
        if not duplicate:
            resistance_candidates.append(value)
    resistance_candidates.sort()

    return {
        "support1": support1,
        "support1_source": support1_source,
        "support2": float(support2),
        "support2_source": support2_source,
        "resistance1": resistance_candidates[0] if resistance_candidates else None,
        "resistance2": resistance_candidates[1] if len(resistance_candidates) >= 2 else None,
        "breakout_level": breakout_level,
    }


def determine_trend(close: float, ema20: float, ema50: float) -> str:
    if close > ema20 > ema50:
        return "BULLISH"
    if close < ema20 < ema50:
        return "BEARISH"
    return "NEUTRAL"


def analyze_entry(ticker: str, mode: str = "SWING", *, history: pd.DataFrame | None = None) -> dict[str, Any]:
    ticker = ticker.strip().upper()
    mode = mode.strip().upper()
    if mode not in {"SWING", "DAYTRADE"}:
        mode = "SWING"

    df = history if history is not None else add_indicators(load_stock_history(ticker, 100))
    latest = df.iloc[-1]
    close = float(latest["close"])
    ema20 = float(latest["ema20"])
    ema50 = float(latest["ema50"])
    atr = float(latest["atr14"])
    volume_ratio = float(latest["volume_ratio"])
    trend = determine_trend(close, ema20, ema50)
    levels = calculate_levels(df)
    fib = calculate_fibonacci_golden_ratio(df, 50)

    support1 = float(levels["support1"])
    support2 = float(levels["support2"])
    breakout_level = float(levels["breakout_level"])

    if mode == "DAYTRADE":
        pullback_width, invalidation_buffer, breakout_buffer = atr * 0.20, atr * 0.30, atr * 0.05
    else:
        pullback_width, invalidation_buffer, breakout_buffer = atr * 0.35, atr * 0.50, atr * 0.10

    entry1_low = max(1.0, support1 - pullback_width * 0.25)
    entry1_high = min(support1 + pullback_width, close)
    entry2_low = max(1.0, support2 - pullback_width * 0.25)
    entry2_high = support2 + pullback_width
    max_entry2_high = entry1_low - max(1.0, atr * 0.10)
    entry2_high = min(entry2_high, max_entry2_high)
    if entry2_high < entry2_low:
        entry2_high = entry2_low

    breakout_entry = breakout_level + breakout_buffer
    invalidation = max(1.0, support2 - invalidation_buffer)

    resistance1 = levels.get("resistance1")
    resistance2 = levels.get("resistance2")
    pullback_entry = (entry1_low + entry1_high) / 2
    atr_target1 = pullback_entry + atr * 2.0
    atr_target2 = pullback_entry + atr * 3.5

    if resistance1 is not None and float(resistance1) > pullback_entry:
        pullback_tp1 = min(float(resistance1), atr_target1)
    else:
        pullback_tp1 = atr_target1

    structural_tp2 = None
    for candidate in (resistance1, resistance2):
        if candidate is not None and float(candidate) > pullback_tp1:
            structural_tp2 = float(candidate)
            break
    pullback_tp2 = min(structural_tp2, atr_target2) if structural_tp2 is not None else atr_target2
    if pullback_tp1 <= pullback_entry:
        pullback_tp1 = pullback_entry + atr
    if pullback_tp2 <= pullback_tp1:
        pullback_tp2 = pullback_tp1 + atr

    breakout_tp1 = breakout_entry + atr
    breakout_tp2 = breakout_entry + atr * 2

    if volume_ratio >= 1.5:
        volume_status = "STRONG VOLUME"
    elif volume_ratio >= 1.0:
        volume_status = "NORMAL / SUPPORTIVE"
    else:
        volume_status = "WEAK VOLUME"

    if close > breakout_level and volume_ratio >= 1.5:
        breakout_status = "BREAKOUT CONFIRMED"
    elif close > breakout_level:
        breakout_status = "BREAKOUT — VOLUME BELUM KUAT"
    elif close >= breakout_level - atr * 0.5:
        breakout_status = "NEAR BREAKOUT"
    else:
        breakout_status = "NO BREAKOUT"

    if breakout_status == "BREAKOUT CONFIRMED":
        entry_state = "BREAKOUT CONFIRMED"
    elif entry1_low <= close <= entry1_high:
        entry_state = "PULLBACK ZONE"
    elif entry2_low <= close <= entry2_high:
        entry_state = "DEEP PULLBACK ZONE"
    elif trend == "BEARISH":
        entry_state = "WAIT REVERSAL CONFIRMATION"
    elif close < entry2_low:
        entry_state = "BELOW ENTRY STRUCTURE"
    else:
        entry_state = "WAIT PULLBACK OR BREAKOUT"

    return {
        "ticker": ticker,
        "mode": mode,
        "date": latest["date"].date().isoformat(),
        "open": float(latest["open"]),
        "high": float(latest["high"]),
        "low": float(latest["low"]),
        "close": close,
        "ema20": ema20,
        "ema50": ema50,
        "atr14": atr,
        "volume": float(latest["volume"]),
        "avg_volume20": float(latest["avg_volume20"]),
        "volume_ratio": volume_ratio,
        "trend": trend,
        "volume_status": volume_status,
        "breakout_status": breakout_status,
        "entry_state": entry_state,
        "support1": support1,
        "support1_source": levels.get("support1_source"),
        "support2": support2,
        "support2_source": levels.get("support2_source"),
        "resistance1": resistance1,
        "resistance2": resistance2,
        "breakout_level": breakout_level,
        "breakout_entry": breakout_entry,
        "fib_direction": fib["direction"],
        "fib_swing_low": fib["swing_low"],
        "fib_swing_high": fib["swing_high"],
        "fib_618": fib["fib_618"],
        "fib_650": fib["fib_650"],
        "fib_golden_low": fib["golden_low"],
        "fib_golden_high": fib["golden_high"],
        "fib_position": fib["position"],
        "fib_distance_pct": fib["distance_pct"],
        "entry1_low": entry1_low,
        "entry1_high": entry1_high,
        "entry2_low": entry2_low,
        "entry2_high": entry2_high,
        "invalidation": invalidation,
        "pullback_tp1": pullback_tp1,
        "pullback_tp2": pullback_tp2,
        "breakout_tp1": breakout_tp1,
        "breakout_tp2": breakout_tp2,
    }


def get_broker_flow(ticker: str, trading_days: int, end_date: str | None = None) -> dict[str, Any]:
    ticker = ticker.upper().strip()
    if trading_days < 1:
        raise ValueError("trading_days must be positive")
    con = sqlite3.connect(tracker.DB_PATH)
    con.row_factory = sqlite3.Row
    dates = con.execute(
        """
        SELECT DISTINCT trade_date
        FROM broker_daily
        WHERE ticker = ? AND (? IS NULL OR trade_date <= ?)
        ORDER BY trade_date DESC
        LIMIT ?
        """,
        (ticker, end_date, end_date, int(trading_days)),
    ).fetchall()

    if not dates:
        con.close()
        return {
            "available": False,
            "days": trading_days,
            "actual_days": 0,
            "data_status": "NO DATA",
            "end_date": None,
            "net_lots": 0,
            "net_value": 0,
            "flow": "NO DATA",
            "dominant_buyer": None,
            "dominant_avg_buy": None,
            "dominant_seller": None,
            "dominant_avg_sell": None,
        }

    selected_dates = [str(row["trade_date"]) for row in dates]
    requested_end = end_date
    start_date, end_date = min(selected_dates), max(selected_dates)
    rows = con.execute(
        """
        SELECT
            broker,
            SUM(CASE WHEN side = 'BUY' THEN lots ELSE 0 END) AS buy_lots,
            SUM(CASE WHEN side = 'SELL' THEN lots ELSE 0 END) AS sell_lots,
            SUM(CASE WHEN side = 'BUY' THEN value ELSE 0 END) AS buy_value,
            SUM(CASE WHEN side = 'SELL' THEN value ELSE 0 END) AS sell_value
        FROM broker_daily
        WHERE ticker = ?
          AND trade_date BETWEEN ? AND ?
        GROUP BY broker
        """,
        (ticker, start_date, end_date),
    ).fetchall()
    con.close()

    broker_rows: list[dict[str, Any]] = []
    for row in rows:
        buy_lots = float(row["buy_lots"] or 0)
        sell_lots = float(row["sell_lots"] or 0)
        buy_value = float(row["buy_value"] or 0)
        sell_value = float(row["sell_value"] or 0)
        broker_rows.append(
            {
                "broker": row["broker"],
                "buy_lots": buy_lots,
                "sell_lots": sell_lots,
                "buy_value": buy_value,
                "sell_value": sell_value,
                "net_lots": buy_lots - sell_lots,
                "net_value": buy_value - sell_value,
            }
        )

    total_buy_lots = sum(x["buy_lots"] for x in broker_rows)
    total_sell_lots = sum(x["sell_lots"] for x in broker_rows)
    total_buy_value = sum(x["buy_value"] for x in broker_rows)
    total_sell_value = sum(x["sell_value"] for x in broker_rows)
    net_lots = total_buy_lots - total_sell_lots
    net_value = total_buy_value - total_sell_value

    if net_lots > 0 and net_value > 0:
        flow = "ACCUMULATION CONFIRMED"
    elif net_lots < 0 and net_value < 0:
        flow = "DISTRIBUTION CONFIRMED"
    else:
        flow = "MIXED"

    positive = [x for x in broker_rows if x["net_value"] > 0]
    negative = [x for x in broker_rows if x["net_value"] < 0]
    dominant_buyer = dominant_avg_buy = dominant_seller = dominant_avg_sell = None

    if positive:
        dom = max(positive, key=lambda x: x["net_value"])
        dominant_buyer = dom["broker"]
        if dom["buy_lots"] > 0:
            dominant_avg_buy = dom["buy_value"] / (dom["buy_lots"] * 100)

    if negative:
        dom = min(negative, key=lambda x: x["net_value"])
        dominant_seller = dom["broker"]
        if dom["sell_lots"] > 0:
            dominant_avg_sell = dom["sell_value"] / (dom["sell_lots"] * 100)

    data_status = "COMPLETE"
    if requested_end and end_date != requested_end:
        data_status = "STALE"
    elif len(selected_dates) < trading_days:
        data_status = "INCOMPLETE"
    if data_status != "COMPLETE":
        flow = data_status

    return {
        "available": True,
        "days": trading_days,
        "actual_days": len(selected_dates),
        "data_status": data_status,
        "start_date": start_date,
        "end_date": end_date,
        "net_lots": net_lots,
        "net_value": net_value,
        "flow": flow,
        "dominant_buyer": dominant_buyer,
        "dominant_avg_buy": dominant_avg_buy,
        "dominant_seller": dominant_seller,
        "dominant_avg_sell": dominant_avg_sell,
    }


def classify_rr(rr: float) -> str:
    if rr < 1.0:
        return "POOR"
    if rr < 1.5:
        return "LOW"
    if rr < 3.0:
        return "NORMAL"
    if rr < 5.0:
        return "ATTRACTIVE"
    return "EXTENDED"


def _broker_flow_points(flow: str, maximum: int) -> int:
    flow = str(flow).upper()
    if "ACCUMULATION CONFIRMED" in flow:
        return maximum
    if "MIXED" in flow:
        return round(maximum * 0.45)
    return 0


def classify_setup_score(score: int) -> str:
    if score >= 85:
        return "HIGH CONFLUENCE"
    if score >= 75:
        return "STRONG SETUP"
    if score >= 65:
        return "SETUP CONFIRMED"
    if score >= 55:
        return "WATCHLIST"
    if score >= 40:
        return "EARLY SETUP"
    return "WEAK SETUP"


def build_entry_score(ticker: str, mode: str = "SWING", *, history: pd.DataFrame | None = None) -> dict[str, Any]:
    ticker = ticker.upper().strip()
    df = add_indicators(history if history is not None else load_stock_history(ticker, 100))
    data = analyze_entry(ticker, mode, history=df)
    latest = df.iloc[-1]
    previous = df.iloc[-2] if len(df) >= 2 else latest

    rsi = float(latest["rsi14"])
    previous_rsi = float(previous["rsi14"])
    macd_hist = float(latest["macd_hist"])
    previous_macd_hist = float(previous["macd_hist"])
    obv = float(latest["obv"])
    obv_ema20 = float(latest["obv_ema20"])

    broker_today = get_broker_flow(ticker, 1, end_date=data["date"])
    broker_5d = get_broker_flow(ticker, 5, end_date=data["date"])
    broker_20d = get_broker_flow(ticker, 20, end_date=data["date"])

    broker_score = (
        _broker_flow_points(broker_today["flow"], 8)
        + _broker_flow_points(broker_5d["flow"], 15)
        + _broker_flow_points(broker_20d["flow"], 12)
    )

    positive_broker = sum(
        1
        for item in (broker_today, broker_5d, broker_20d)
        if item["available"] and item["flow"] == "ACCUMULATION CONFIRMED"
    )
    negative_broker = sum(
        1
        for item in (broker_today, broker_5d, broker_20d)
        if item["available"] and item["flow"] == "DISTRIBUTION CONFIRMED"
    )
    broker_bias = "ACCUMULATION" if positive_broker >= 2 else "DISTRIBUTION" if negative_broker >= 2 else "MIXED"

    close = float(data["close"])
    ema20 = float(data["ema20"])
    ema50 = float(data["ema50"])
    atr = float(data["atr14"])

    if close > ema20 > ema50:
        technical_score = 25
    elif close > ema20 and close > ema50:
        technical_score = 21
    elif close > ema20:
        technical_score = 18
    elif ema20 > ema50 and close >= ema20 - atr * 0.50:
        technical_score = 14
    elif close >= ema20 - atr * 0.50:
        technical_score = 10
    else:
        technical_score = 4
    if obv > obv_ema20 and technical_score < 25:
        technical_score = min(25, technical_score + 2)

    if 45 <= rsi <= 65 and rsi >= previous_rsi:
        rsi_score = 7
    elif 40 <= rsi <= 70 and rsi >= previous_rsi:
        rsi_score = 5
    elif 35 <= rsi <= 75:
        rsi_score = 3
    else:
        rsi_score = 1

    if macd_hist > 0 and macd_hist >= previous_macd_hist:
        macd_score = 8
    elif macd_hist > 0:
        macd_score = 6
    elif macd_hist > previous_macd_hist:
        macd_score = 4
    else:
        macd_score = 0
    momentum_score = rsi_score + macd_score

    volume_ratio = float(data["volume_ratio"])
    if volume_ratio >= 1.50:
        volume_score = 10
    elif volume_ratio >= 1.20:
        volume_score = 8
    elif volume_ratio >= 1.00:
        volume_score = 6
    elif volume_ratio >= 0.80:
        volume_score = 3
    else:
        volume_score = 1

    pullback_entry = (float(data["entry1_low"]) + float(data["entry1_high"])) / 2
    pullback_risk = pullback_entry - float(data["invalidation"])
    pullback_reward = float(data["pullback_tp1"]) - pullback_entry
    rr_pullback = pullback_reward / pullback_risk if pullback_risk > 0 else 0.0

    breakout_stop = max(1.0, float(data["breakout_level"]) - atr * 0.50)
    breakout_risk = float(data["breakout_entry"]) - breakout_stop
    breakout_reward = float(data["breakout_tp1"]) - float(data["breakout_entry"])
    rr_breakout = breakout_reward / breakout_risk if breakout_risk > 0 else 0.0

    if data["breakout_status"] in {"BREAKOUT CONFIRMED", "BREAKOUT — VOLUME BELUM KUAT", "NEAR BREAKOUT"}:
        selected_rr, selected_plan = rr_breakout, "BREAKOUT"
    else:
        selected_rr, selected_plan = rr_pullback, "PULLBACK"

    if selected_rr >= 3.0:
        rr_score = 15
    elif selected_rr >= 2.0:
        rr_score = 12
    elif selected_rr >= 1.50:
        rr_score = 10
    elif selected_rr >= 1.20:
        rr_score = 6
    elif selected_rr >= 1.00:
        rr_score = 3
    else:
        rr_score = 0

    score = int(max(0, min(100, broker_score + technical_score + momentum_score + volume_score + rr_score)))
    setup_class = classify_setup_score(score)

    filters: list[str] = []
    hard_avoid = False
    wait_required = False
    for name, flow in (("1D", broker_today), ("5D", broker_5d), ("20D", broker_20d)):
        if flow.get("data_status") != "COMPLETE":
            wait_required = True
            filters.append(f"Broker {name}: {flow.get('data_status', 'NO DATA')} "
                           f"({flow.get('actual_days', 0)}/{flow['days']} hari, "
                           f"terakhir {flow.get('end_date') or '-'}).")

    if broker_5d["flow"] == "DISTRIBUTION CONFIRMED" and broker_20d["flow"] == "DISTRIBUTION CONFIRMED":
        hard_avoid = True
        filters.append("Broker 5D + 20D masih distribusi.")

    if selected_rr < 1.20:
        wait_required = True
        filters.append(f"Risk/Reward belum menarik ({selected_rr:.2f}).")

    extension_atr = (close - ema20) / atr if atr > 0 else 0.0
    if extension_atr >= 2.0:
        wait_required = True
        filters.append("Harga terlalu jauh dari EMA20; tunggu pullback.")

    momentum_improving = rsi >= previous_rsi or macd_hist > previous_macd_hist
    if data["trend"] == "BEARISH" and broker_bias != "ACCUMULATION" and not momentum_improving:
        wait_required = True
        filters.append("Trend bearish dan momentum belum berbalik.")

    if data["breakout_status"] == "BREAKOUT — VOLUME BELUM KUAT":
        wait_required = True
        filters.append("Breakout belum didukung volume.")

    if hard_avoid:
        status, filter_status = "🔴 AVOID — BROKER DISTRIBUTION", "BLOCK"
    elif data["trend"] == "BEARISH" and broker_bias == "ACCUMULATION":
        status, filter_status = "🟡 EARLY ACCUMULATION — WAIT REVERSAL", "WAIT"
    elif data["breakout_status"] == "BREAKOUT CONFIRMED" and score >= 65 and selected_rr >= 1.50 and not wait_required:
        status, filter_status = "🚀 BREAKOUT CONFIRMED", "PASS"
    elif data["entry_state"] in {"PULLBACK ZONE", "DEEP PULLBACK ZONE"} and score >= 65 and selected_rr >= 1.50 and not wait_required:
        status, filter_status = "🟢 ENTRY ZONE — SCALE IN", "PASS"
    elif wait_required:
        status, filter_status = "🟡 WAIT — FILTER BELUM LOLOS", "WAIT"
    elif score >= 85:
        status, filter_status = "🟢 HIGH CONFLUENCE — WAIT ENTRY TRIGGER", "READY"
    elif score >= 75:
        status, filter_status = "🟢 STRONG SETUP", "READY"
    elif score >= 65:
        status, filter_status = "🟠 SETUP CONFIRMED", "READY"
    elif score >= 55:
        status, filter_status = "🔵 WATCHLIST", "WATCH"
    elif score >= 40:
        status, filter_status = "🟡 EARLY SETUP", "WATCH"
    else:
        status, filter_status = "🔴 WEAK SETUP", "WAIT"

    if not filters:
        filters.append("Tidak ada blocking condition utama.")

    result = dict(data)
    result.update(
        {
            "rsi14": rsi,
            "previous_rsi": previous_rsi,
            "macd_hist": macd_hist,
            "previous_macd_hist": previous_macd_hist,
            "broker_today": broker_today,
            "broker_5d": broker_5d,
            "broker_20d": broker_20d,
            "broker_bias": broker_bias,
            "broker_score": broker_score,
            "technical_score": technical_score,
            "momentum_score": momentum_score,
            "volume_score": volume_score,
            "rr_score": rr_score,
            "score": score,
            "setup_class": setup_class,
            "rr_pullback": rr_pullback,
            "rr_breakout": rr_breakout,
            "selected_rr": selected_rr,
            "selected_plan": selected_plan,
            "rr_pullback_label": classify_rr(rr_pullback),
            "rr_breakout_label": classify_rr(rr_breakout),
            "selected_rr_label": classify_rr(selected_rr),
            "filter_status": filter_status,
            "filters": filters,
            "status": status,
            "extension_atr": extension_atr,
        }
    )
    return result


def _price(value: float | None) -> str:
    return "-" if value is None else f"{value:,.0f}"


def format_scanner_report(ticker: str, mode: str = "SWING") -> str:
    data = build_entry_score(ticker, mode)
    lines = [
        f"🚦 ENTRY SCANNER — {data['ticker']} {data['mode']}",
        f"Date: {data['date']}",
        f"Latest Price: {_price(data['close'])}",
        "",
        f"⭐ SCORE: {data['score']}/100",
        f"Setup: {data['setup_class']}",
        f"Status: {data['status']}",
        f"Entry Filter: {data['filter_status']}",
        "",
        "📊 SCORE BREAKDOWN",
        f"> Broker Flow: {data['broker_score']}/35",
        f"> Technical: {data['technical_score']}/25",
        f"> Momentum: {data['momentum_score']}/15",
        f"> Volume: {data['volume_score']}/10",
        f"> Risk/Reward: {data['rr_score']}/15",
        "",
        "🏦 BROKER FLOW",
        f"> 1D (s.d. {data['broker_today'].get('end_date') or '-'}): {data['broker_today']['flow']}",
        f"> 5D: {data['broker_5d']['flow']}",
        f"> 20D: {data['broker_20d']['flow']}",
        f"> Bias: {data['broker_bias']}",
    ]

    b5 = data["broker_5d"]
    if b5.get("dominant_buyer"):
        lines.append(f"> Top Net Buyer 5D: {b5['dominant_buyer']}")
    if b5.get("dominant_avg_buy") is not None:
        lines.append(f"> Avg Buy {b5['dominant_buyer']}: {_price(b5['dominant_avg_buy'])}")
    if b5.get("dominant_seller"):
        lines.append(f"> Top Net Seller 5D: {b5['dominant_seller']}")
    if b5.get("dominant_avg_sell") is not None:
        lines.append(f"> Avg Sell {b5['dominant_seller']}: {_price(b5['dominant_avg_sell'])}")

    lines.extend(
        [
            "",
            "📈 MARKET STRUCTURE",
            f"> Trend: {data['trend']}",
            f"> EMA20 / EMA50: {_price(data['ema20'])} / {_price(data['ema50'])}",
            f"> RSI14: {data['rsi14']:.1f}",
            f"> MACD Hist: {data['macd_hist']:+.2f}",
            f"> Volume: {data['volume_ratio']:.2f}x Avg20",
            "",
            "🌀 FIBONACCI GOLDEN RATIO",
            f"> Impulse: {data['fib_direction']}",
            (
                f"> Swing: {_price(data['fib_swing_low'])} "
                f"→ {_price(data['fib_swing_high'])}"
            ),
            (
                f"> Golden Pocket 0.618–0.650: "
                f"{_price(data['fib_golden_low'])} – "
                f"{_price(data['fib_golden_high'])}"
            ),
            f"> Fib 0.618: {_price(data['fib_618'])}",
            f"> Fib 0.650: {_price(data['fib_650'])}",
            f"> Position: {data['fib_position']}",
            "",
            "🚦 ENTRY FILTER",
        ]
    )
    lines.extend(f"> {reason}" for reason in data["filters"])

    lines.extend(
        [
            "",
            "🟡 PULLBACK PLAN",
            f"> Entry 1: {_price(data['entry1_low'])} – {_price(data['entry1_high'])}",
            f"> Entry 2: {_price(data['entry2_low'])} – {_price(data['entry2_high'])}",
            f"> Invalidation: < {_price(data['invalidation'])}",
            f"> TP1: {_price(data['pullback_tp1'])}",
            f"> TP2: {_price(data['pullback_tp2'])}",
            f"> R:R: {data['rr_pullback']:.2f} | {data['rr_pullback_label']}",
            "",
            "🚀 BREAKOUT PLAN",
            f"> Trigger: > {_price(data['breakout_entry'])}",
            f"> Breakout Status: {data['breakout_status']}",
            "> Volume Confirmation: >= 1.50x Avg20",
            f"> TP1: {_price(data['breakout_tp1'])}",
            f"> TP2: {_price(data['breakout_tp2'])}",
            f"> R:R: {data['rr_breakout']:.2f} | {data['rr_breakout_label']}",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("ticker")
    parser.add_argument("--mode", default="SWING", choices=["SWING", "DAYTRADE"])
    args = parser.parse_args()
    print(format_scanner_report(args.ticker, args.mode))


if __name__ == "__main__":
    main()


