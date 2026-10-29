"""Quality + trend EOD screener for Telegram Bot 1.

The command implements the ratio rules supplied by the user and adds momentum,
liquidity, foreign-flow and risk/reward confirmations.  It is deliberately
cache-only: all market and fundamental inputs come from Project Fabric/Neon.

``Bandar Accum/Dist`` is represented by Bot 1's price-volume accumulation
proxy and is labelled as such.  A real Piotroski value is preferred when the
warehouse exposes one; otherwise a transparent nine-check quality proxy is
used instead of inventing an audited Piotroski score.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
import math
import re
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from market_regime import detect_regime
from project_fabric.fundamental_intelligence import growth_snapshot
from project_fabric.postgres_backend import configured_database_url
from project_fabric.universe_intelligence import history_features

try:
    import psycopg
except ImportError:  # pragma: no cover - optional outside GitHub runtime.
    psycopg = None


WIB = ZoneInfo("Asia/Jakarta")
MIN_HISTORY = 205
HISTORY_LIMIT = 230
MIN_PRICE = 65.0
MIN_MARKET_CAP = 2_000_000_000_000.0
MIN_MARKET_CAP_PERCENTILE = 60.0
MIN_VALUE_MA5 = 100_000_000.0
MIN_F_SCORE = 5.0
MIN_ACCUMULATION = 10.0


def _number(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        result = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _row_number(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _number(row.get(key))
        if value is not None:
            return value
    return None


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value or "")[:10]


def _canonical(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _flatten(payload: Any, depth: int = 0) -> dict[str, Any]:
    if not isinstance(payload, dict) or depth > 3:
        return {}
    output: dict[str, Any] = {}
    for key, value in payload.items():
        output[_canonical(str(key))] = value
        if isinstance(value, dict):
            output.update(_flatten(value, depth + 1))
    return output


def _market_cap(row: dict[str, Any]) -> float | None:
    raw = row.get("raw_json") if isinstance(row.get("raw_json"), dict) else {}
    flat = _flatten(raw)
    for key in (
        "MarketCap", "marketCap", "market_cap", "MarketCapitalization",
        "marketCapitalization",
    ):
        value = _number(raw.get(key))
        if value is not None and value > 0:
            return value
    for key in ("marketcap", "marketcapitalization"):
        value = _number(flat.get(key))
        if value is not None and value > 0:
            return value
    return None


def market_cap_percentiles(rows: list[dict[str, Any]]) -> dict[str, float]:
    """Return 0-100 percentile; larger companies receive larger ranks."""
    values = [
        (str(row.get("ticker") or "").upper(), cap)
        for row in rows
        if (cap := _market_cap(row)) is not None
    ]
    result: dict[str, float] = {}
    for ticker, cap in values:
        less = sum(other < cap for _, other in values)
        equal = sum(other == cap for _, other in values)
        result[ticker] = 100.0 * (less + 0.5 * equal) / len(values)
    return result


def quality_score(
    fundamental: dict[str, Any],
    statements: list[dict[str, Any]],
) -> dict[str, Any]:
    """Prefer real Piotroski; otherwise calculate a disclosed 9-check proxy."""
    raw = fundamental.get("raw_fundamental")
    flat = _flatten(raw if isinstance(raw, dict) else fundamental)
    for key in (
        "piotroskifscore", "piotroskiscore", "piotroski", "fscore",
    ):
        actual = _number(flat.get(key))
        if actual is not None and 0 <= actual <= 9:
            return {
                "score": actual,
                "source": "PIOTROSKI",
                "coverage": 9,
                "checks": {},
            }

    growth = growth_snapshot(statements)
    current = growth.get("current") or {}
    values = growth.get("values") or {}
    profit = _number(current.get("net_profit_loss"))
    ocf = _number(current.get("operating_cash_flow"))
    checks: dict[str, bool | None] = {
        "profit_positive": None if profit is None else profit > 0,
        "ocf_positive": None if ocf is None else ocf > 0,
        "roa_positive": None if _number(fundamental.get("roa_pct")) is None else _number(fundamental.get("roa_pct")) > 0,
        "ocf_above_profit": None if profit is None or ocf is None else ocf > profit,
        "revenue_yoy_positive": None if _number(values.get("revenue")) is None else _number(values.get("revenue")) > 0,
        "profit_yoy_positive": None if _number(values.get("net_profit_loss")) is None else _number(values.get("net_profit_loss")) > 0,
        "ocf_yoy_positive": None if _number(values.get("operating_cash_flow")) is None else _number(values.get("operating_cash_flow")) > 0,
        "roe_positive": None if _number(fundamental.get("roe_pct")) is None else _number(fundamental.get("roe_pct")) > 0,
        "margin_positive": None if _number(fundamental.get("net_margin_pct")) is None else _number(fundamental.get("net_margin_pct")) > 0,
    }
    coverage = sum(value is not None for value in checks.values())
    return {
        "score": float(sum(value is True for value in checks.values())) if coverage == 9 else None,
        "source": "F-PROXY 9 CHECK",
        "coverage": coverage,
        "checks": checks,
    }


def _history_frame(
    history: list[dict[str, Any]],
    market: dict[str, Any],
) -> pd.DataFrame:
    rows = list(history) + [market]
    prepared = []
    for row in rows:
        raw = row.get("raw_json") if isinstance(row.get("raw_json"), dict) else {}
        stamp = _iso(row.get("trade_date") or row.get("date"))
        close = _row_number(row, "close_price", "close", "Close") or _row_number(raw, "Close", "close")
        high = _row_number(row, "high_price", "high", "High") or _row_number(raw, "High", "high")
        low = _row_number(row, "low_price", "low", "Low") or _row_number(raw, "Low", "low")
        opening = _row_number(row, "open_price", "open", "OpenPrice", "Open") or _row_number(raw, "OpenPrice", "Open", "open")
        volume = _row_number(row, "volume_shares", "volume", "Volume") or _row_number(raw, "Volume", "volume")
        value = _row_number(row, "turnover_idr", "value", "Value") or _row_number(raw, "Value", "value")
        buy = _row_number(row, "foreign_buy_value_idr", "foreignBuyValue", "ForeignBuyValue")
        sell = _row_number(row, "foreign_sell_value_idr", "foreignSellValue", "ForeignSellValue")
        if buy is None or sell is None:
            buy = _row_number(raw, "foreignBuyValue", "ForeignBuyValue")
            sell = _row_number(raw, "foreignSellValue", "ForeignSellValue")
        if (buy is None or sell is None) and close is not None:
            buy_shares = _row_number(raw, "ForeignBuy", "foreignBuy", "ForeignBuyShares")
            sell_shares = _row_number(raw, "ForeignSell", "foreignSell", "ForeignSellShares")
            if buy_shares is not None and sell_shares is not None:
                buy, sell = buy_shares * close, sell_shares * close
        if not stamp or None in (close, high, low, opening, volume):
            continue
        if close <= 0 or high < low or not low <= close <= high or volume < 0:
            continue
        prepared.append({
            "date": pd.Timestamp(stamp), "open": opening, "high": high,
            "low": low, "close": close, "volume": volume,
            "value": value, "foreign_buy": buy, "foreign_sell": sell,
        })
    if not prepared:
        return pd.DataFrame()
    return (
        pd.DataFrame(prepared)
        .sort_values("date")
        .drop_duplicates("date", keep="last")
        .tail(HISTORY_LIMIT)
        .reset_index(drop=True)
    )


def _add_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    """Narrow indicator set, kept independent from the live ZAPI stack."""
    result = frame.copy()
    previous_close = result["close"].shift(1)
    true_range = pd.concat((
        result["high"] - result["low"],
        (result["high"] - previous_close).abs(),
        (result["low"] - previous_close).abs(),
    ), axis=1).max(axis=1)
    result["atr14"] = true_range.rolling(14, min_periods=14).mean()
    result["avg_volume20"] = result["volume"].rolling(20).mean()
    result["volume_ratio"] = (
        result["volume"] / result["avg_volume20"].replace(0, float("nan"))
    ).fillna(0.0)
    delta = result["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    relative = avg_gain / avg_loss.replace(0, float("nan"))
    result["rsi14"] = 100 - (100 / (1 + relative))
    result.loc[(avg_loss == 0) & (avg_gain > 0), "rsi14"] = 100
    result["rsi14"] = result["rsi14"].fillna(50)
    ema12 = result["close"].ewm(span=12, adjust=False).mean()
    ema26 = result["close"].ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    result["macd_hist"] = macd - macd.ewm(span=9, adjust=False).mean()
    return result


def _accumulation_proxy(frame: pd.DataFrame, ticker: str, as_of: str) -> float | None:
    cutoff = datetime.combine(
        date.fromisoformat(as_of) + timedelta(days=1), time.min, tzinfo=WIB
    )
    payload = {"items": [
        {
            "date": row.date.date().isoformat(), "open": float(row.open),
            "high": float(row.high), "low": float(row.low),
            "close": float(row.close), "volume": float(row.volume),
        }
        for row in frame.itertuples()
    ]}
    feature = history_features(ticker, payload, asof=cutoff)
    return _number((feature.get("accumulation_persistence") or {}).get("score"))


def analyze_quality_trend(
    market: dict[str, Any],
    history: list[dict[str, Any]],
    fundamental: dict[str, Any],
    statements: list[dict[str, Any]],
    market_cap_percentile: float | None,
) -> dict[str, Any] | None:
    ticker = str(market.get("ticker") or "").upper()
    as_of = _iso(market.get("trade_date"))
    frame = _history_frame(history, market)
    if len(frame) < MIN_HISTORY or not as_of or frame.iloc[-1]["date"].date().isoformat() != as_of:
        return None
    frame = _add_indicators(frame)
    frame["ma20"] = frame["close"].rolling(20).mean()
    frame["ma50"] = frame["close"].rolling(50).mean()
    frame["ma200"] = frame["close"].rolling(200).mean()
    latest, previous = frame.iloc[-1], frame.iloc[-2]
    close = float(latest["close"])
    ma20, ma50, ma200 = (float(latest[key]) for key in ("ma20", "ma50", "ma200"))
    value_ma5 = _number(frame.tail(5)["value"].mean()) if frame.tail(5)["value"].notna().all() else None
    return_1d = (close / float(previous["close"]) - 1.0) * 100.0
    market_cap = _market_cap(market)
    pe = _number(fundamental.get("pe"))
    pb = _number(fundamental.get("pb"))
    quality = quality_score(fundamental, statements)
    accumulation = _accumulation_proxy(frame, ticker, as_of)

    core = {
        "price_gt_65": close > MIN_PRICE,
        "ma20_gt_ma200": ma20 > ma200,
        "ma20_gt_ma50": ma20 > ma50,
        "ma50_gt_ma200": ma50 > ma200,
        "price_gt_ma20": close > ma20,
        "accumulation_gt_10": accumulation is not None and accumulation > MIN_ACCUMULATION,
        "f_score_gt_5": quality["score"] is not None and quality["score"] > MIN_F_SCORE,
        "market_cap_gt_2t": market_cap is not None and market_cap > MIN_MARKET_CAP,
        "pbv_positive": pb is not None and pb > 0,
        "pe_positive": pe is not None and pe > 0,
        "market_cap_rank_gt_60": market_cap_percentile is not None and market_cap_percentile > MIN_MARKET_CAP_PERCENTILE,
        "value_ma5_gt_100m": value_ma5 is not None and value_ma5 > MIN_VALUE_MA5,
        "return_1d_positive": return_1d > 0,
    }
    if not all(core.values()):
        return None

    rsi = float(latest["rsi14"])
    previous_rsi = float(previous["rsi14"])
    macd_hist = float(latest["macd_hist"])
    previous_macd = float(previous["macd_hist"])
    volume_ratio = float(latest["volume_ratio"])
    foreign_rows = frame.tail(5).dropna(subset=["foreign_buy", "foreign_sell"])
    foreign_5d = (
        float((foreign_rows["foreign_buy"] - foreign_rows["foreign_sell"]).sum())
        if len(foreign_rows) == 5 else None
    )
    atr = float(latest["atr14"])
    invalidation = float(frame.tail(10)["low"].min())
    risk = close - invalidation
    prior_high = float(frame.iloc[:-1].tail(50)["high"].max())
    target = max(prior_high, close + 2.0 * atr)
    rr = (target - close) / risk if risk > 0 else 0.0
    confirmations = {
        "rsi": 50 <= rsi <= 70 and rsi >= previous_rsi,
        "macd": macd_hist > 0 and macd_hist >= previous_macd,
        "volume": volume_ratio >= 1.0,
        "foreign": foreign_5d is not None and foreign_5d > 0,
        "rr": rr >= 1.5,
    }
    confirmation_count = sum(confirmations.values())
    score = 65.0 + confirmation_count * 7.0
    return {
        "ticker": ticker,
        "as_of": as_of,
        "score": score,
        "confirmation_count": confirmation_count,
        "core": core,
        "confirmations": confirmations,
        "close": close,
        "ma20": ma20,
        "ma50": ma50,
        "ma200": ma200,
        "return_1d": return_1d,
        "value_ma5": value_ma5,
        "market_cap": market_cap,
        "market_cap_percentile": market_cap_percentile,
        "pe": pe,
        "pb": pb,
        "quality": quality,
        "accumulation": accumulation,
        "rsi": rsi,
        "macd_hist": macd_hist,
        "volume_ratio": volume_ratio,
        "foreign_5d": foreign_5d,
        "entry": close,
        "invalidation": invalidation,
        "target": target,
        "rr": rr,
    }


def _rows(cursor) -> list[dict[str, Any]]:
    columns = [item.name for item in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _load_universe(database_url: str | None = None) -> dict[str, Any]:
    url = (database_url if database_url is not None else configured_database_url()).strip()
    if not url or psycopg is None:
        raise RuntimeError("Neon QUALITY TREND belum terkonfigurasi")
    with psycopg.connect(url, connect_timeout=12, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT ticker, company_name, sector, pe, pb, ps, roe_pct, roa_pct,
                      net_margin_pct, debt_equity_pct, current_ratio,
                      endpoint_status, updated_at, to_jsonb(f) AS raw_fundamental
               FROM project_fabric.fundamental_latest f
               ORDER BY ticker"""
        )
        fundamentals = _rows(cursor)
        cursor.execute("SELECT max(trade_date) FROM project_fabric.stock_summary_daily")
        market_day = cursor.fetchone()[0]
        if market_day is None:
            raise RuntimeError("Snapshot EOD belum tersedia")
        cursor.execute(
            """SELECT ticker, trade_date, open_price, high_price, low_price,
                      close_price, volume_shares, turnover_idr,
                      foreign_buy_value_idr, foreign_sell_value_idr,
                      raw_json, captured_at
               FROM project_fabric.stock_summary_daily
               WHERE trade_date=%s ORDER BY ticker""",
            (market_day,),
        )
        summaries = _rows(cursor)
        tickers = [str(row["ticker"]).upper() for row in summaries]
        cursor.execute(
            """SELECT ticker, period_kind, timeframe, revenue, net_profit_loss,
                      operating_cash_flow, captured_at
               FROM project_fabric.financial_statement_periods
               WHERE ticker=ANY(%s) ORDER BY ticker, timeframe""",
            (tickers,),
        )
        statements = _rows(cursor)
    return {
        "url": url, "as_of": str(market_day), "fundamentals": fundamentals,
        "summaries": summaries, "statements": statements,
    }


