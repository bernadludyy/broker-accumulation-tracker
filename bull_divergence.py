"""Cache-only staged regular bullish-divergence screener for Telegram Bot 1."""
from __future__ import annotations

import math
import os
from collections import defaultdict
from typing import Any

import pandas as pd

from market_regime import detect_regime
from project_fabric.postgres_backend import configured_database_url

try:
    import psycopg
except ImportError:  # pragma: no cover - optional outside GitHub runtime.
    psycopg = None


HISTORY_LIMIT = max(60, int(os.getenv("BULL_DIV_HISTORY_LIMIT", "100")))
MIN_TURNOVER_IDR = float(os.getenv("BULL_DIV_MIN_TURNOVER", "2000000000"))
MIN_PRICE = float(os.getenv("BULL_DIV_MIN_PRICE", "50"))
MIN_RSI_GAP = float(os.getenv("BULL_DIV_MIN_RSI_GAP", "3"))
MIN_PRICE_LOWER_PCT = float(os.getenv("BULL_DIV_MIN_PRICE_LOWER_PCT", "0.5"))
MAX_PRICE_LOWER_PCT = float(os.getenv("BULL_DIV_MAX_PRICE_LOWER_PCT", "15"))
MAX_SECOND_RSI = float(os.getenv("BULL_DIV_MAX_SECOND_RSI", "55"))
MAX_PIVOT_AGE = int(os.getenv("BULL_DIV_MAX_PIVOT_AGE", "7"))
MAX_PIVOT_DISTANCE = int(os.getenv("BULL_DIV_MAX_PIVOT_DISTANCE", "30"))
PIVOT_LEFT_BARS = max(1, int(os.getenv("BULL_DIV_PIVOT_LEFT_BARS", "2")))
PIVOT_RIGHT_BARS = max(1, int(os.getenv("BULL_DIV_PIVOT_RIGHT_BARS", "2")))
MAX_EXTENSION_PCT = float(os.getenv("BULL_DIV_MAX_EXTENSION_PCT", "5"))
MAX_EXTENSION_ATR = float(os.getenv("BULL_DIV_MAX_EXTENSION_ATR", "1.25"))


