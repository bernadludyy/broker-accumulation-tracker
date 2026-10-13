"""Cache-only EOD pre-screen for the Bot 1 Telegram command.

The ranker reads one synchronized snapshot from the Project Fabric Neon
warehouse.  It never calls ZAPI, never writes data, and never treats foreign
aggregate value as broker confirmation.  Broker gates remain the job of
``/refresh TICKER`` followed by ``BOT1 FULL TICKER``.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
import json
import math
from typing import Any

import pandas as pd

from .fundamental_intelligence import score_fundamental
from .postgres_backend import configured_database_url

try:
    import psycopg
except ImportError:  # pragma: no cover - optional outside GitHub runtime.
    psycopg = None


UNIVERSE_WEIGHTS = {
    "accumulation_persistence": 0.25,
    "breakout_readiness": 0.25,
    "institutional_flow": 0.20,
    "distribution_risk": 0.15,
    "entry_quality": 0.15,
}
MIN_HISTORY_DAYS = 50


def _rows(cursor) -> list[dict[str, Any]]:
    columns = [item.name for item in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _iso_date(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value or "")[:10]


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def universe_snapshot(scores_json: Any) -> dict[str, Any]:
    """Apply the same five-factor weighting as Universe Intelligence."""
    items = _json_object(scores_json)
    values: dict[str, float] = {}
    for key, weight in UNIVERSE_WEIGHTS.items():
        del weight
        item = items.get(key)
        if not isinstance(item, dict):
            continue
        score = _number(item.get("score"))
        if score is None or not 0 <= score <= 100:
            continue
        if key == "institutional_flow" and item.get("source") != "reported_foreign_value":
            continue
        values[key] = 100.0 - score if key == "distribution_risk" else score

    coverage = len(values)
    if coverage < 3:
        score = None
    else:
        numerator = sum(UNIVERSE_WEIGHTS[key] * value for key, value in values.items())
        denominator = sum(UNIVERSE_WEIGHTS[key] for key in values)
        score = round(numerator / denominator, 1) if denominator else None
    distribution = _number((items.get("distribution_risk") or {}).get("score"))
    return {
        "score": score,
        "coverage": coverage,
        "distribution_score": distribution,
        "risk_high": distribution is not None and distribution >= 70,
    }


def foreign_snapshot(history: pd.DataFrame) -> dict[str, Any]:
    """Score reported foreign value over the last five synchronized bars."""
    required = {"foreign_buy", "foreign_sell"}
    if history.empty or not required.issubset(history.columns):
        return {"score": None, "passed": False, "reason": "DATA TERBATAS"}
    recent = history.tail(5).copy()
    if len(recent) < 5 or recent[list(required)].isna().any().any():
        return {"score": None, "passed": False, "reason": "DATA TERBATAS"}
    recent["net_foreign"] = recent["foreign_buy"] - recent["foreign_sell"]
    net_1d = float(recent.iloc[-1]["net_foreign"])
    net_5d = float(recent["net_foreign"].sum())
    positive_days = int((recent["net_foreign"] > 0).sum())
    score = (35.0 if net_1d > 0 else 0.0) + (35.0 if net_5d > 0 else 0.0)
    score += 30.0 * positive_days / 5.0
    passed = net_1d > 0 and net_5d > 0 and positive_days >= 3
    return {
        "score": round(score, 1),
        "passed": passed,
        "net_1d": net_1d,
        "net_5d": net_5d,
        "positive_days": positive_days,
        "reason": "CONFIRMED" if passed else "BELUM CONFIRMED",
    }


def _score_rr(value: float) -> int:
    if value >= 3.0:
        return 15
    if value >= 2.0:
        return 12
    if value >= 1.5:
        return 10
    if value >= 1.2:
        return 6
    if value >= 1.0:
        return 3
    return 0


def technical_snapshot(history: pd.DataFrame, ticker: str) -> dict[str, Any]:
    """Reuse Bot 1 indicators and normalize the non-broker 65 points."""
    # Delayed import keeps pure helper tests independent from the SQLite bot.
    from entry_engine import add_indicators, analyze_entry

    indicators = add_indicators(history)
    setup = analyze_entry(ticker, "SWING", history=indicators)
    latest = indicators.iloc[-1]
    previous = indicators.iloc[-2]
    close = float(latest["close"])
    ema20 = float(latest["ema20"])
    ema50 = float(latest["ema50"])
    atr = float(latest["atr14"])

    if close > ema20 > ema50:
        trend_score = 25
    elif close > ema20 and close > ema50:
        trend_score = 21
    elif close > ema20:
        trend_score = 18
    elif ema20 > ema50 and close >= ema20 - atr * 0.5:
        trend_score = 14
    elif close >= ema20 - atr * 0.5:
        trend_score = 10
    else:
        trend_score = 4
    if float(latest["obv"]) > float(latest["obv_ema20"]) and trend_score < 25:
        trend_score = min(25, trend_score + 2)

    rsi = float(latest["rsi14"])
    previous_rsi = float(previous["rsi14"])
    if 45 <= rsi <= 65 and rsi >= previous_rsi:
        rsi_score = 7
    elif 40 <= rsi <= 70 and rsi >= previous_rsi:
        rsi_score = 5
    elif 35 <= rsi <= 75:
        rsi_score = 3
    else:
        rsi_score = 1

    macd = float(latest["macd_hist"])
    previous_macd = float(previous["macd_hist"])
    if macd > 0 and macd >= previous_macd:
        macd_score = 8
    elif macd > 0:
        macd_score = 6
    elif macd > previous_macd:
        macd_score = 4
    else:
        macd_score = 0
    momentum_score = rsi_score + macd_score

    volume_ratio = float(latest["volume_ratio"])
    if volume_ratio >= 1.5:
        volume_score = 10
    elif volume_ratio >= 1.2:
        volume_score = 8
    elif volume_ratio >= 1.0:
        volume_score = 6
    elif volume_ratio >= 0.8:
        volume_score = 3
    else:
        volume_score = 1

    atr = float(setup["atr14"])
    pullback_entry = (float(setup["entry1_low"]) + float(setup["entry1_high"])) / 2
    pullback_risk = pullback_entry - float(setup["invalidation"])
    pullback_reward = float(setup["pullback_tp1"]) - pullback_entry
    rr_pullback = pullback_reward / pullback_risk if pullback_risk > 0 else 0.0
    breakout_stop = max(1.0, float(setup["breakout_level"]) - atr * 0.5)
    breakout_risk = float(setup["breakout_entry"]) - breakout_stop
    breakout_reward = float(setup["breakout_tp1"]) - float(setup["breakout_entry"])
    rr_breakout = breakout_reward / breakout_risk if breakout_risk > 0 else 0.0
    if setup["breakout_status"] in {
        "BREAKOUT CONFIRMED", "BREAKOUT — VOLUME BELUM KUAT", "NEAR BREAKOUT"
    }:
        selected_rr, selected_plan = rr_breakout, "BREAKOUT"
    else:
        selected_rr, selected_plan = rr_pullback, "PULLBACK"
    rr_score = _score_rr(selected_rr)
    raw_score = trend_score + momentum_score + volume_score + rr_score
    normalized = round(raw_score / 65.0 * 100.0, 1)
    extension_atr = (close - ema20) / atr if atr > 0 else 0.0
    passed = (
        normalized >= 60
        and selected_rr >= 1.5
        and setup["trend"] != "BEARISH"
        and extension_atr < 2.0
        and setup["breakout_status"] != "BREAKOUT — VOLUME BELUM KUAT"
    )
    return {
        "score": normalized,
        "passed": passed,
        "trend": setup["trend"],
        "momentum_score": momentum_score,
        "volume_ratio": volume_ratio,
        "selected_rr": round(selected_rr, 2),
        "selected_plan": selected_plan,
        "entry_low": float(setup["entry1_low"] if selected_plan == "PULLBACK" else setup["breakout_entry"]),
        "entry_high": float(setup["entry1_high"] if selected_plan == "PULLBACK" else setup["breakout_entry"]),
        "invalidation": float(setup["invalidation"]),
        "tp1": float(setup["pullback_tp1"] if selected_plan == "PULLBACK" else setup["breakout_tp1"]),
    }


def combined_score(fundamental: float, technical: float, foreign: float,
                   universe: float | None) -> float:
    components = [(fundamental, 0.35), (technical, 0.35), (foreign, 0.20)]
    if universe is not None:
        components.append((universe, 0.10))
    numerator = sum(value * weight for value, weight in components)
    denominator = sum(weight for _, weight in components)
    return round(numerator / denominator, 1)


def _load_snapshot(database_url: str | None = None) -> dict[str, Any]:
    url = (database_url if database_url is not None else configured_database_url()).strip()
    if not url or psycopg is None:
        raise RuntimeError("Neon BOT1 RANK belum terkonfigurasi")
    with psycopg.connect(url, connect_timeout=12, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT ticker, company_name, sector, pe, pb, ps, roe_pct, roa_pct,
                      net_margin_pct, debt_equity_pct, current_ratio,
                      endpoint_status, updated_at
               FROM project_fabric.fundamental_latest
               ORDER BY ticker"""
        )
        fundamentals = _rows(cursor)
        cursor.execute(
            """WITH target AS (
                     SELECT max(trade_date) AS trade_date
                     FROM project_fabric.stock_summary_daily
                   ), coverage AS (
                     SELECT h.ticker, count(*) AS history_days
                     FROM project_fabric.stock_history_daily h, target t
                     WHERE h.trade_date <= t.trade_date
                     GROUP BY h.ticker
                     HAVING count(*) >= %s
                   )
               SELECT s.ticker, s.trade_date, s.open_price, s.high_price,
                      s.low_price, s.close_price, s.volume_shares,
                      s.foreign_buy_value_idr, s.foreign_sell_value_idr,
                      s.captured_at, c.history_days, u.scores_json,
                      u.available_factors, u.history_last_date
               FROM project_fabric.stock_summary_daily s
               JOIN target t ON t.trade_date = s.trade_date
               JOIN coverage c ON c.ticker = s.ticker
               LEFT JOIN project_fabric.universe_scores u
                 ON u.ticker = s.ticker AND u.trade_date = s.trade_date
               WHERE s.close_price > 0 AND s.volume_shares > 0
               ORDER BY s.turnover_idr DESC NULLS LAST""",
            (MIN_HISTORY_DAYS,),
        )
        candidates = _rows(cursor)
        tickers = [str(row["ticker"]).upper() for row in candidates]
        if not tickers:
            return {"as_of": None, "captured_at": None, "fundamentals": fundamentals,
                    "candidates": [], "statements": [], "history": []}
        cursor.execute(
            """SELECT ticker, period_kind, timeframe, revenue, net_profit_loss,
                      operating_cash_flow, captured_at
               FROM project_fabric.financial_statement_periods
               WHERE ticker = ANY(%s)
               ORDER BY ticker, timeframe""",
            (tickers,),
        )
        statements = _rows(cursor)
        cursor.execute(
            """WITH ranked AS (
                     SELECT h.*,
                            row_number() OVER (
                              PARTITION BY h.ticker ORDER BY h.trade_date DESC
                            ) AS row_number
                     FROM project_fabric.stock_history_daily h
                     WHERE h.ticker = ANY(%s)
                       AND h.trade_date <= %s
                   )
               SELECT ticker, trade_date, open_price, high_price, low_price,
                      close_price, volume_shares, foreign_buy_value_idr,
                      foreign_sell_value_idr
               FROM ranked
               WHERE row_number <= 100
               ORDER BY ticker, trade_date""",
            (tickers, candidates[0]["trade_date"]),
        )
        history = _rows(cursor)
    return {
        "as_of": candidates[0]["trade_date"],
        "captured_at": max((row.get("captured_at") for row in candidates if row.get("captured_at")), default=None),
        "fundamentals": fundamentals,
        "candidates": candidates,
        "statements": statements,
        "history": history,
    }