def _load_histories(
    tickers: list[str], as_of: str, database_url: str
) -> dict[str, list[dict[str, Any]]]:
    if not tickers:
        return {}
    with psycopg.connect(database_url, connect_timeout=12, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute(
            """WITH ranked AS (
                   SELECT ticker, trade_date, open_price, high_price, low_price,
                          close_price, volume_shares, turnover_idr,
                          foreign_buy_value_idr, foreign_sell_value_idr, raw_json,
                          row_number() OVER (
                            PARTITION BY ticker ORDER BY trade_date DESC
                          ) AS row_number
                   FROM project_fabric.stock_history_daily
                   WHERE ticker=ANY(%s) AND trade_date<=%s
                 )
                 SELECT ticker, trade_date, open_price, high_price, low_price,
                        close_price, volume_shares, turnover_idr,
                        foreign_buy_value_idr, foreign_sell_value_idr, raw_json
                 FROM ranked WHERE row_number<=%s
                 ORDER BY ticker, trade_date""",
            (tickers, as_of, HISTORY_LIMIT),
        )
        rows = _rows(cursor)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["ticker"]).upper()].append(row)
    return dict(grouped)


def screen_quality_trend(
    limit: int = 10,
    ticker: str | None = None,
    database_url: str | None = None,
) -> dict[str, Any]:
    requested = max(1, min(int(limit), 15))
    snapshot = _load_universe(database_url)
    summaries = snapshot["summaries"]
    cap_ranks = market_cap_percentiles(summaries)
    fundamentals = {str(row["ticker"]).upper(): row for row in snapshot["fundamentals"]}
    statement_map: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in snapshot["statements"]:
        statement_map[str(row["ticker"]).upper()].append(row)

    wanted = ticker.strip().upper().replace(".JK", "") if ticker else None
    preliminary: list[dict[str, Any]] = []
    quality_map: dict[str, dict[str, Any]] = {}
    for row in summaries:
        code = str(row.get("ticker") or "").upper()
        fundamental = fundamentals.get(code)
        if wanted and code != wanted:
            continue
        if fundamental is None:
            continue
        quality = quality_score(fundamental, statement_map[code])
        quality_map[code] = quality
        if (
            (_row_number(row, "close_price") or 0) > MIN_PRICE
            and (_market_cap(row) or 0) > MIN_MARKET_CAP
            and cap_ranks.get(code, 0) > MIN_MARKET_CAP_PERCENTILE
            and (_number(fundamental.get("pe")) or 0) > 0
            and (_number(fundamental.get("pb")) or 0) > 0
            and quality.get("score") is not None
            and float(quality["score"]) > MIN_F_SCORE
        ):
            preliminary.append(row)

    histories = _load_histories(
        [str(row["ticker"]).upper() for row in preliminary],
        snapshot["as_of"], snapshot["url"],
    )
    results = []
    for market in preliminary:
        code = str(market["ticker"]).upper()
        analyzed = analyze_quality_trend(
            market, histories.get(code, []), fundamentals[code],
            statement_map[code], cap_ranks.get(code),
        )
        if analyzed is not None:
            results.append(analyzed)
    results.sort(
        key=lambda row: (-row["confirmation_count"], -row["score"], -row["accumulation"], row["ticker"])
    )
    regime_rows = [row.get("raw_json") for row in summaries if isinstance(row.get("raw_json"), dict)]
    regime = detect_regime(regime_rows, "LATEST DAILY DATABASE SNAPSHOT")
    return {
        "as_of": snapshot["as_of"], "ticker": wanted,
        "preliminary": len(preliminary), "eligible": len(results),
        "rows": results[:requested], "regime": regime,
    }


