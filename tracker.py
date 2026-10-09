
from __future__ import annotations

import os
import json
import math
import sqlite3
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, Optional

import pandas as pd
import requests
from dotenv import load_dotenv
from bot_io import get_json, send_message
from project_fabric.data_gateway import DataGateway, market_policy

load_dotenv()

ZPI_BASE_URL = "https://api.zpi.web.id/v1/finance:pluang"
ZPI_IDX_BASE_URL = "https://api.zapi.ink/v1/finance:idx"
API_KEY = os.getenv("ZPI_API_KEY", "")
DB_PATH = os.getenv("DB_PATH", "broker_tracker.db")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

_GATEWAY = DataGateway(service="broker-accumulation-tracker")


def get_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA foreign_keys=ON;")
    return con


def init_db(db_path: str = DB_PATH) -> None:
    con = get_connection(db_path)
    cur = con.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS broker_daily (
        trade_date TEXT NOT NULL,
        ticker TEXT NOT NULL,
        broker TEXT NOT NULL,
        side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
        lots INTEGER NOT NULL,
        value REAL NOT NULL,
        avg_price REAL,
        source TEXT,
        fetched_at TEXT NOT NULL,
        PRIMARY KEY (trade_date, ticker, broker, side)
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS stock_daily (
        trade_date TEXT NOT NULL,
        ticker TEXT NOT NULL,
        close REAL,
        volume_shares REAL,
        value REAL,
        frequency INTEGER,
        fetched_at TEXT NOT NULL,
        PRIMARY KEY (trade_date, ticker)
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS stock_summary_snapshots (
        trade_date TEXT PRIMARY KEY,
        row_count INTEGER NOT NULL CHECK(row_count > 0),
        source TEXT NOT NULL,
        fetched_at TEXT NOT NULL
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS stock_summary_daily (
        trade_date TEXT NOT NULL,
        ticker TEXT NOT NULL,
        open REAL,
        high REAL,
        low REAL,
        close REAL,
        volume_shares REAL,
        value REAL,
        frequency INTEGER,
        foreign_buy_value REAL,
        foreign_sell_value REAL,
        net_foreign_value REAL,
        payload_json TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        PRIMARY KEY (trade_date, ticker),
        FOREIGN KEY (trade_date) REFERENCES stock_summary_snapshots(trade_date)
            ON DELETE CASCADE
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS scan_results (
        scan_time TEXT NOT NULL,
        ticker TEXT NOT NULL,
        lookback_days INTEGER NOT NULL,
        net_lots REAL,
        gross_buy_lots REAL,
        gross_sell_lots REAL,
        buy_value REAL,
        sell_value REAL,
        dominant_buyers INTEGER,
        dominant_sellers INTEGER,
        avg_buy_price REAL,
        avg_sell_price REAL,
        last_price REAL,
        price_vs_avg_buy_pct REAL,
        accumulation_ratio REAL,
        score REAL,
        label TEXT,
        PRIMARY KEY (scan_time, ticker, lookback_days)
    );
    """)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_broker_daily_ticker_date ON broker_daily(ticker, trade_date);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_broker_daily_broker_date ON broker_daily(broker, trade_date);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_stock_summary_ticker_date ON stock_summary_daily(ticker, trade_date);")
    con.commit()
    con.close()


def _summary_number(row: dict, *keys: str) -> float | None:
    for key in keys:
        value = row.get(key)
        if value in (None, ""):
            continue
        try:
            number = float(str(value).replace(",", ""))
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            return number
    return None


def _summary_ticker(row: dict) -> str:
    return str(
        row.get("ticker")
        or row.get("StockCode")
        or row.get("stockCode")
        or row.get("code")
        or ""
    ).strip().upper()


def save_stock_summary_snapshot(
    trade_date: str,
    rows: list[dict],
    db_path: str = DB_PATH,
    source: str = "zapi:finance/idx/stock-summary",
) -> int:
    """Atomically replace one complete stock-summary snapshot."""
    try:
        parsed_date = date.fromisoformat(str(trade_date))
    except (TypeError, ValueError):
        raise ValueError("Tanggal snapshot stock-summary tidak valid.") from None
    trade_date = parsed_date.isoformat()
    if not isinstance(rows, list) or not rows:
        raise ValueError("Snapshot stock-summary kosong; data lama dipertahankan.")

    fetched_at = datetime.now().astimezone().isoformat(timespec="seconds")
    prepared: list[tuple] = []
    stock_rows: list[tuple] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Baris stock-summary tidak valid.")
        ticker = _summary_ticker(row)
        row_date = row.get("Date") or row.get("date") or row.get("TradingDate")
        if not ticker or ticker in seen:
            raise ValueError("Ticker stock-summary kosong/duplikat.")
        if not row_date or str(row_date)[:10] != trade_date:
            raise ValueError("Tanggal baris stock-summary tidak sesuai snapshot.")
        seen.add(ticker)

        payload = dict(row)
        payload["ticker"] = ticker
        payload.setdefault("Date", trade_date)
        try:
            payload_json = json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            raise ValueError(f"Payload stock-summary {ticker} tidak dapat disimpan.") from None

        opening = _summary_number(payload, "OpenPrice", "Open", "open")
        high = _summary_number(payload, "HighPrice", "High", "high")
        low = _summary_number(payload, "LowPrice", "Low", "low")
        close = _summary_number(payload, "Close", "close")
        volume = _summary_number(payload, "Volume", "volume", "VolumeShares", "volumeShares")
        value = _summary_number(payload, "Value", "value", "TransactionValue", "transactionValue")
        frequency_number = _summary_number(payload, "Frequency", "frequency")
        frequency = int(frequency_number) if frequency_number is not None and frequency_number.is_integer() else None
        foreign_buy = _summary_number(payload, "foreignBuyValue", "ForeignBuyValue")
        foreign_sell = _summary_number(payload, "foreignSellValue", "ForeignSellValue")
        net_foreign = _summary_number(payload, "netForeignValue", "NetForeignValue")

        prepared.append((
            trade_date, ticker, opening, high, low, close, volume, value,
            frequency, foreign_buy, foreign_sell, net_foreign,
            payload_json, fetched_at,
        ))
        stock_rows.append((trade_date, ticker, close, volume, value, frequency, fetched_at))

    init_db(db_path)
    con = get_connection(db_path)
    try:
        with con:
            con.execute(
                """
                INSERT INTO stock_summary_snapshots (trade_date, row_count, source, fetched_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(trade_date) DO UPDATE SET
                    row_count=excluded.row_count,
                    source=excluded.source,
                    fetched_at=excluded.fetched_at
                """,
                (trade_date, len(prepared), source, fetched_at),
            )
            con.execute("DELETE FROM stock_summary_daily WHERE trade_date = ?", (trade_date,))
            con.executemany(
                """
                INSERT INTO stock_summary_daily (
                    trade_date, ticker, open, high, low, close,
                    volume_shares, value, frequency,
                    foreign_buy_value, foreign_sell_value, net_foreign_value,
                    payload_json, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                prepared,
            )
            con.executemany(
                """
                INSERT INTO stock_daily (
                    trade_date, ticker, close, volume_shares, value, frequency, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_date, ticker) DO UPDATE SET
                    close=COALESCE(excluded.close, stock_daily.close),
                    volume_shares=COALESCE(excluded.volume_shares, stock_daily.volume_shares),
                    value=COALESCE(excluded.value, stock_daily.value),
                    frequency=COALESCE(excluded.frequency, stock_daily.frequency),
                    fetched_at=excluded.fetched_at
                """,
                stock_rows,
            )
    finally:
        con.close()
    return len(prepared)


def load_stock_summary_snapshot(
    trade_date: str,
    db_path: str = DB_PATH,
) -> list[dict] | None:
    """Return a snapshot only when metadata and stored row count are complete."""
    init_db(db_path)
    con = get_connection(db_path)
    try:
        meta = con.execute(
            "SELECT row_count FROM stock_summary_snapshots WHERE trade_date = ?",
            (trade_date,),
        ).fetchone()
        if not meta:
            return None
        records = con.execute(
            """
            SELECT ticker, payload_json
            FROM stock_summary_daily
            WHERE trade_date = ?
            ORDER BY ticker
            """,
            (trade_date,),
        ).fetchall()
        if len(records) != int(meta[0]):
            return None
        rows: list[dict] = []
        for ticker, payload_json in records:
            try:
                row = json.loads(payload_json)
            except (TypeError, ValueError):
                return None
            if not isinstance(row, dict) or _summary_ticker(row) != ticker:
                return None
            row_date = row.get("Date") or row.get("date") or row.get("TradingDate")
            if not row_date or str(row_date)[:10] != trade_date:
                return None
            row["ticker"] = ticker
            rows.append(row)
        return rows
    finally:
        con.close()


def _headers() -> dict:
    if not API_KEY:
        raise RuntimeError(
            "ZPI_API_KEY belum diisi. Salin .env.example menjadi .env lalu masukkan API key Zapi."
        )
    return {"x-api-key": API_KEY, "accept": "application/json"}


def _unwrap_project_payload(payload):
    """Normalize shared ZAPI wrapper regardless of which bot populated Neon."""
    current = payload
    for _ in range(4):
        if (
            isinstance(current, dict)
            and "project" in current
            and isinstance(current.get("data"), (dict, list))
        ):
            current = current["data"]
            continue
        break
    return current


def _get(path: str, params: dict, timeout: int = 30) -> dict:
    path = path.strip().lstrip("/")
    payload = _GATEWAY.get_or_load(
        endpoint=f"pluang:{path}",
        params=params,
        loader=lambda: get_json(
            f"{ZPI_BASE_URL}/{path}",
            headers=_headers(),
            params=params,
            timeout=timeout,
        ),
        policy=market_policy("pluang", path, params),
    )
    return _unwrap_project_payload(payload)


def _get_idx(path: str, params: dict, timeout: int = 30) -> dict:
    path = path.strip().lstrip("/")
    payload = _GATEWAY.get_or_load(
        endpoint=f"idx:{path}",
        params=params,
        loader=lambda: get_json(
            f"{ZPI_IDX_BASE_URL}/{path}",
            headers=_headers(),
            params=params,
            timeout=timeout,
        ),
        policy=market_policy("idx", path, params),
    )
    return _unwrap_project_payload(payload)


def fetch_stock_summary(
    date: str | None = None,
    code: str | None = None,
    length: int = 5000,
    start: int = 0,
) -> dict:
    params: dict = {
        "length": int(length),
        "start": int(start),
    }

    if date:
        params["date"] = date

    if code:
        params["code"] = code.upper().strip()

    return _get_idx(
        "stock-summary",
        params,
    )

CANONICAL_STOCK_HISTORY_LENGTH = 120


def _trim_stock_history_payload(payload: dict, length: int) -> dict:
    """Return only the newest requested bars without changing the shared cache."""
    requested = max(1, int(length))
    output = deepcopy(payload)

    if not isinstance(output, dict):
        return output

    container = output.get("data")
    if not isinstance(container, dict):
        container = output

    items = container.get("items")
    if not isinstance(items, list):
        return output

    trimmed = items[:requested]
    container["items"] = trimmed
    container["count"] = len(trimmed)

    if trimmed:
        newest_date = trimmed[0].get("date") if isinstance(trimmed[0], dict) else None
        oldest_date = trimmed[-1].get("date") if isinstance(trimmed[-1], dict) else None
        if newest_date:
            container["to"] = newest_date
        if oldest_date:
            container["from"] = oldest_date

    return output


def fetch_stock_history(
    ticker: str,
    length: int = CANONICAL_STOCK_HISTORY_LENGTH,
) -> dict:
    ticker = ticker.upper().strip()
    requested = max(1, int(length))
    upstream_length = max(CANONICAL_STOCK_HISTORY_LENGTH, requested)

    payload = _get_idx(
        "stock-history",
        {
            "code": ticker,
            "length": upstream_length,
        },
    )

    if requested >= upstream_length:
        return payload
    return _trim_stock_history_payload(payload, requested)


CANONICAL_FOREIGN_FLOW_LENGTH = 50


def fetch_foreign_flow(
    date: str | None = None,
    code: str | None = None,
    sort: str = "net",
    length: int = CANONICAL_FOREIGN_FLOW_LENGTH,
    start: int = 0,
) -> dict:
    requested_length = max(1, int(length))
    requested_start = max(0, int(start))

    # Common leaderboard requests share one canonical first-page cache key.
    # Callers already apply their final display limit locally.
    upstream_length = (
        max(CANONICAL_FOREIGN_FLOW_LENGTH, requested_length)
        if requested_start == 0
        else requested_length
    )

    params: dict = {
        "sort": sort,
        "length": upstream_length,
        "start": requested_start,
    }

    if date:
        params["date"] = date

    if code:
        params["code"] = code.upper().strip()

    return _get_idx(
        "foreign-flow",
        params,
    )

def fetch_broker_summary(
    ticker: str,
    start_date: str,
    end_date: Optional[str] = None,
    net: bool = True,
) -> dict:
    ticker = ticker.upper().strip()
    end_date = end_date or start_date
    return _get(
        "broker-summary",
        {
            "code": ticker,
            "startDate": start_date,
            "endDate": end_date,
            "net": str(net).lower(),
        },
    )


def fetch_quote(ticker: str) -> dict:
    return _get("quote", {"code": ticker.upper().strip()})


def fetch_multi_quote(tickers: Iterable[str]) -> dict:
    codes = ",".join(t.upper().strip() for t in tickers)
    return _get("summary", {"codes": codes})


def _parse_number(x) -> Optional[float]:
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


class BrokerSnapshotValidationError(ValueError):
    """Upstream broker snapshot is malformed and must not overwrite stored data."""


def save_broker_snapshot(payload: dict, db_path: str = DB_PATH) -> int:
    """Validate the complete daily snapshot before replacing it atomically.

    Empty responses preserve existing data: they can indicate upstream delay.
    Multi-day aggregates must never be inserted as a single daily observation.
    """
    if not isinstance(payload, dict):
        raise BrokerSnapshotValidationError("Broker snapshot harus berupa object.")
    if payload.get("success") is False or payload.get("ok") is False:
        raise BrokerSnapshotValidationError("Broker snapshot gagal dari upstream.")
    if not any(payload.get(k) for k in ("buyers", "sellers")):
        return 0
    ticker = str(payload.get("code") or "").strip().upper()
    trade_date = payload.get("endDate") or payload.get("startDate")
    if not ticker or not isinstance(trade_date, str):
        raise BrokerSnapshotValidationError("Ticker/tanggal broker snapshot tidak lengkap.")
    trade_date = date.fromisoformat(trade_date).isoformat()
    if payload.get("startDate") and payload["startDate"] != trade_date:
        raise BrokerSnapshotValidationError("Hanya snapshot satu hari yang boleh disimpan.")
    source = payload.get("source", "pluang")
    fetched_at = datetime.now().isoformat(timespec="seconds")
    rows, seen = [], set()
    for side_key, side in (("buyers", "BUY"), ("sellers", "SELL")):
        items = payload.get(side_key)
        if not isinstance(items, list):
            raise BrokerSnapshotValidationError(f"Field {side_key} harus berupa list lengkap.")
        for item in items:
            if not isinstance(item, dict):
                raise BrokerSnapshotValidationError("Baris broker tidak valid.")
            broker = str(item.get("broker") or "").strip().upper()
            lots = _parse_number(item.get("lots"))
            value = _parse_number(item.get("value"))
            average = _parse_number(item.get("averagePrice"))
            if not broker or (broker, side) in seen:
                raise BrokerSnapshotValidationError("Kode broker kosong atau duplikat.")
            if any(x is None or not math.isfinite(x) or x < 0 for x in (lots, value)):
                raise BrokerSnapshotValidationError(
                    "Lot/value broker tidak valid "
                    f"(date={trade_date}, ticker={ticker}, broker={broker}, side={side}, "
                    f"lots={item.get('lots')!r}, value={item.get('value')!r})."
                )
            if lots != int(lots):
                raise BrokerSnapshotValidationError("Lot broker harus bilangan bulat.")
            if average is not None and (not math.isfinite(average) or average < 0):
                raise BrokerSnapshotValidationError("Average price broker tidak valid.")
            seen.add((broker, side))
            rows.append((trade_date, ticker, broker, side, int(lots), value, average, source, fetched_at))
    con = get_connection(db_path)
    try:
        with con:
            con.execute("DELETE FROM broker_daily WHERE trade_date=? AND ticker=?", (trade_date, ticker))
            con.executemany("""
                INSERT INTO broker_daily
                (trade_date, ticker, broker, side, lots, value, avg_price, source, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, rows)
    finally:
        con.close()
    return len(rows)


def collect_daily(
    ticker: str,
    start_date: str,
    end_date: str,
    db_path: str = DB_PATH,
    sleep_seconds: float = 0.25,
) -> dict:
    """
    Mengambil data per tanggal kalender.
    Hari libur/non-bursa biasanya akan kosong/gagal dan dilewati.
    """
    d0 = date.fromisoformat(start_date)
    d1 = date.fromisoformat(end_date)

    stats = {
        "ticker": ticker.upper(),
        "saved_rows": 0,
        "dates_ok": 0,
        "dates_skipped": 0,
        "invalid_snapshots": 0,
        "invalid_dates": [],
    }
    d = d0

    while d <= d1:
        # Lewati Sabtu-Minggu sebelum request.
        if d.weekday() < 5:
            ds = d.isoformat()
            try:
                payload = fetch_broker_summary(ticker, ds, ds, net=True)
                n = save_broker_snapshot(payload, db_path=db_path)
                if n > 0:
                    stats["saved_rows"] += n
                    stats["dates_ok"] += 1
                else:
                    stats["dates_skipped"] += 1
            except BrokerSnapshotValidationError as exc:
                # Reject only the malformed day. Never overwrite that date,
                # but continue the backfill/refresh for other trading dates.
                stats["dates_skipped"] += 1
                stats["invalid_snapshots"] += 1
                stats["invalid_dates"].append(ds)
                print(
                    f"[BROKER-SNAPSHOT-SKIP] {ticker.upper()} {ds}: {exc}"
                )
            except requests.HTTPError:
                stats["dates_skipped"] += 1
            time.sleep(sleep_seconds)
        d += timedelta(days=1)

    return stats


def collect_watchlist(
    tickers: Iterable[str],
    start_date: str,
    end_date: str,
    db_path: str = DB_PATH,
) -> pd.DataFrame:
    out = []
    for ticker in tickers:
        out.append(collect_daily(ticker, start_date, end_date, db_path=db_path))
    return pd.DataFrame(out)


def broker_detail(
    ticker: str,
    start_date: str,
    end_date: str,
    db_path: str = DB_PATH,
) -> pd.DataFrame:
    con = get_connection(db_path)
    df = pd.read_sql_query("""
        SELECT trade_date, ticker, broker, side, lots, value, avg_price
        FROM broker_daily
        WHERE ticker = ?
          AND trade_date BETWEEN ? AND ?
        ORDER BY trade_date, broker, side;
    """, con, params=[ticker.upper(), start_date, end_date])
    con.close()

    if df.empty:
        return df

    df["signed_lots"] = df.apply(
        lambda r: r["lots"] if r["side"] == "BUY" else -r["lots"], axis=1
    )
    df["signed_value"] = df.apply(
        lambda r: r["value"] if r["side"] == "BUY" else -r["value"], axis=1
    )
    return df


def aggregate_brokers(
    ticker: str,
    start_date: str,
    end_date: str,
    db_path: str = DB_PATH,
) -> pd.DataFrame:
    df = broker_detail(ticker, start_date, end_date, db_path)
    if df.empty:
        return pd.DataFrame(columns=[
            "broker", "buy_lots", "sell_lots", "net_lots",
            "buy_value", "sell_value", "avg_buy_price", "avg_sell_price"
        ])

    buy = (
        df[df.side == "BUY"]
        .groupby("broker", as_index=False)
        .agg(buy_lots=("lots", "sum"), buy_value=("value", "sum"))
    )
    sell = (
        df[df.side == "SELL"]
        .groupby("broker", as_index=False)
        .agg(sell_lots=("lots", "sum"), sell_value=("value", "sum"))
    )

    result = buy.merge(sell, on="broker", how="outer").fillna(0)
    result["net_lots"] = result["buy_lots"] - result["sell_lots"]

    # 1 lot IDX = 100 saham. Avg price berbasis total value / jumlah saham.
    result["avg_buy_price"] = result.apply(
        lambda r: r["buy_value"] / (r["buy_lots"] * 100)
        if r["buy_lots"] > 0 else None, axis=1
    )
    result["avg_sell_price"] = result.apply(
        lambda r: r["sell_value"] / (r["sell_lots"] * 100)
        if r["sell_lots"] > 0 else None, axis=1
    )

    return result.sort_values("net_lots", ascending=False).reset_index(drop=True)

def get_trading_dates(
    ticker: str,
    end_date: str,
    trading_days: int,
    db_path: str = DB_PATH,
) -> list[str]:
    """
    Mengambil N tanggal perdagangan terakhir yang benar-benar
    tersedia di database untuk ticker tertentu.
    """
    con = get_connection(db_path)

    query = """
        SELECT DISTINCT trade_date
        FROM broker_daily
        WHERE ticker = ?
          AND trade_date <= ?
        ORDER BY trade_date DESC
        LIMIT ?
    """

    df = pd.read_sql_query(
        query,
        con,
        params=[ticker.upper(), end_date, trading_days]
    )

    con.close()

    if df.empty:
        return []

    # urutkan kembali dari tanggal paling lama ke terbaru
    return sorted(df["trade_date"].tolist())


def analyze_ticker_trading_days(
    ticker: str,
    end_date: str,
    trading_days: int = 20,
    db_path: str = DB_PATH,
    last_price: Optional[float] = None,
) -> dict:

    dates = get_trading_dates(
        ticker=ticker,
        end_date=end_date,
        trading_days=trading_days,
        db_path=db_path,
    )

    actual_days = len(dates)

    if actual_days == 0:
        return {
            "ticker": ticker.upper(),
            "lookback_days": trading_days,
            "actual_trading_days": 0,
            "net_lots": 0,
            "score": 0,
            "label": "NO DATA",
            "data_status": "NO DATA",
        }

    start_date = dates[0]
    actual_end_date = dates[-1]

    agg = aggregate_brokers(
        ticker=ticker,
        start_date=start_date,
        end_date=actual_end_date,
        db_path=db_path,
    )

    if agg.empty:
        return {
            "ticker": ticker.upper(),
            "lookback_days": trading_days,
            "actual_trading_days": actual_days,
            "net_lots": 0,
            "score": 0,
            "label": "NO DATA",
            "data_status": "NO DATA",
        }

    gross_buy_lots = float(agg["buy_lots"].sum())
    gross_sell_lots = float(agg["sell_lots"].sum())

    net_lots = gross_buy_lots - gross_sell_lots

    buy_value = float(agg["buy_value"].sum())
    sell_value = float(agg["sell_value"].sum())

    avg_buy_price = (
        buy_value / (gross_buy_lots * 100)
        if gross_buy_lots > 0 else None
    )

    avg_sell_price = (
        sell_value / (gross_sell_lots * 100)
        if gross_sell_lots > 0 else None
    )

    total_lots = gross_buy_lots + gross_sell_lots

    accumulation_ratio = (
        net_lots / total_lots
        if total_lots > 0 else 0
    )

    dominant_buyers = int(
        (agg["net_lots"] > 0).sum()
    )

    dominant_sellers = int(
        (agg["net_lots"] < 0).sum()
    )

    # ==============================
    # ACCUMULATION SCORE
    # ==============================

    ratio_component = max(
        -1.0,
        min(1.0, accumulation_ratio)
    )

    breadth_total = (
        dominant_buyers +
        dominant_sellers
    )

    breadth = (
        (dominant_buyers - dominant_sellers)
        / breadth_total
        if breadth_total > 0 else 0
    )

    raw_score = (
        50
        + 40 * ratio_component
        + 10 * breadth
    )

    score = round(
        max(0, min(100, raw_score)),
        2
    )

    if score >= 70:
        label = "STRONG ACCUMULATION"

    elif score >= 57:
        label = "ACCUMULATION"

    elif score > 43:
        label = "NEUTRAL"

    elif score > 30:
        label = "DISTRIBUTION"

    else:
        label = "STRONG DISTRIBUTION"

    # ==============================
    # PRICE VS BROKER AVG BUY
    # ==============================

    price_vs_avg_buy_pct = None

    if last_price is not None and avg_buy_price:

        price_vs_avg_buy_pct = (
            (last_price / avg_buy_price) - 1
        ) * 100

    data_status = (
        "COMPLETE"
        if actual_days >= trading_days
        else f"ONLY {actual_days} DAYS"
    )

    return {
        "ticker": ticker.upper(),

        "lookback_days": trading_days,

        "actual_trading_days": actual_days,

        "start_date": start_date,

        "end_date": actual_end_date,

        "net_lots": round(net_lots),

        "gross_buy_lots": round(gross_buy_lots),

        "gross_sell_lots": round(gross_sell_lots),

        "buy_value": round(buy_value),

        "sell_value": round(sell_value),

        "dominant_buyers": dominant_buyers,

        "dominant_sellers": dominant_sellers,

        "avg_buy_price": (
            round(avg_buy_price, 2)
            if avg_buy_price else None
        ),

        "avg_sell_price": (
            round(avg_sell_price, 2)
            if avg_sell_price else None
        ),

        "last_price": last_price,

        "price_vs_avg_buy_pct": (
            round(price_vs_avg_buy_pct, 2)
            if price_vs_avg_buy_pct is not None
            else None
        ),

        "accumulation_ratio": round(
            accumulation_ratio,
            4
        ),

        "score": score,

        "label": label,

        "data_status": data_status,
    }


def add_consistency_score(
    df: pd.DataFrame,
    threshold: float = 0.02,
) -> pd.DataFrame:

    """
    consistency_score:
        0 - 100 = kekuatan konsistensi arah.

    consistency_direction:
        -100 = konsisten distribusi
           0 = mixed / netral
        +100 = konsisten akumulasi
    """

    if df.empty:
        return df

    result = df.copy()

    def signal(x):

        if pd.isna(x):
            return 0

        if x > threshold:
            return 1

        if x < -threshold:
            return -1

        return 0

    result["flow_signal"] = (
        result["accumulation_ratio"]
        .apply(signal)
    )

    score_map = {}

    for ticker, group in result.groupby("ticker"):

        signals = group[
            "flow_signal"
        ].tolist()

        n = len(signals)

        if n == 0:
            continue

        signal_sum = sum(signals)

        # -100 sampai +100
        direction_score = round(
            signal_sum / n * 100,
            2
        )

        # 0 sampai 100
        consistency_strength = round(
            abs(direction_score),
            2
        )

        if all(s == 1 for s in signals):

            label = (
                "CONSISTENT ACCUMULATION"
            )

        elif direction_score > 0:

            label = (
                "ACCUMULATION BIAS"
            )

        elif all(s == -1 for s in signals):

            label = (
                "CONSISTENT DISTRIBUTION"
            )

        elif direction_score < 0:

            label = (
                "DISTRIBUTION BIAS"
            )

        else:

            label = (
                "MIXED / NEUTRAL"
            )

        score_map[ticker] = {
            "strength":
                consistency_strength,

            "direction":
                direction_score,

            "label":
                label,
        }

    result["consistency_score"] = (
        result["ticker"]
        .map(
            lambda x:
            score_map.get(
                x,
                {}
            ).get(
                "strength",
                0
            )
        )
    )

    result["consistency_direction"] = (
        result["ticker"]
        .map(
            lambda x:
            score_map.get(
                x,
                {}
            ).get(
                "direction",
                0
            )
        )
    )

    result["consistency_label"] = (
        result["ticker"]
        .map(
            lambda x:
            score_map.get(
                x,
                {}
            ).get(
                "label",
                "NO DATA"
            )
        )
    )

    return result


def scan_watchlist_trading_days(
    tickers: Iterable[str],
    end_date: str,
    lookbacks=(5, 10, 20),
    db_path: str = DB_PATH,
    fetch_prices: bool = True,
) -> pd.DataFrame:

    tickers = [
        ticker.upper().strip()
        for ticker in tickers
    ]

    price_map = {}

    if fetch_prices:

        for i in range(
            0,
            len(tickers),
            20
        ):

            batch = tickers[i:i + 20]

            try:

                data = fetch_multi_quote(batch)

                for item in data.get(
                    "items",
                    []
                ):

                    price_map[
                        item.get("code")
                    ] = _parse_number(
                        item.get("lastPrice")
                    )

            except Exception:
                pass

    rows = []

    for ticker in tickers:

        for lookback in lookbacks:

            row = analyze_ticker_trading_days(
                ticker=ticker,
                end_date=end_date,
                trading_days=lookback,
                db_path=db_path,
                last_price=price_map.get(
                    ticker
                ),
            )

            rows.append(row)

    df = pd.DataFrame(rows)

    if df.empty:
        return df

    df = add_consistency_score(df)

    return (
        df.sort_values(
            [
                "lookback_days",
                "score",
                "net_lots",
            ],
            ascending=[
                True,
                False,
                False,
            ],
        )
        .reset_index(drop=True)
    )

def _date_window(end_date: str, calendar_days: int) -> tuple[str, str]:
    d1 = date.fromisoformat(end_date)
    d0 = d1 - timedelta(days=calendar_days - 1)
    return d0.isoformat(), d1.isoformat()


def analyze_ticker(
    ticker: str,
    end_date: str,
    lookback_calendar_days: int = 20,
    db_path: str = DB_PATH,
    last_price: Optional[float] = None,
) -> dict:
    start_date, end_date = _date_window(end_date, lookback_calendar_days)
    agg = aggregate_brokers(ticker, start_date, end_date, db_path)

    if agg.empty:
        return {
            "ticker": ticker.upper(),
            "lookback_days": lookback_calendar_days,
            "net_lots": 0,
            "score": 0,
            "label": "NO DATA",
        }

    gross_buy_lots = float(agg["buy_lots"].sum())
    gross_sell_lots = float(agg["sell_lots"].sum())
    net_lots = gross_buy_lots - gross_sell_lots
    buy_value = float(agg["buy_value"].sum())
    sell_value = float(agg["sell_value"].sum())

    net_value = buy_value - sell_value

    value_denom = buy_value + sell_value
    value_ratio = (
        net_value / value_denom
        if value_denom
        else 0.0
    )

    avg_buy_price = buy_value / (gross_buy_lots * 100) if gross_buy_lots else None
    avg_sell_price = sell_value / (gross_sell_lots * 100) if gross_sell_lots else None

    denom = gross_buy_lots + gross_sell_lots
    accumulation_ratio = net_lots / denom if denom else 0.0  # -1 sampai +1

    dominant_buyers = int((agg["net_lots"] > 0).sum())
    dominant_sellers = int((agg["net_lots"] < 0).sum())

    # Skor 0-100: fokus pada kekuatan net accumulation dan breadth.
    ratio_component = max(-1.0, min(1.0, accumulation_ratio))
    breadth_den = dominant_buyers + dominant_sellers
    breadth = ((dominant_buyers - dominant_sellers) / breadth_den) if breadth_den else 0.0

    raw = 50 + 40 * ratio_component + 10 * breadth
    score = round(max(0, min(100, raw)), 2)

    if score >= 70:
        label = "STRONG ACCUMULATION"
    elif score >= 57:
        label = "ACCUMULATION"
    elif score > 43:
        label = "NEUTRAL"
    elif score > 30:
        label = "DISTRIBUTION"
    else:
        label = "STRONG DISTRIBUTION"

    price_vs_avg_buy_pct = None
    if last_price is not None and avg_buy_price:
        price_vs_avg_buy_pct = (last_price / avg_buy_price - 1) * 100

    return {
        "ticker": ticker.upper(),
        "lookback_days": lookback_calendar_days,
        "start_date": start_date,
        "end_date": end_date,
        "net_lots": round(net_lots),
        "gross_buy_lots": round(gross_buy_lots),
        "gross_sell_lots": round(gross_sell_lots),
        "buy_value": round(buy_value),
        "sell_value": round(sell_value),
        "net_value": round(net_value),
        "value_ratio": round(value_ratio, 4),
        "dominant_buyers": dominant_buyers,
        "dominant_sellers": dominant_sellers,
        "avg_buy_price": round(avg_buy_price, 2) if avg_buy_price else None,
        "avg_sell_price": round(avg_sell_price, 2) if avg_sell_price else None,
        "last_price": last_price,
        "price_vs_avg_buy_pct": round(price_vs_avg_buy_pct, 2) if price_vs_avg_buy_pct is not None else None,
        "accumulation_ratio": round(accumulation_ratio, 4),
        "score": score,
        "label": label,
    }


def scan_watchlist(
    tickers: Iterable[str],
    end_date: str,
    lookbacks=(5, 10, 20),
    db_path: str = DB_PATH,
    fetch_prices: bool = True,
) -> pd.DataFrame:
    tickers = [t.upper().strip() for t in tickers]

    price_map = {}
    if fetch_prices:
        # Zapi multi-quote max 20 kode per panggilan.
        for i in range(0, len(tickers), 20):
            batch = tickers[i:i+20]
            try:
                data = fetch_multi_quote(batch)
                for item in data.get("items", []):
                    price_map[item.get("code")] = _parse_number(item.get("lastPrice"))
            except Exception:
                pass

    rows = []
    for ticker in tickers:
        for lb in lookbacks:
            rows.append(
                analyze_ticker(
                    ticker=ticker,
                    end_date=end_date,
                    lookback_calendar_days=lb,
                    db_path=db_path,
                    last_price=price_map.get(ticker),
                )
            )

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["lookback_days", "score", "net_lots"], ascending=[True, False, False])
    return df.reset_index(drop=True)


def save_scan(df: pd.DataFrame, db_path: str = DB_PATH) -> None:
    if df.empty:
        return
    scan_time = datetime.now().isoformat(timespec="seconds")
    cols = [
        "ticker", "lookback_days", "net_lots", "gross_buy_lots", "gross_sell_lots",
        "buy_value", "sell_value", "dominant_buyers", "dominant_sellers",
        "avg_buy_price", "avg_sell_price", "last_price", "price_vs_avg_buy_pct",
        "accumulation_ratio", "score", "label"
    ]
    temp = df.copy()
    for c in cols:
        if c not in temp.columns:
            temp[c] = None
    temp.insert(0, "scan_time", scan_time)

    con = get_connection(db_path)
    temp[["scan_time"] + cols].to_sql("scan_results", con, if_exists="append", index=False)
    con.close()


def top_brokers(
    ticker: str,
    end_date: str,
    lookback_calendar_days: int = 20,
    top_n: int = 10,
    db_path: str = DB_PATH,
) -> pd.DataFrame:
    start_date, end_date = _date_window(end_date, lookback_calendar_days)
    df = aggregate_brokers(ticker, start_date, end_date, db_path)
    if df.empty:
        return df
    return df.head(top_n)


def format_telegram_message(df: pd.DataFrame, lookback: int = 20, top_n: int = 10) -> str:
    x = df[df["lookback_days"] == lookback].copy()
    if x.empty:
        return f"Tidak ada data scan {lookback} hari."

    x = x.sort_values(["score", "net_lots"], ascending=False).head(top_n)

    lines = [f"📊 Broker Accumulation Tracker — {lookback} hari", ""]
    for _, r in x.iterrows():
        net_lots = int(r.get("net_lots", 0))
        score = r.get("score", 0)
        label = r.get("label", "-")
        avg = r.get("avg_buy_price")
        last = r.get("last_price")
        vs = r.get("price_vs_avg_buy_pct")

        lines.append(
            f"{r['ticker']} | {label}\n"
            f"Score {score:.1f} | Net {net_lots:+,} lot\n"
            f"Avg buy {avg:,.0f}" if pd.notna(avg) else f"{r['ticker']} | {label}\nScore {score:.1f} | Net {net_lots:+,} lot"
        )
        if pd.notna(last):
            suffix = f" | Last {last:,.0f}"
            if pd.notna(vs):
                suffix += f" ({vs:+.2f}% vs avg)"
            lines[-1] += suffix
        lines.append("")

    return "\n".join(lines)


def send_telegram(text: str) -> dict:
    return send_message(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, text)


if __name__ == "__main__":
    init_db()
    print(f"Database siap: {DB_PATH}")


def create_final_broker_score(df):

    results = []

    if df is None or df.empty:
        return pd.DataFrame()

    for ticker, group in df.groupby("ticker"):

        scores = dict(
            zip(
                group["lookback_days"],
                group["score"]
            )
        )

        score_5d = float(scores.get(5, 50))
        score_10d = float(scores.get(10, 50))
        score_20d = float(scores.get(20, 50))

        # ======================================
        # CONSISTENCY
        # ======================================

        consistency_score = (
            float(group["consistency_score"].iloc[0])
            if "consistency_score" in group.columns
            else 0.0
        )

        consistency_direction = (
            float(group["consistency_direction"].iloc[0])
            if "consistency_direction" in group.columns
            else 0.0
        )

        consistency_label = (
            group["consistency_label"].iloc[0]
            if "consistency_label" in group.columns
            else "MIXED / NEUTRAL"
        )

        # Direction:
        # -100 = full distribution
        #    0 = neutral / mixed
        # +100 = full accumulation
        #
        # Diubah ke skala 0-100:
        # -100 -> 0
        #    0 -> 50
        # +100 -> 100

        consistency_component = (
            50 + consistency_direction / 2
        )

        consistency_component = max(
            0,
            min(100, consistency_component)
        )

        # ======================================
        # FINAL FLOW SCORE
        # ======================================
        #
        # Consistency TIDAK dimasukkan lagi
        # ke final_score agar tidak double count.
        #
        # 5D  = 45%
        # 10D = 35%
        # 20D = 20%

        final_score = (
            score_5d * 0.45
            + score_10d * 0.35
            + score_20d * 0.20
        )

        final_score = round(
            max(0, min(100, final_score)),
            2
        )

        # ======================================
        # FINAL LABEL
        # ======================================

        if final_score >= 70:
            final_label = "STRONG ACCUMULATION"

        elif final_score >= 57:
            final_label = "ACCUMULATION"

        elif final_score > 43:
            final_label = "NEUTRAL"

        elif final_score > 30:
            final_label = "DISTRIBUTION"

        else:
            final_label = "STRONG DISTRIBUTION"

        # ======================================
        # Blok Tanggal
        # ======================================

        row5_df = group.loc[
            group["lookback_days"] == 5
        ]

        row10_df = group.loc[
            group["lookback_days"] == 10
        ]

        row20_df = group.loc[
            group["lookback_days"] == 20
        ]


        row5 = (
            row5_df.iloc[0]
            if not row5_df.empty
            else None
        )

        row10 = (
            row10_df.iloc[0]
            if not row10_df.empty
            else None
        )

        row20 = (
            row20_df.iloc[0]
            if not row20_df.empty
            else None
        )

        if row20 is not None:

            net_lots_20d = row20.get(
                "net_lots"
            )

            avg_buy_20d = row20.get(
                "avg_buy_price"
            )

            last_price = row20.get(
                "last_price"
            )

            price_vs_avg = row20.get(
                "price_vs_avg_buy_pct"
            )

        else:

            net_lots_20d = None
            avg_buy_20d = None
            last_price = None
            price_vs_avg = None


        # ======================================
        # RENTANG TANGGAL
        # row5 / row10 / row20 adalah Series
        # ======================================

        start_5d = (
            row5.get("start_date")
            if row5 is not None
            else None
        )

        end_5d = (
            row5.get("end_date")
            if row5 is not None
            else None
        )

        start_10d = (
            row10.get("start_date")
            if row10 is not None
            else None
        )

        end_10d = (
            row10.get("end_date")
            if row10 is not None
            else None
        )

        start_20d = (
            row20.get("start_date")
            if row20 is not None
            else None
        )

        end_20d = (
            row20.get("end_date")
            if row20 is not None
            else None
        )


        results.append({

            "start_5d": start_5d,
            "end_5d": end_5d,

            "start_10d": start_10d,
            "end_10d": end_10d,

            "start_20d": start_20d,
            "end_20d": end_20d,

            "ticker": ticker,

            "score_5d": round(score_5d, 2),
            "score_10d": round(score_10d, 2),
            "score_20d": round(score_20d, 2),

            "consistency_score":
                round(consistency_score, 2),

            "consistency_direction":
                round(consistency_direction, 2),

            "consistency_label":
                consistency_label,

            "consistency_component":
                round(consistency_component, 2),

            "final_score":
                final_score,

            "final_label":
                final_label,

            "net_lots_20d":
                net_lots_20d,

            "avg_buy_20d":
                avg_buy_20d,

            "last_price":
                last_price,

            "price_vs_avg_buy_pct":
                price_vs_avg
        })

    result = pd.DataFrame(results)

    return (
        result
        .sort_values(
            ["final_score", "net_lots_20d"],
            ascending=[False, False]
        )
        .reset_index(drop=True)
    )

def value(v, decimals=0):
    if v is None or pd.isna(v):
        return "-"
    return f"{v:,.{decimals}f}"


def signed(v, decimals=0):
    if v is None or pd.isna(v):
        return "-"
    return f"{v:+,.{decimals}f}"

def format_dashboard_telegram_message(
    df: pd.DataFrame,
    end_date: str = None,
    top_n: int = 5,
) -> str:

    if df is None or df.empty:
        return "Tidak ada data Broker Accumulation Screener."

    x = df.copy()

    # ==========================================
    # SORTING
    # ==========================================

    sort_cols = []
    ascending = []

    if "final_score" in x.columns:
        sort_cols.append("final_score")
        ascending.append(False)

    if "ytd_score" in x.columns:
        sort_cols.append("ytd_score")
        ascending.append(False)

    if "broker_dominance_score" in x.columns:
        sort_cols.append("broker_dominance_score")
        ascending.append(False)

    if sort_cols:
        x = x.sort_values(
            sort_cols,
            ascending=ascending
        )

    x = x.head(top_n)


    # ==========================================
    # HELPERS
    # ==========================================

    def value(v, decimals=0):

        if v is None or pd.isna(v):
            return "N/A"

        return f"{v:,.{decimals}f}"


    def signed(v, decimals=0):

        if v is None or pd.isna(v):
            return "N/A"

        return f"{v:+,.{decimals}f}"


    def buyer_persistence_label(label):

        mapping = {
            "VERY DOMINANT": "VERY PERSISTENT",
            "DOMINANT": "PERSISTENT",
            "MODERATE": "MODERATE",
            "WEAK": "LOW",
            "VERY WEAK": "VERY LOW",
        }

        return mapping.get(
            str(label),
            str(label)
        )


    # ==========================================
    # HEADER
    # ==========================================

    lines = [
        "📊 BROKER ACCUMULATION SCREENER"
    ]

    if end_date is not None:
        lines.append(
            f"> Data Until: {end_date}"
        )

    lines.append(
        f"> Stocks Shown: {len(x)}"
    )

    lines.append("")


    # ==========================================
    # LOOP PER SAHAM
    # ==========================================

    for rank, (_, r) in enumerate(
        x.iterrows(),
        start=1
    ):
        start_5d = r.get("start_5d")
        end_5d = r.get("end_5d")

        start_10d = r.get("start_10d")
        end_10d = r.get("end_10d")

        start_20d = r.get("start_20d")
        end_20d = r.get("end_20d")

        ytd_start = r.get("ytd_start")
        ytd_end = r.get("ytd_end")

        # --------------------------------------
        # MAIN SCORE
        # --------------------------------------

        ticker = r.get(
            "ticker",
            "N/A"
        )

        final_score = r.get(
            "final_score"
        )

        final_label = r.get(
            "final_label",
            "N/A"
        )

        score_5d = r.get(
            "score_5d"
        )

        score_10d = r.get(
            "score_10d"
        )

        score_20d = r.get(
            "score_20d"
        )

        net20 = r.get(
            "net_lots_20d"
        )


        # --------------------------------------
        # CONSISTENCY
        # --------------------------------------

        consistency_direction = r.get(
            "consistency_direction"
        )

        consistency_label = r.get(
            "consistency_label",
            "MIXED / NEUTRAL"
        )

        if (
            consistency_direction is not None
            and not pd.isna(consistency_direction)
        ):
            direction_persistence = abs(
                float(consistency_direction)
            )
        else:
            direction_persistence = None


        # --------------------------------------
        # YTD
        # --------------------------------------

        ytd_score = r.get(
            "ytd_score"
        )

        ytd_label = r.get(
            "ytd_label",
            "N/A"
        )

        ytd_net = r.get(
            "ytd_net_lots"
        )

        flow_alignment = r.get(
            "flow_alignment",
            "N/A"
        )


        # --------------------------------------
        # DOMINANT BROKER
        # --------------------------------------

        broker = r.get(
            "dominant_broker"
        )

        broker_dom_score = r.get(
            "broker_dominance_score"
        )

        broker_dom_label = r.get(
            "broker_dominance_label",
            "N/A"
        )

        horizon_count = r.get(
            "broker_horizon_count"
        )

        broker_net_5d = r.get(
            "broker_net_5d"
        )

        broker_net_20d = r.get(
            "broker_net_20d"
        )

        broker_net_ytd = r.get(
            "broker_net_ytd"
        )

        broker_avg = r.get(
            "broker_avg_buy_20d"
        )

        broker_today_date = r.get(
            "broker_today_date"
        )

        broker_today_action = r.get(
            "broker_today_action"
        )

        broker_today_buy = r.get(
            "broker_today_buy_lots"
        )

        broker_today_sell = r.get(
            "broker_today_sell_lots"
        )

        broker_today_net = r.get(
            "broker_today_net_lots"
        )


        # --------------------------------------
        # PRICE
        # --------------------------------------

        last_price = r.get(
            "last_price"
        )

        broker_price_vs_avg_pct = None

        if (
            broker_avg is not None
            and not pd.isna(broker_avg)
            and broker_avg != 0
            and last_price is not None
            and not pd.isna(last_price)
        ):

            broker_price_vs_avg_pct = (
                (last_price - broker_avg)
                / broker_avg
                * 100
            )


        # ======================================
        # OUTPUT
        # ======================================

        lines.append(
            f"{rank}. {ticker} | {final_label}"
        )

        # FLOW

        lines.append(
            f"> Flow Score: "
            f"{value(final_score, 2)}"
        )

        lines.append(
            f"> Score 5D / 10D / 20D: "
            f"{value(score_5d, 2)} / "
            f"{value(score_10d, 2)} / "
            f"{value(score_20d, 2)}"
        )

        if (
            start_5d is not None
            and start_10d is not None
            and start_20d is not None
            and end_20d is not None
        ):
            lines.append(
                f"> Period 5D/10D/20D: "
                f"{start_5d} / "
                f"{start_10d} / "
                f"{start_20d} → "
                f"{end_20d}"
            )

        lines.append(
            f"> Net Flow 20D: "
            f"{signed(net20)} lot"
        )


        # CONSISTENCY

        lines.append(
            f"> Flow Direction: "
            f"{consistency_label}"
        )

        if direction_persistence is not None:

            lines.append(
                f"> Persistence: "
                f"{value(direction_persistence, 2)}%"
            )


        # YTD

        if (
            ytd_score is not None
            and not pd.isna(ytd_score)
        ):
            lines.append(
                f"> YTD Period: "
                f"{ytd_start} → {ytd_end}"
            )

            lines.append(
                f"> YTD Score: "
                f"{value(ytd_score, 2)}"
                f" | {ytd_label}"
            )

        if (
            ytd_net is not None
            and not pd.isna(ytd_net)
        ):

            lines.append(
                f"> YTD Net: "
                f"{signed(ytd_net)} lot"
            )

        if (
            flow_alignment is not None
            and str(flow_alignment) != "N/A"
        ):

            lines.append(
                f"> Alignment: "
                f"{flow_alignment}"
            )


        # BROKER

        if (
            broker is not None
            and not pd.isna(broker)
        ):

            lines.append(
                f"> Top Buyer: "
                f"{broker}"
            )

            if (
                broker_dom_score is not None
                and not pd.isna(broker_dom_score)
            ):

                lines.append(
                    f"> Buyer Persist: "
                    f"{value(broker_dom_score, 1)}"
                    f" | "
                    f"{buyer_persistence_label(broker_dom_label)}"
                )

            if (
                horizon_count is not None
                and not pd.isna(horizon_count)
            ):

                lines.append(
                    f"> Buyer Horizon: "
                    f"{int(horizon_count)}/3"
                )

                # =========================
                # DAILY ACTION
                # =========================

            if (
                broker_today_action is not None
                and broker_today_action not in [
                    "NO DATA",
                    "NO ACTIVITY"
                ]
            ):

                lines.append(
                    f"> Latest Action: "
                    f"{broker_today_action}"
                )

                if (
                    broker_today_date is not None
                    and not pd.isna(broker_today_date)
                ):
                    lines.append(
                        f"> Action Date: "
                        f"{broker_today_date}"
                    )

                if (
                    broker_today_buy is not None
                    and not pd.isna(broker_today_buy)
                ):
                    lines.append(
                        f"> Daily Buy: "
                        f"{value(broker_today_buy)} lot"
                    )

                if (
                    broker_today_sell is not None
                    and not pd.isna(broker_today_sell)
                ):
                    lines.append(
                        f"> Daily Sell: "
                        f"{value(broker_today_sell)} lot"
                    )

                if (
                    broker_today_net is not None
                    and not pd.isna(broker_today_net)
                ):
                    lines.append(
                        f"> Daily Net: "
                        f"{signed(broker_today_net)} lot"
                    )

            if (
                broker_net_5d is not None
                and not pd.isna(broker_net_5d)
            ):

                lines.append(
                    f"> {broker} Net 5D: "
                    f"{signed(broker_net_5d)} lot"
                )

            if (
                broker_net_20d is not None
                and not pd.isna(broker_net_20d)
            ):

                lines.append(
                    f"> {broker} Net 20D: "
                    f"{signed(broker_net_20d)} lot"
                )

            if (
                broker_net_ytd is not None
                and not pd.isna(broker_net_ytd)
            ):

                lines.append(
                    f"> {broker} Net YTD: "
                    f"{signed(broker_net_ytd)} lot"
                )

            if (
                broker_avg is not None
                and not pd.isna(broker_avg)
            ):

                lines.append(
                    f"> {broker} Avg Buy 20D: "
                    f"{value(broker_avg, 2)}"
                )


        # PRICE

        if (
            last_price is not None
            and not pd.isna(last_price)
        ):

            lines.append(
                f"> Last Price: "
                f"{value(last_price)}"
            )

        if broker_price_vs_avg_pct is not None:

            broker_name = (
                broker
                if broker is not None
                and not pd.isna(broker)
                else "Broker"
            )

            lines.append(
                f"> Price vs {broker_name} Avg: "
                f"{signed(broker_price_vs_avg_pct, 2)}%"
            )


        # SEPARATOR

        lines.append(
            "────────────────────"
        )

    return "\n".join(lines)

#Icremental Collector tujuannya setelah history tersimpan di sqlite akan mengambil tanggal data terakhir

def get_last_stored_date(
    ticker: str,
    db_path: str = DB_PATH,
):
    """
    Mengambil tanggal broker terakhir yang sudah tersimpan
    untuk ticker tertentu.
    """

    con = get_connection(db_path)

    row = con.execute(
        """
        SELECT MAX(trade_date)
        FROM broker_daily
        WHERE ticker = ?
        """,
        [ticker.upper().strip()]
    ).fetchone()

    con.close()

    if row and row[0]:
        return row[0]

    return None


def collect_missing_dates(
    tickers,
    end_date: str,
    start_date: str | None = None,
    db_path: str = DB_PATH,
    sleep_seconds: float = 0.25,
):
    """
    Mengambil hanya tanggal setelah data terakhir di database.

    Jika ticker belum pernah dikumpulkan:
    - gunakan start_date sebagai awal histori.

    Jika ticker sudah punya histori:
    - otomatis mulai dari hari setelah tanggal terakhir.
    """

    results = []

    end_dt = date.fromisoformat(end_date)

    for ticker in tickers:

        ticker = ticker.upper().strip()

        last_date = get_last_stored_date(
            ticker,
            db_path=db_path
        )

        # ==========================
        # Tentukan tanggal mulai
        # ==========================

        if last_date:

            current = (
                date.fromisoformat(last_date)
                + timedelta(days=1)
            )

        elif start_date:

            current = date.fromisoformat(
                start_date
            )

        else:

            results.append({
                "ticker": ticker,
                "last_date_before": None,
                "requested_dates": 0,
                "dates_ok": 0,
                "dates_skipped": 0,
                "errors": 0,
                "saved_rows": 0,
                "status": "START_DATE REQUIRED"
            })

            continue

        requested_dates = 0
        dates_ok = 0
        dates_skipped = 0
        invalid_snapshots = 0
        errors = 0
        saved_rows = 0

        # ==========================
        # Sudah paling update
        # ==========================

        if current > end_dt:

            results.append({
                "ticker": ticker,
                "last_date_before": last_date,
                "last_date_after": last_date,
                "requested_dates": 0,
                "dates_ok": 0,
                "dates_skipped": 0,
                "errors": 0,
                "saved_rows": 0,
                "status": "UP TO DATE"
            })

            continue

        # ==========================
        # Ambil hanya tanggal baru
        # ==========================

        while current <= end_dt:

            # hanya Senin - Jumat
            if current.weekday() < 5:

                ds = current.isoformat()

                requested_dates += 1

                try:

                    payload = fetch_broker_summary(
                        ticker=ticker,
                        start_date=ds,
                        end_date=ds,
                        net=True
                    )

                    n = save_broker_snapshot(
                        payload,
                        db_path=db_path
                    )

                    if n > 0:

                        saved_rows += n
                        dates_ok += 1

                    else:

                        dates_skipped += 1

                except BrokerSnapshotValidationError as exc:

                    # A malformed upstream day must not abort the entire
                    # incremental refresh. Preserve existing data and leave
                    # this date incomplete for later review/retry.
                    dates_skipped += 1
                    invalid_snapshots += 1
                    print(
                        f"[BROKER-SNAPSHOT-SKIP] {ticker} {ds}: {exc}"
                    )

                except requests.HTTPError:

                    errors += 1

                except requests.RequestException:

                    errors += 1

                time.sleep(sleep_seconds)

            current += timedelta(days=1)

        last_date_after = get_last_stored_date(
            ticker,
            db_path=db_path
        )
        
        if errors > 0 and dates_ok == 0:
            status = "ERROR"

        elif dates_ok > 0:
            status = "UPDATED"

        elif dates_skipped > 0:
            status = "NO DATA"

        else:
            status = "UP TO DATE"


        results.append({
            "ticker": ticker,
            "last_date_before": last_date,
            "last_date_after": last_date_after,
            "requested_dates": requested_dates,
            "dates_ok": dates_ok,
            "dates_skipped": dates_skipped,
            "invalid_snapshots": invalid_snapshots,
            "errors": errors,
            "saved_rows": saved_rows,
            "status": status
        })

    return pd.DataFrame(results)



from zoneinfo import ZoneInfo

#supaya bot tidak membuang API secara cuma-cuma

def get_safe_end_date(
    requested_end_date: str,
    cutoff_hour: int = 18
) -> str:
    """
    Jangan meminta data hari berjalan sebelum cutoff.
    Default cutoff 18:00 WIB sebagai buffer agar data
    broker summary harian sudah tersedia.
    """

    jakarta = ZoneInfo("Asia/Jakarta")
    now = datetime.now(jakarta)

    requested = date.fromisoformat(
        requested_end_date
    )

    today = now.date()

    safe_date = min(requested, today)
    if safe_date == today and now.hour < cutoff_hour:
        safe_date -= timedelta(days=1)
    while safe_date.weekday() >= 5:
        safe_date -= timedelta(days=1)
    return safe_date.isoformat()


def collect_missing_range(
    tickers,
    start_date: str,
    end_date: str,
    db_path: str = DB_PATH,
    sleep_seconds: float = 0.25,
):
    """
    Backfill histori dalam rentang tertentu.
    Hanya meminta tanggal Senin-Jumat yang belum ada di database.
    Cocok untuk mengisi histori lama, misalnya sejak 1 Januari.
    """

    results = []

    start_dt = date.fromisoformat(start_date)
    end_dt = date.fromisoformat(end_date)

    for ticker in tickers:

        ticker = ticker.upper().strip()

        # Ambil semua tanggal yang SUDAH ada
        con = get_connection(db_path)

        existing_df = pd.read_sql_query(
            """
            SELECT DISTINCT trade_date
            FROM broker_daily
            WHERE ticker = ?
              AND trade_date BETWEEN ? AND ?
            """,
            con,
            params=[
                ticker,
                start_date,
                end_date
            ]
        )

        con.close()

        existing_dates = set(
            existing_df["trade_date"].tolist()
        )

        requested_dates = 0
        dates_ok = 0
        dates_skipped = 0
        errors = 0
        saved_rows = 0

        current = start_dt

        while current <= end_dt:

            # hanya Senin - Jumat
            if current.weekday() < 5:

                ds = current.isoformat()

                # Skip kalau tanggal sudah ada di DB
                if ds in existing_dates:
                    current += timedelta(days=1)
                    continue

                requested_dates += 1

                try:

                    payload = fetch_broker_summary(
                        ticker=ticker,
                        start_date=ds,
                        end_date=ds,
                        net=True
                    )

                    n = save_broker_snapshot(
                        payload,
                        db_path=db_path
                    )

                    if n > 0:

                        saved_rows += n
                        dates_ok += 1

                    else:

                        dates_skipped += 1

                except requests.HTTPError:

                    errors += 1

                except requests.RequestException:

                    errors += 1

                time.sleep(sleep_seconds)

            current += timedelta(days=1)

        results.append({
            "ticker": ticker,
            "start_date": start_date,
            "end_date": end_date,
            "requested_dates": requested_dates,
            "dates_ok": dates_ok,
            "dates_skipped": dates_skipped,
            "errors": errors,
            "saved_rows": saved_rows
        })

    return pd.DataFrame(results)

def analyze_ticker_ytd(
    ticker: str,
    end_date: str,
    db_path: str = DB_PATH,
    last_price: Optional[float] = None,
) -> dict:

    ticker = ticker.upper().strip()

    end_dt = date.fromisoformat(end_date)

    start_date = f"{end_dt.year}-01-01"

    # ==============================
    # CEK TANGGAL YANG TERSEDIA
    # ==============================

    con = get_connection(db_path)

    dates_df = pd.read_sql_query(
        """
        SELECT DISTINCT trade_date
        FROM broker_daily
        WHERE ticker = ?
          AND trade_date BETWEEN ? AND ?
        ORDER BY trade_date
        """,
        con,
        params=[
            ticker,
            start_date,
            end_date
        ]
    )

    con.close()

    if dates_df.empty:

        return {
            "ticker": ticker,
            "ytd_start": start_date,
            "ytd_end": end_date,
            "ytd_trading_days": 0,
            "ytd_net_lots": 0,
            "ytd_score": 0,
            "ytd_label": "NO DATA"
        }

    first_date = dates_df[
        "trade_date"
    ].iloc[0]

    last_date = dates_df[
        "trade_date"
    ].iloc[-1]

    trading_days = len(dates_df)

    # ==============================
    # AGREGASI BROKER
    # ==============================

    agg = aggregate_brokers(
        ticker=ticker,
        start_date=first_date,
        end_date=last_date,
        db_path=db_path
    )

    if agg.empty:

        return {
            "ticker": ticker,
            "ytd_start": first_date,
            "ytd_end": last_date,
            "ytd_trading_days": trading_days,
            "ytd_net_lots": 0,
            "ytd_score": 0,
            "ytd_label": "NO DATA"
        }

    gross_buy_lots = float(
        agg["buy_lots"].sum()
    )

    gross_sell_lots = float(
        agg["sell_lots"].sum()
    )

    net_lots = (
        gross_buy_lots
        - gross_sell_lots
    )

    buy_value = float(
        agg["buy_value"].sum()
    )

    sell_value = float(
        agg["sell_value"].sum()
    )

    # ==============================
    # AVERAGE PRICE
    # ==============================

    avg_buy_price = (
        buy_value
        / (gross_buy_lots * 100)
        if gross_buy_lots > 0
        else None
    )

    avg_sell_price = (
        sell_value
        / (gross_sell_lots * 100)
        if gross_sell_lots > 0
        else None
    )

    # ==============================
    # ACCUMULATION RATIO
    # ==============================

    total_activity = (
        gross_buy_lots
        + gross_sell_lots
    )

    accumulation_ratio = (
        net_lots / total_activity
        if total_activity > 0
        else 0
    )

    dominant_buyers = int(
        (agg["net_lots"] > 0).sum()
    )

    dominant_sellers = int(
        (agg["net_lots"] < 0).sum()
    )

    broker_count = (
        dominant_buyers
        + dominant_sellers
    )

    breadth = (
        (
            dominant_buyers
            - dominant_sellers
        )
        / broker_count
        if broker_count > 0
        else 0
    )

    # ==============================
    # YTD SCORE
    # ==============================

    ratio_component = max(
        -1,
        min(
            1,
            accumulation_ratio
        )
    )

    raw_score = (
        50
        + 40 * ratio_component
        + 10 * breadth
    )

    ytd_score = round(
        max(
            0,
            min(
                100,
                raw_score
            )
        ),
        2
    )

    if ytd_score >= 70:

        ytd_label = (
            "STRONG ACCUMULATION"
        )

    elif ytd_score >= 57:

        ytd_label = (
            "ACCUMULATION"
        )

    elif ytd_score > 43:

        ytd_label = "NEUTRAL"

    elif ytd_score > 30:

        ytd_label = (
            "DISTRIBUTION"
        )

    else:

        ytd_label = (
            "STRONG DISTRIBUTION"
        )

    # ==============================
    # PRICE VS YTD AVG BUY
    # ==============================

    price_vs_avg = None

    if (
        last_price is not None
        and avg_buy_price
    ):

        price_vs_avg = (
            (
                last_price
                / avg_buy_price
            )
            - 1
        ) * 100

    return {

        "ticker": ticker,

        "ytd_start": first_date,

        "ytd_end": last_date,

        "ytd_trading_days":
            trading_days,

        "ytd_net_lots":
            round(net_lots),

        "ytd_buy_lots":
            round(gross_buy_lots),

        "ytd_sell_lots":
            round(gross_sell_lots),

        "ytd_avg_buy":
            round(
                avg_buy_price,
                2
            )
            if avg_buy_price
            else None,

        "ytd_avg_sell":
            round(
                avg_sell_price,
                2
            )
            if avg_sell_price
            else None,

        "ytd_accumulation_ratio":
            round(
                accumulation_ratio,
                4
            ),

        "ytd_dominant_buyers":
            dominant_buyers,

        "ytd_dominant_sellers":
            dominant_sellers,

        "last_price":
            last_price,

        "ytd_price_vs_avg_pct":
            round(
                price_vs_avg,
                2
            )
            if price_vs_avg
            is not None
            else None,

        "ytd_score":
            ytd_score,

        "ytd_label":
            ytd_label
    }

def scan_watchlist_ytd(
    tickers,
    end_date: str,
    db_path: str = DB_PATH,
    fetch_prices: bool = True,
):

    tickers = [
        ticker.upper().strip()
        for ticker in tickers
    ]

    price_map = {}

    if fetch_prices:

        for i in range(
            0,
            len(tickers),
            20
        ):

            batch = tickers[i:i + 20]

            try:

                data = fetch_multi_quote(
                    batch
                )

                for item in data.get(
                    "items",
                    []
                ):

                    price_map[
                        item.get("code")
                    ] = _parse_number(
                        item.get(
                            "lastPrice"
                        )
                    )

            except Exception:
                pass

    rows = []

    for ticker in tickers:

        row = analyze_ticker_ytd(
            ticker=ticker,
            end_date=end_date,
            db_path=db_path,
            last_price=price_map.get(
                ticker
            )
        )

        rows.append(row)

    df = pd.DataFrame(rows)

    if df.empty:
        return df

    return (
        df.sort_values(
            [
                "ytd_score",
                "ytd_net_lots"
            ],
            ascending=[
                False,
                False
            ]
        )
        .reset_index(drop=True)
    )


def top_accumulating_brokers(
    ticker: str,
    start_date: str,
    end_date: str,
    top_n: int = 10,
    db_path: str = DB_PATH,
):

    df = aggregate_brokers(
        ticker=ticker,
        start_date=start_date,
        end_date=end_date,
        db_path=db_path
    )

    if df.empty:
        return df

    df = df.copy()

    total_positive_net = (
        df.loc[
            df["net_lots"] > 0,
            "net_lots"
        ].sum()
    )

    df["accumulation_share_pct"] = (
        df["net_lots"]
        / total_positive_net
        * 100
        if total_positive_net > 0
        else 0
    )

    return (
        df.sort_values(
            "net_lots",
            ascending=False
        )
        .head(top_n)
        .reset_index(drop=True)
    )

def top_broker_flow(
    ticker: str,
    start_date: str,
    end_date: str,
    top_n: int = 10,
    mode: str = "accumulation",
    db_path: str = DB_PATH,
):

    df = aggregate_brokers(
        ticker=ticker,
        start_date=start_date,
        end_date=end_date,
        db_path=db_path
    )

    if df.empty:
        return df

    df = df.copy()

    if mode == "accumulation":

        df = df[
            df["net_lots"] > 0
        ]

        df = df.sort_values(
            "net_lots",
            ascending=False
        )

        total = df[
            "net_lots"
        ].sum()

    elif mode == "distribution":

        df = df[
            df["net_lots"] < 0
        ]

        df = df.sort_values(
            "net_lots",
            ascending=True
        )

        total = abs(
            df["net_lots"].sum()
        )

    else:

        raise ValueError(
            "mode harus 'accumulation' "
            "atau 'distribution'"
        )

    if total > 0:

        df[
            "flow_share_pct"
        ] = (
            abs(df["net_lots"])
            / total
            * 100
        )

    else:

        df[
            "flow_share_pct"
        ] = 0

    return (
        df.head(top_n)
        .reset_index(drop=True)
    )

def top_broker_trading_days(
    ticker: str,
    end_date: str,
    trading_days: int = 20,
    top_n: int = 10,
    mode: str = "accumulation",
    db_path: str = DB_PATH,
):

    dates = get_trading_dates(
        ticker=ticker,
        end_date=end_date,
        trading_days=trading_days,
        db_path=db_path
    )

    if not dates:

        return pd.DataFrame()

    return top_broker_flow(
        ticker=ticker,
        start_date=dates[0],
        end_date=dates[-1],
        top_n=top_n,
        mode=mode,
        db_path=db_path
    )

def top_broker_ytd(
    ticker: str,
    end_date: str,
    top_n: int = 10,
    mode: str = "accumulation",
    db_path: str = DB_PATH,
):
    """
    Top broker accumulation / distribution
    dari awal tahun sampai end_date.
    """

    ticker = ticker.upper().strip()

    year = date.fromisoformat(
        end_date
    ).year

    start_date = f"{year}-01-01"

    return top_broker_flow(
        ticker=ticker,
        start_date=start_date,
        end_date=end_date,
        top_n=top_n,
        mode=mode,
        db_path=db_path
    )

def get_dominant_broker_daily_action(
    ticker: str,
    broker: str,
    trade_date: str,
):
    """
    Cek aksi broker tertentu pada satu tanggal:
    NET BUY / NET SELL / FLAT.

    Menggunakan data broker summary satu hari.
    """

    if not ticker or not broker or not trade_date:
        return {
            "broker_today_date": trade_date,
            "broker_today_buy_lots": 0,
            "broker_today_sell_lots": 0,
            "broker_today_net_lots": 0,
            "broker_today_action": "NO DATA",
        }

    daily = top_accumulating_brokers(
        ticker=ticker,
        start_date=trade_date,
        end_date=trade_date,
        top_n=100
    )

    if daily is None or daily.empty:
        return {
            "broker_today_date": trade_date,
            "broker_today_buy_lots": 0,
            "broker_today_sell_lots": 0,
            "broker_today_net_lots": 0,
            "broker_today_action": "NO DATA",
        }

    broker_row = daily[
        daily["broker"].astype(str).str.upper()
        == str(broker).upper()
    ]

    if broker_row.empty:
        return {
            "broker_today_date": trade_date,
            "broker_today_buy_lots": 0,
            "broker_today_sell_lots": 0,
            "broker_today_net_lots": 0,
            "broker_today_action": "NO ACTIVITY",
        }

    r = broker_row.iloc[0]

    buy_lots = float(r.get("buy_lots", 0) or 0)
    sell_lots = float(r.get("sell_lots", 0) or 0)
    net_lots = float(r.get("net_lots", buy_lots - sell_lots) or 0)

    if net_lots > 0:
        action = "NET BUY"

    elif net_lots < 0:
        action = "NET SELL"

    else:
        action = "FLAT"

    return {
        "broker_today_date": trade_date,
        "broker_today_buy_lots": buy_lots,
        "broker_today_sell_lots": sell_lots,
        "broker_today_net_lots": net_lots,
        "broker_today_action": action,
    }

def add_dominant_broker_daily_action(
    df: pd.DataFrame,
    trade_date: str,
):

    results = []

    for _, row in df.iterrows():

        ticker = row.get("ticker")
        broker = row.get("dominant_broker")

        action = get_dominant_broker_daily_action(
            ticker=ticker,
            broker=broker,
            trade_date=trade_date,
        )

        new_row = row.to_dict()
        new_row.update(action)

        results.append(new_row)

    return pd.DataFrame(results)

def broker_dominance_score(
    ticker: str,
    end_date: str,
    top_n: int = 5,
    db_path: str = DB_PATH,
):
    """
    Mengukur broker accumulator yang dominan pada:
    - 5 trading days
    - 20 trading days
    - YTD

    Bobot:
    5D  = 30%
    20D = 40%
    YTD = 30%

    Score maksimum = 100.
    """

    ticker = ticker.upper().strip()

    # =============================
    # Ambil top accumulator
    # =============================

    top_5d = top_broker_trading_days(
        ticker=ticker,
        end_date=end_date,
        trading_days=5,
        top_n=top_n,
        mode="accumulation",
        db_path=db_path
    )

    top_20d = top_broker_trading_days(
        ticker=ticker,
        end_date=end_date,
        trading_days=20,
        top_n=top_n,
        mode="accumulation",
        db_path=db_path
    )

    top_ytd = top_broker_ytd(
        ticker=ticker,
        end_date=end_date,
        top_n=top_n,
        mode="accumulation",
        db_path=db_path
    )

    # =============================
    # Rank → points
    # top 1 = 100
    # top 2 = 80
    # ...
    # =============================

    def rank_points(rank):

        if rank is None:
            return 0

        return max(
            0,
            100 * (
                top_n - rank + 1
            ) / top_n
        )

    brokers = {}

    def process_horizon(
        df,
        horizon,
        weight
    ):

        if df is None or df.empty:
            return

        for idx, row in df.reset_index(
            drop=True
        ).iterrows():

            broker = row["broker"]

            rank = idx + 1

            if broker not in brokers:

                brokers[broker] = {
                    "ticker": ticker,
                    "broker": broker,

                    "rank_5d": None,
                    "rank_20d": None,
                    "rank_ytd": None,

                    "net_lots_5d": 0,
                    "net_lots_20d": 0,
                    "net_lots_ytd": 0,

                    "avg_buy_5d": None,
                    "avg_buy_20d": None,
                    "avg_buy_ytd": None,

                    "share_5d_pct": 0,
                    "share_20d_pct": 0,
                    "share_ytd_pct": 0,

                    "dominance_score": 0
                }

            b = brokers[broker]

            b[
                f"rank_{horizon}"
            ] = rank

            b[
                f"net_lots_{horizon}"
            ] = row.get(
                "net_lots",
                0
            )

            b[
                f"avg_buy_{horizon}"
            ] = row.get(
                "avg_buy_price"
            )

            b[
                f"share_{horizon}_pct"
            ] = row.get(
                "flow_share_pct",
                0
            )

            b["dominance_score"] += (
                rank_points(rank)
                * weight
            )

    # =============================
    # Bobot horizon
    # =============================

    process_horizon(
        top_5d,
        "5d",
        0.30
    )

    process_horizon(
        top_20d,
        "20d",
        0.40
    )

    process_horizon(
        top_ytd,
        "ytd",
        0.30
    )

    # =============================
    # Buat DataFrame
    # =============================

    if not brokers:
        return pd.DataFrame()

    result = pd.DataFrame(
        brokers.values()
    )

    # berapa horizon broker muncul
    result[
        "horizon_count"
    ] = (
        result[
            [
                "rank_5d",
                "rank_20d",
                "rank_ytd"
            ]
        ]
        .notna()
        .sum(axis=1)
    )

    result[
        "dominance_score"
    ] = (
        result[
            "dominance_score"
        ]
        .round(2)
    )

    # =============================
    # Label
    # =============================

    def dominance_label(score):

        if score >= 80:
            return "VERY DOMINANT"

        elif score >= 60:
            return "DOMINANT"

        elif score >= 40:
            return "MODERATE"

        elif score >= 20:
            return "WEAK"

        else:
            return "VERY WEAK"

    result[
        "dominance_label"
    ] = result[
        "dominance_score"
    ].apply(
        dominance_label
    )

    return (
        result
        .sort_values(
            [
                "dominance_score",
                "horizon_count",
                "net_lots_20d"
            ],
            ascending=[
                False,
                False,
                False
            ]
        )
        .reset_index(drop=True)
    )

def scan_watchlist_broker_dominance(
    tickers,
    end_date: str,
    top_n: int = 5,
    db_path: str = DB_PATH,
):

    rows = []

    for ticker in tickers:

        ticker = ticker.upper().strip()

        df = broker_dominance_score(
            ticker=ticker,
            end_date=end_date,
            top_n=top_n,
            db_path=db_path
        )

        if df.empty:

            rows.append({
                "ticker": ticker,
                "dominant_broker": None,
                "broker_dominance_score": 0,
                "broker_dominance_label":
                    "NO DATA",
                "broker_horizon_count": 0,
                "broker_net_5d": 0,
                "broker_net_20d": 0,
                "broker_net_ytd": 0,
                "broker_avg_buy_20d": None
            })

            continue

        best = df.iloc[0]

        rows.append({

            "ticker":
                ticker,

            "dominant_broker":
                best["broker"],

            "broker_dominance_score":
                best["dominance_score"],

            "broker_dominance_label":
                best["dominance_label"],

            "broker_horizon_count":
                best["horizon_count"],

            "broker_net_5d":
                best["net_lots_5d"],

            "broker_net_20d":
                best["net_lots_20d"],

            "broker_net_ytd":
                best["net_lots_ytd"],

            "broker_avg_buy_20d":
                best["avg_buy_20d"]
        })

    return pd.DataFrame(rows)