def rank_bot1(limit: int = 10, database_url: str | None = None) -> dict[str, Any]:
    requested = max(1, min(int(limit), 15))
    snapshot = _load_snapshot(database_url)
    fundamental_universe = snapshot["fundamentals"]
    fundamentals = {str(row["ticker"]).upper(): row for row in fundamental_universe}
    statements: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in snapshot["statements"]:
        statements[str(row["ticker"]).upper()].append(row)
    histories: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in snapshot["history"]:
        histories[str(row["ticker"]).upper()].append(row)

    ranked: list[dict[str, Any]] = []
    skipped = 0
    as_of = _iso_date(snapshot.get("as_of"))
    for market in snapshot["candidates"]:
        ticker = str(market["ticker"]).upper()
        target = fundamentals.get(ticker)
        if target is None:
            skipped += 1
            continue
        try:
            fundamental = score_fundamental(target, fundamental_universe, statements[ticker])
            if fundamental.get("score") is None:
                skipped += 1
                continue
            rows = list(histories[ticker])
            rows.append(market)
            frame = pd.DataFrame({
                "date": [pd.Timestamp(row["trade_date"]) for row in rows],
                "open": [_number(row.get("open_price")) for row in rows],
                "high": [_number(row.get("high_price")) for row in rows],
                "low": [_number(row.get("low_price")) for row in rows],
                "close": [_number(row.get("close_price")) for row in rows],
                "volume": [_number(row.get("volume_shares")) for row in rows],
                "foreign_buy": [_number(row.get("foreign_buy_value_idr")) for row in rows],
                "foreign_sell": [_number(row.get("foreign_sell_value_idr")) for row in rows],
            })
            frame = (frame.dropna(subset=["date", "open", "high", "low", "close", "volume"])
                     .sort_values("date").drop_duplicates("date", keep="last").tail(100)
                     .reset_index(drop=True))
            if len(frame) < MIN_HISTORY_DAYS or frame.iloc[-1]["date"].date().isoformat() != as_of:
                skipped += 1
                continue
            technical = technical_snapshot(frame, ticker)
            foreign = foreign_snapshot(frame)
            if foreign.get("score") is None:
                skipped += 1
                continue
            universe = universe_snapshot(market.get("scores_json"))
            score = combined_score(
                float(fundamental["score"]), float(technical["score"]),
                float(foreign["score"]), universe.get("score"),
            )
            gates = {
                "fundamental": float(fundamental["score"]) >= 60 and not fundamental.get("red_flag"),
                "technical": bool(technical["passed"]),
                "foreign": bool(foreign["passed"]),
                "distribution": not universe.get("risk_high"),
            }
            if all(gates.values()):
                status = "SIAP VALIDASI BROKER"
                status_order = 0
            elif score >= 60 and gates["fundamental"]:
                status = "PANTAU"
                status_order = 1
            else:
                status = "TUNGGU"
                status_order = 2
            ranked.append({
                "ticker": ticker,
                "score": score,
                "status": status,
                "status_order": status_order,
                "fundamental": fundamental,
                "technical": technical,
                "foreign": foreign,
                "universe": universe,
                "gates": gates,
            })
        except Exception:
            skipped += 1
            continue
    ranked.sort(key=lambda row: (row["status_order"], -row["score"], row["ticker"]))
    return {
        "as_of": as_of or None,
        "captured_at": snapshot.get("captured_at"),
        "eligible": len(ranked),
        "skipped": skipped,
        "rows": ranked[:requested],
    }