def _rupiah(value: float | None, *, signed: bool = False) -> str:
    if value is None:
        return "N/A"
    prefix = "+" if signed and value >= 0 else ""
    absolute = abs(value)
    sign = "-" if value < 0 else prefix
    if absolute >= 1_000_000_000_000:
        return f"{sign}Rp{absolute / 1_000_000_000_000:.2f}T"
    if absolute >= 1_000_000_000:
        return f"{sign}Rp{absolute / 1_000_000_000:.2f}B"
    if absolute >= 1_000_000:
        return f"{sign}Rp{absolute / 1_000_000:.1f}M"
    return f"{sign}Rp{absolute:,.0f}"


def format_quality_trend(
    limit: int = 10,
    ticker: str | None = None,
    result: dict[str, Any] | None = None,
) -> str:
    try:
        result = result or screen_quality_trend(limit=limit, ticker=ticker)
    except Exception as exc:
        return "\n".join((
            "📐 RATIO TREND — DATA TERBATAS",
            f"Detail: {type(exc).__name__}: {str(exc)[:160]}",
            "Fail-closed | 0 ZAPI | Tidak ada rule yang dilonggarkan.",
        ))
    regime = result["regime"]
    rows = result.get("rows") or []
    header = [
        f"📐 RATIO TREND — EOD {result.get('as_of') or 'N/A'}",
        f"Market Gate: {regime.label} → {regime.mode}",
        "Hard gate: semua rule gambar wajib lolos | 0 ZAPI",
        f"Lolos: {result.get('eligible', 0)} | Ditampilkan: {len(rows)}",
    ]
    if not rows:
        suffix = f" untuk {result['ticker']}" if result.get("ticker") else ""
        return "\n".join(header + [
            f"Tidak ada saham{suffix} yang lolos seluruh rule.",
            "Data yang kosong dianggap GAGAL, bukan nol atau asumsi.",
            "F-Proxy hanya dipakai bila Piotroski asli belum tersedia.",
        ])

    lines = list(header)
    for index, row in enumerate(rows, start=1):
        quality = row["quality"]
        f_label = "F-Score" if quality["source"] == "PIOTROSKI" else "F-Proxy"
        status = (
            "WATCH ONLY — MARKET CASH"
            if regime.mode == "CASH"
            else "READY VALIDASI BROKER"
            if row["confirmation_count"] >= 4
            else "PANTAU KONFIRMASI"
        )
        macd = "✅" if row["confirmations"]["macd"] else "⚠️"
        lines.extend((
            "",
            f"{index}. {row['ticker']} — {row['score']:.0f}/100 | {status}",
            f"Price {row['close']:,.0f} > MA20 {row['ma20']:,.0f} > MA50 {row['ma50']:,.0f} > MA200 {row['ma200']:,.0f}",
            f"{f_label} {quality['score']:.0f}/9 | PE {row['pe']:.2f} | PBV {row['pb']:.2f}",
            f"MCap {_rupiah(row['market_cap'])} | Rank percentile {row['market_cap_percentile']:.0f}",
            f"Accum Proxy {row['accumulation']:.0f} | RSI {row['rsi']:.1f} | MACD {macd} | Vol {row['volume_ratio']:.2f}x",
            f"Return 1D {row['return_1d']:+.2f}% | Value MA5 {_rupiah(row['value_ma5'])} | Foreign 5D {_rupiah(row['foreign_5d'], signed=True)}",
            f"Plan {row['entry']:,.0f} | Stop <{row['invalidation']:,.0f} | TP {row['target']:,.0f} | R:R {row['rr']:.2f}",
            f"Konfirmasi tambahan: {row['confirmation_count']}/5",
        ))
    lines.extend((
        "",
        "Rank Market Cap = percentile (lebih besar lebih baik); >60 sesuai rule gambar.",
        "Accum Proxy = price-volume Bot 1, bukan indikator proprietary Stockbit.",
        "F-Proxy = 9 cek data tersedia, bukan Piotroski audited; Piotroski asli diprioritaskan bila tersedia.",
        "Final: /refresh TICKER lalu BOT1 FULL TICKER sebelum entry.",
    ))
    return "\n".join(lines)