def _number(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _rsi14(close: pd.Series) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    relative = avg_gain / avg_loss.replace(0, float("nan"))
    result = 100 - (100 / (1 + relative))
    result.loc[(avg_loss == 0) & (avg_gain > 0)] = 100
    return result


def _atr14(frame: pd.DataFrame) -> pd.Series:
    previous_close = frame["close"].shift(1)
    ranges = pd.concat(
        (
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ),
        axis=1,
    )
    return ranges.max(axis=1).rolling(14, min_periods=5).mean()


def _pivot_lows(frame: pd.DataFrame) -> list[dict[str, Any]]:
    """Return local lows plus whether enough right bars confirm each pivot.

    The newest bar may be emitted as a provisional pivot for an EARLY alert.  A
    prior pivot is confirmed only after ``PIVOT_RIGHT_BARS`` later lows hold.
    """
    pivots: list[dict[str, Any]] = []
    for index in range(PIVOT_LEFT_BARS, len(frame)):
        low = float(frame.iloc[index]["low"])
        left = frame.iloc[index - PIVOT_LEFT_BARS:index]["low"]
        if left.empty or low > float(left.min()):
            continue
        right = frame.iloc[index + 1:min(len(frame), index + 1 + PIVOT_RIGHT_BARS)]["low"]
        if not right.empty and low > float(right.min()):
            continue
        if pd.isna(frame.iloc[index]["rsi14"]):
            continue
        right_bars = len(right)
        item = {
            "index": index,
            "confirmed": right_bars >= PIVOT_RIGHT_BARS,
            "right_bars": right_bars,
        }
        # Consecutive bars can describe one bottoming cluster. Keep its lowest
        # point instead of manufacturing two pivots from the same swing.
        if pivots and index - int(pivots[-1]["index"]) <= PIVOT_LEFT_BARS:
            previous = int(pivots[-1]["index"])
            if low <= float(frame.iloc[previous]["low"]):
                pivots[-1] = item
            continue
        pivots.append(item)
    return pivots


def _status_for_candidate(
    frame: pd.DataFrame,
    second: int,
    *,
    pivot_confirmed: bool,
    current_close: float,
) -> tuple[str, str, bool, float, float | None]:
    latest = frame.iloc[-1]
    second_row = frame.iloc[second]
    pivot_age = len(frame) - 1 - second
    latest_rsi = float(latest["rsi14"])
    second_rsi = float(second_row["rsi14"])
    extension_pct = (current_close / float(second_row["low"]) - 1) * 100
    atr = _number(latest.get("atr14"))
    extension_atr = (
        (current_close - float(second_row["low"])) / atr
        if atr and atr > 0
        else None
    )
    after = frame.iloc[second + 1:]
    no_new_low = after.empty or float(after["low"].min()) >= float(second_row["low"])
    rsi_turning_up = latest_rsi > second_rsi
    one_bar_reversal = (
        pivot_age >= 1
        and no_new_low
        and current_close > float(second_row["close"])
        and rsi_turning_up
    )
    confirmed = (
        pivot_confirmed
        and no_new_low
        and current_close > float(second_row["high"])
        and rsi_turning_up
    )
    extended = pivot_age >= 1 and (
        extension_pct > MAX_EXTENSION_PCT
        or (extension_atr is not None and extension_atr > MAX_EXTENSION_ATR)
    )
    if extended:
        return (
            "LATE/EXTENDED",
            f"harga sudah +{extension_pct:.1f}% dari pivot low; tunggu pullback",
            confirmed,
            extension_pct,
            extension_atr,
        )
    if confirmed:
        return (
            "CONFIRMED",
            "pivot terkonfirmasi dan close melewati high candle divergence",
            True,
            extension_pct,
            extension_atr,
        )
    if one_bar_reversal:
        return (
            "VALIDATED",
            "1-candle reversal: low bertahan, close dan RSI berbalik naik",
            False,
            extension_pct,
            extension_atr,
        )
    return (
        "EARLY",
        "pivot terbaru masih provisional; tunggu satu candle reversal",
        False,
        extension_pct,
        extension_atr,
    )


def analyze_divergence(
    ticker: str,
    history: list[dict[str, Any]],
    summary: dict[str, Any],
) -> dict[str, Any] | None:
    """Detect staged regular bullish divergence from adjacent pivot lows."""
    points: list[dict[str, Any]] = []
    for row in history:
        day = str(row.get("trade_date") or row.get("date") or "")[:10]
        low = _number(row.get("low_price", row.get("low")))
        high = _number(row.get("high_price", row.get("high")))
        close = _number(row.get("close_price", row.get("close")))
        volume = _number(row.get("volume_shares", row.get("volume")))
        if day and min(low or 0, high or 0, close or 0) > 0 and volume is not None:
            points.append({
                "date": day,
                "low": low,
                "high": high,
                "close": close,
                "volume": max(0.0, volume),
                "foreign_buy": _number(row.get("foreign_buy_value_idr")),
                "foreign_sell": _number(row.get("foreign_sell_value_idr")),
            })
    summary_day = str(summary.get("trade_date") or "")[:10]
    summary_low = _number(summary.get("low_price"))
    summary_high = _number(summary.get("high_price"))
    summary_close = _number(summary.get("close_price"))
    summary_volume = _number(summary.get("volume_shares"))
    if (
        summary_day
        and min(summary_low or 0, summary_high or 0, summary_close or 0) > 0
        and summary_volume is not None
        and (not points or summary_day > max(row["date"] for row in points))
    ):
        points.append({
            "date": summary_day,
            "low": summary_low,
            "high": summary_high,
            "close": summary_close,
            "volume": max(0.0, summary_volume),
            "foreign_buy": None,
            "foreign_sell": None,
        })
    if len(points) < 35:
        return None

    frame = pd.DataFrame(points).drop_duplicates("date", keep="last").sort_values("date")
    if len(frame) < 35:
        return None
    frame["rsi14"] = _rsi14(frame["close"])
    frame["volume_ma20"] = frame["volume"].rolling(20).mean()
    frame["atr14"] = _atr14(frame)
    pivots = _pivot_lows(frame)
    if len(pivots) < 2:
        return None

    selected: tuple[dict[str, Any], dict[str, Any]] | None = None
    # Only adjacent swing lows may form a divergence. This deliberately avoids
    # the old COIN false positive where a much lower valley existed between the
    # two dates selected by the screener.
    for second_pos in range(len(pivots) - 1, 0, -1):
        first_meta = pivots[second_pos - 1]
        second_meta = pivots[second_pos]
        first = int(first_meta["index"])
        second = int(second_meta["index"])
        if len(frame) - 1 - second > MAX_PIVOT_AGE:
            continue
        distance = second - first
        if distance < 3 or distance > MAX_PIVOT_DISTANCE:
            continue
        first_low = float(frame.iloc[first]["low"])
        second_low = float(frame.iloc[second]["low"])
        intermediate = frame.iloc[first + 1:second]["low"]
        if not intermediate.empty and float(intermediate.min()) < second_low:
            continue
        price_lower_pct = (first_low / second_low - 1) * 100
        first_rsi = float(frame.iloc[first]["rsi14"])
        second_rsi = float(frame.iloc[second]["rsi14"])
        if not MIN_PRICE_LOWER_PCT <= price_lower_pct <= MAX_PRICE_LOWER_PCT:
            continue
        if second_rsi - first_rsi < MIN_RSI_GAP or second_rsi > MAX_SECOND_RSI:
            continue
        selected = (first_meta, second_meta)
        break
    if selected is None:
        return None

    first_meta, second_meta = selected
    first = int(first_meta["index"])
    second = int(second_meta["index"])
    first_row = frame.iloc[first]
    second_row = frame.iloc[second]
    latest = frame.iloc[-1]
    turnover = _number(summary.get("turnover_idr")) or 0.0
    current_close = _number(summary.get("close_price")) or float(latest["close"])
    if current_close < MIN_PRICE or turnover < MIN_TURNOVER_IDR:
        return None

    price_lower_pct = (float(first_row["low"]) / float(second_row["low"]) - 1) * 100
    rsi_gap = float(second_row["rsi14"] - first_row["rsi14"])
    status, status_reason, confirmed, extension_pct, extension_atr = _status_for_candidate(
        frame,
        second,
        pivot_confirmed=bool(second_meta["confirmed"]),
        current_close=current_close,
    )
    volume_ma20 = _number(latest["volume_ma20"])
    volume_ratio = float(latest["volume"] / volume_ma20) if volume_ma20 else None
    recent = frame.tail(5)
    foreign_values = [
        float(buy - sell)
        for buy, sell in zip(recent["foreign_buy"], recent["foreign_sell"])
        if pd.notna(buy) and pd.notna(sell)
    ]
    foreign_5d = sum(foreign_values) if foreign_values else None
    status_bonus = {"CONFIRMED": 12, "VALIDATED": 8, "EARLY": 3, "LATE/EXTENDED": -12}[status]
    score = min(100.0, max(0.0, (
        35 + min(25, rsi_gap * 2.5)
        + min(15, price_lower_pct * 2)
        + status_bonus
        + (8 if volume_ratio is not None and volume_ratio >= 1.2 else 0)
        + (7 if foreign_5d is not None and foreign_5d > 0 else 0)
    )))
    return {
        "ticker": ticker,
        "status": status,
        "status_reason": status_reason,
        "score": score,
        "confirmed": confirmed,
        "pivot_confirmed": bool(second_meta["confirmed"]),
        "pivot_age": len(frame) - 1 - second,
        "first_date": str(first_row["date"]),
        "first_low": float(first_row["low"]),
        "first_rsi": float(first_row["rsi14"]),
        "second_date": str(second_row["date"]),
        "second_low": float(second_row["low"]),
        "second_high": float(second_row["high"]),
        "second_rsi": float(second_row["rsi14"]),
        "price_lower_pct": price_lower_pct,
        "rsi_gap": rsi_gap,
        "current_close": current_close,
        "latest_rsi": float(latest["rsi14"]),
        "turnover_idr": turnover,
        "volume_ratio": volume_ratio,
        "foreign_5d": foreign_5d,
        "extension_pct": extension_pct,
        "extension_atr": extension_atr,
        "trigger_price": float(second_row["high"]),
        "invalidation_price": float(second_row["low"]),
    }


def _load_snapshot(
    ticker: str | None = None,
    database_url: str | None = None,
) -> tuple[str, list[dict[str, Any]], dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    url = (database_url if database_url is not None else configured_database_url()).strip()
    if not url or psycopg is None:
        raise ValueError("Neon histori harga belum terkonfigurasi")
    with psycopg.connect(url, connect_timeout=12, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT max(trade_date) FROM project_fabric.stock_summary_daily")
        market_day = cursor.fetchone()[0]
        if market_day is None:
            raise ValueError("Snapshot EOD belum tersedia")
        cursor.execute(
            "SELECT raw_json FROM project_fabric.stock_summary_daily WHERE trade_date=%s ORDER BY ticker",
            (market_day,),
        )
        regime_rows = [row[0] for row in cursor.fetchall() if isinstance(row[0], dict)]
        params: list[Any] = [market_day]
        ticker_filter = ""
        if ticker:
            ticker_filter = " AND ticker=%s"
            params.append(ticker)
        cursor.execute(
            """SELECT ticker, trade_date, close_price, turnover_idr,
                      open_price, high_price, low_price, volume_shares, raw_json
               FROM project_fabric.stock_summary_daily
               WHERE trade_date=%s""" + ticker_filter + " ORDER BY ticker",
            params,
        )
        columns = [item.name for item in cursor.description]
        summaries = [dict(zip(columns, row)) for row in cursor.fetchall()]
        tickers = [str(row["ticker"]).upper() for row in summaries]
        if not tickers:
            return str(market_day), [], {}, regime_rows
        cursor.execute(
            """WITH ranked AS (
                   SELECT ticker, trade_date, low_price, high_price, close_price,
                          volume_shares, foreign_buy_value_idr, foreign_sell_value_idr,
                          row_number() OVER (
                            PARTITION BY ticker ORDER BY trade_date DESC
                          ) AS row_number
                   FROM project_fabric.stock_history_daily
                   WHERE ticker=ANY(%s) AND trade_date<=%s
                 )
                 SELECT ticker, trade_date, low_price, high_price, close_price,
                        volume_shares, foreign_buy_value_idr, foreign_sell_value_idr
                 FROM ranked WHERE row_number<=%s
                 ORDER BY ticker, trade_date""",
            (tickers, market_day, HISTORY_LIMIT),
        )
        columns = [item.name for item in cursor.description]
        history_rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    histories: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in history_rows:
        histories[str(row["ticker"]).upper()].append(row)
    return str(market_day), summaries, dict(histories), regime_rows


def _rupiah(value: float | None) -> str:
    if value is None:
        return "N/A"
    sign = "+" if value > 0 else "-" if value < 0 else ""
    amount = abs(value)
    if amount >= 1e9:
        return f"{sign}Rp{amount / 1e9:.2f} miliar"
    if amount >= 1e6:
        return f"{sign}Rp{amount / 1e6:.1f} juta"
    return f"{sign}Rp{amount:,.0f}"


def format_bull_divergence(
    *,
    ticker: str | None = None,
    limit: int = 10,
    database_url: str | None = None,
) -> str:
    code = ticker.strip().upper() if ticker else None
    try:
        market_day, summaries, histories, regime_rows = _load_snapshot(code, database_url)
        candidates = []
        for summary in summaries:
            current = str(summary["ticker"]).upper()
            result = analyze_divergence(current, histories.get(current, []), summary)
            if result:
                candidates.append(result)
        priority = {"CONFIRMED": 0, "VALIDATED": 1, "EARLY": 2, "LATE/EXTENDED": 3}
        candidates.sort(key=lambda row: (priority[row["status"]], -row["score"], row["ticker"]))
        regime = detect_regime(regime_rows, f"NEON EOD {market_day}")
    except Exception as exc:
        print(f"[BULL-DIV-ERROR] {type(exc).__name__}: {str(exc)[:200]}")
        detail = str(exc) if isinstance(exc, ValueError) else "Data Neon gagal dibaca; coba lagi."
        return f"⏳ BULL DIVERGENCE — DATA BELUM VALID\n{detail}"

    lines = [
        f"📈 BULL DIVERGENCE — EOD {market_day}",
        "Tahap: EARLY → VALIDATED → CONFIRMED; LATE tidak boleh dikejar.",
        f"Market Gate: {regime.label} → {regime.mode}",
        f"Universe {len(summaries)} | Histori valid {sum(bool(histories.get(str(row['ticker']).upper())) for row in summaries)} | Kandidat {len(candidates)}",
        "Filter: pivot berurutan | tanpa lower low di tengah | RSI gap ≥3 | pivot ≤7 sesi.",
        "",
    ]
    shown = candidates[:max(1, min(int(limit), 10))]
    if not shown:
        lines.append(f"{code or 'Belum ada saham'} yang memenuhi regular bullish divergence valid.")
    for index, row in enumerate(shown, 1):
        volume = f"{row['volume_ratio']:.2f}x" if row["volume_ratio"] is not None else "N/A"
        pivot_state = "confirmed" if row["pivot_confirmed"] else "provisional"
        lines.extend((
            f"{index}. {row['ticker']} | {row['status']} | Score {row['score']:.0f}",
            f"   Harga: {row['first_date']} {row['first_low']:,.0f} → {row['second_date']} {row['second_low']:,.0f} (LL {row['price_lower_pct']:.2f}%)",
            f"   RSI14: {row['first_rsi']:.1f} → {row['second_rsi']:.1f} (HL +{row['rsi_gap']:.1f}) | kini {row['latest_rsi']:.1f}",
            f"   Close {row['current_close']:,.0f} | dari low +{row['extension_pct']:.1f}% | Vol/MA20 {volume} | F5D {_rupiah(row['foreign_5d'])}",
            f"   Trigger >{row['trigger_price']:,.0f} | Invalid <{row['invalidation_price']:,.0f} | pivot {pivot_state}",
        ))
    lines.extend((
        "",
        "EARLY = pivot provisional; VALIDATED = satu candle reversal; CONFIRMED = pivot + break high.",
        "LATE/EXTENDED = harga >5% atau >1.25 ATR dari low; tunggu pullback.",
        "ZAPI: 0 request — seluruh data dari Neon EOD.",
        (
            "⚠️ Market Gate CASH: hanya watchlist, jangan entry."
            if regime.mode == "CASH"
            else "⚠️ Tetap wajib cek broker/foreign, R:R, dan Bot 4 sebelum entry."
        ),
    ))
    return "\n".join(lines)