def _compact_rupiah(value: float | None) -> str:
    if value is None:
        return "N/A"
    sign = "+" if value > 0 else "-" if value < 0 else ""
    amount = abs(value)
    if amount >= 1_000_000_000_000:
        return f"{sign}Rp{amount / 1_000_000_000_000:.2f}T"
    if amount >= 1_000_000_000:
        return f"{sign}Rp{amount / 1_000_000_000:.2f}B"
    if amount >= 1_000_000:
        return f"{sign}Rp{amount / 1_000_000:.2f}M"
    return f"{sign}Rp{amount:,.0f}"


def _price(value: float) -> str:
    return f"{value:,.0f}"


def format_bot1_rank(limit: int = 10, result: dict[str, Any] | None = None) -> str:
    try:
        result = result or rank_bot1(limit)
    except Exception as exc:
        return "\n".join((
            "🧭 SETUP RANK — DATA TERBATAS",
            f"Detail: {type(exc).__name__}: {str(exc)[:160]}",
            "Tidak ada ZAPI yang dipanggil.",
        ))
    rows = result.get("rows") or []
    if not rows:
        return "\n".join((
            f"🧭 SETUP RANK — EOD {result.get('as_of') or 'N/A'}",
            "Tidak ada kandidat dengan data Fundamental + Teknikal + Foreign yang lengkap.",
            "0 ZAPI | Broker tidak diperbarui.",
        ))
    lines = [
        f"🧭 SETUP RANK — EOD {result.get('as_of')}",
        "Pre-screen Neon | 0 ZAPI | Broker belum dicek",
        "Bobot: Fundamental 35 | Teknikal 35 | Foreign 20 | Universe 10",
        f"Tersaring: {result.get('eligible', len(rows))} | Ditampilkan: {len(rows)}",
    ]
    for index, row in enumerate(rows, start=1):
        fundamental = row["fundamental"]
        technical = row["technical"]
        foreign = row["foreign"]
        universe = row["universe"]
        universe_text = "N/A" if universe.get("score") is None else f"{universe['score']:.0f}"
        lines.extend((
            "",
            f"{index}. {row['ticker']} — {row['score']:.1f} | {row['status']}",
            f"F {fundamental['score']:.0f} | T {technical['score']:.0f} | For {foreign['score']:.0f} | U {universe_text}",
            f"Foreign 5D {_compact_rupiah(foreign.get('net_5d'))} ({foreign.get('positive_days', 0)}/5) | {technical['trend']} | R:R {technical['selected_rr']:.2f}",
            f"{technical['selected_plan']} {_price(technical['entry_low'])}–{_price(technical['entry_high'])} | Inv <{_price(technical['invalidation'])} | TP1 {_price(technical['tp1'])}",
        ))
    lines.extend((
        "",
        "Final: pilih kandidat teratas, lalu /refresh TICKER dan BOT1 FULL TICKER.",
        "Ranking ini pre-screen EOD, bukan sinyal beli dan bukan konfirmasi broker.",
    ))
    return "\n".join(lines)
