from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

import tracker

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("DB_PATH", "broker_tracker.db"))
if not DB_PATH.is_absolute():
    DB_PATH = BASE_DIR / DB_PATH

WATCHLIST_FILE = BASE_DIR / "watchlist.txt"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

TIMEZONE = ZoneInfo("Asia/Jakarta")
BACKFILL_SLEEP_SECONDS = float(os.getenv("ZAPI_SLEEP_SECONDS", "1.0"))
TOP_BROKERS = int(os.getenv("QUERY_TOP_BROKERS", "3"))

TICKER_RE = re.compile(r"^[A-Z0-9]{2,12}$")


# ============================================================
# BASIC HELPERS
# ============================================================

def db_connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA foreign_keys=ON;")
    return con


def ensure_schema() -> None:
    tracker.init_db(str(DB_PATH))

    with db_connect() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS telegram_processed_updates (
                update_id INTEGER PRIMARY KEY,
                chat_id TEXT NOT NULL,
                command_text TEXT,
                processed_at TEXT NOT NULL
                    DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        con.commit()

def claim_telegram_update(
    update_id: int | None,
    chat_id: str,
    command_text: str,
) -> bool:
    """
    Claim Telegram update_id secara atomik.

    True:
        update belum pernah diproses.

    False:
        update_id sudah pernah diproses,
        sehingga command harus diabaikan.
    """

    if update_id is None:
        # Manual/non-standard event tidak memakai
        # duplicate protection.
        return True

    with db_connect() as con:
        cursor = con.execute(
            """
            INSERT OR IGNORE INTO telegram_processed_updates (
                update_id,
                chat_id,
                command_text
            )
            VALUES (?, ?, ?)
            """,
            (
                int(update_id),
                str(chat_id),
                str(command_text),
            ),
        )

        con.commit()

        return cursor.rowcount == 1


def checkpoint_db() -> None:
    if not DB_PATH.exists():
        return
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    finally:
        con.close()


def normalize_ticker(value: str) -> str:
    ticker = value.strip().upper().replace(".JK", "")
    if not TICKER_RE.fullmatch(ticker):
        raise ValueError(
            "Ticker tidak valid. Gunakan kode saham seperti ADRO, BBRI, INET."
        )
    return ticker


def format_int(value: float | int | None, signed: bool = False) -> str:
    if value is None:
        return "-"
    x = int(round(float(value)))
    return f"{x:+,}" if signed else f"{x:,}"


def format_price(value: float | int | None) -> str:
    if value is None:
        return "-"
    return f"{float(value):,.0f}"


def format_rp(value: float | int | None, signed: bool = False) -> str:
    if value is None:
        return "-"
    x = float(value)
    sign = ""
    if signed:
        sign = "+" if x > 0 else "-" if x < 0 else ""
    ax = abs(x)
    if ax >= 1_000_000_000_000:
        return f"{sign}Rp {ax / 1_000_000_000_000:,.2f} T"
    if ax >= 1_000_000_000:
        return f"{sign}Rp {ax / 1_000_000_000:,.2f} M"
    if ax >= 1_000_000:
        return f"{sign}Rp {ax / 1_000_000:,.2f} Jt"
    if ax >= 1_000:
        return f"{sign}Rp {ax / 1_000:,.1f} Rb"
    return f"{sign}Rp {ax:,.0f}"


def split_message(text: str, max_chars: int = 3500) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in text.splitlines():
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > max_chars and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def send_telegram(text: str, chat_id: str | None = None) -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN belum tersedia.")

    target = str(chat_id or TELEGRAM_CHAT_ID).strip()
    if not target:
        raise RuntimeError("TELEGRAM_CHAT_ID belum tersedia.")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    for chunk in split_message(text):
        r = requests.post(
            url,
            json={
                "chat_id": target,
                "text": chunk,
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        r.raise_for_status()


def flow_label(net_lots: float, net_value: float) -> str:
    if net_lots > 0 and net_value > 0:
        return "ACCUMULATION CONFIRMED"
    if net_lots < 0 and net_value < 0:
        return "DISTRIBUTION CONFIRMED"
    if net_lots > 0 and net_value <= 0:
        return "MIXED — LOT BUY / VALUE SELL"
    if net_lots < 0 and net_value >= 0:
        return "MIXED — LOT SELL / VALUE BUY"
    return "NEUTRAL"


# ============================================================
# WATCHLIST
# ============================================================

def load_watchlist() -> list[str]:
    if not WATCHLIST_FILE.exists():
        return []

    rows: list[str] = []
    for line in WATCHLIST_FILE.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        try:
            rows.append(normalize_ticker(s))
        except ValueError:
            continue

    return list(dict.fromkeys(rows))


def add_watchlist(ticker: str) -> bool:
    ticker = normalize_ticker(ticker)
    current = load_watchlist()
    if ticker in current:
        return False

    if WATCHLIST_FILE.exists():
        original = WATCHLIST_FILE.read_text(encoding="utf-8")
    else:
        original = "# WATCHLIST\n"

    if original and not original.endswith("\n"):
        original += "\n"

    original += f"{ticker}\n"
    WATCHLIST_FILE.write_text(original, encoding="utf-8")
    return True


def remove_watchlist(ticker: str) -> bool:
    ticker = normalize_ticker(ticker)
    if not WATCHLIST_FILE.exists():
        return False

    lines = WATCHLIST_FILE.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    removed = False

    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and s.upper() == ticker:
            removed = True
            continue
        out.append(line)

    WATCHLIST_FILE.write_text(
        "\n".join(out).rstrip() + "\n",
        encoding="utf-8",
    )
    return removed


# ============================================================
# DATABASE STATUS / HISTORY
# ============================================================

def ticker_status(ticker: str) -> dict[str, Any]:
    ticker = normalize_ticker(ticker)

    with db_connect() as con:
        row = con.execute(
            """
            SELECT
                MIN(trade_date) AS first_date,
                MAX(trade_date) AS last_date,
                COUNT(DISTINCT trade_date) AS trading_days,
                COUNT(*) AS rows_count,
                COUNT(DISTINCT broker) AS broker_count,
                COALESCE(SUM(CASE WHEN side='BUY' THEN lots ELSE 0 END),0)
                    AS total_buy_lots,
                COALESCE(SUM(CASE WHEN side='SELL' THEN lots ELSE 0 END),0)
                    AS total_sell_lots,
                COALESCE(SUM(CASE WHEN side='BUY' THEN value ELSE 0 END),0)
                    AS total_buy_value,
                COALESCE(SUM(CASE WHEN side='SELL' THEN value ELSE 0 END),0)
                    AS total_sell_value
            FROM broker_daily
            WHERE ticker = ?
            """,
            (ticker,),
        ).fetchone()

    result = dict(row) if row else {}
    result["ticker"] = ticker
    return result


def available_trade_dates(
    ticker: str,
    end_date: str | None = None,
) -> list[str]:
    ticker = normalize_ticker(ticker)

    sql = """
        SELECT DISTINCT trade_date
        FROM broker_daily
        WHERE ticker = ?
    """
    params: list[Any] = [ticker]

    if end_date:
        sql += " AND trade_date <= ?"
        params.append(end_date)

    sql += " ORDER BY trade_date"

    with db_connect() as con:
        rows = con.execute(sql, params).fetchall()

    return [str(r[0]) for r in rows]


def reference_trade_dates(year: int, end_date: str) -> list[str]:
    with db_connect() as con:
        rows = con.execute(
            """
            SELECT trade_date
            FROM broker_daily
            WHERE trade_date BETWEEN ? AND ?
            GROUP BY trade_date
            HAVING COUNT(DISTINCT ticker) >= 2
            ORDER BY trade_date
            """,
            (f"{year}-01-01", end_date),
        ).fetchall()
    return [str(r[0]) for r in rows]


def safe_end_date() -> str:
    now = datetime.now(TIMEZONE)
    d = now.date()

    # Sebelum market selesai, gunakan hari kerja sebelumnya.
    if d.weekday() < 5 and now.hour < 18:
        d -= timedelta(days=1)

    while d.weekday() >= 5:
        d -= timedelta(days=1)

    return d.isoformat()


def validate_ticker_with_api(ticker: str) -> tuple[bool, str]:
    ticker = normalize_ticker(ticker)
    try:
        payload = tracker.fetch_quote(ticker)
        if not payload:
            return False, "respons quote kosong"
        return True, "OK"
    except Exception as exc:
        return False, str(exc)


def update_ticker_history(
    ticker: str,
    *,
    full_ytd_if_new: bool = True,
    repair_gaps: bool = True,
) -> dict[str, Any]:
    """
    Smart update untuk satu ticker.
    - New ticker: backfill YTD.
    - Existing ticker: isi prefix/suffix yang belum ada.
    - Internal gap: isi hanya tanggal referensi yang hilang.
    """
    ticker = normalize_ticker(ticker)
    ensure_schema()

    end_date = safe_end_date()
    year = int(end_date[:4])
    year_start = f"{year}-01-01"

    before = ticker_status(ticker)
    first_date = before.get("first_date")
    last_date = before.get("last_date")

    requests_plan: list[tuple[str, str, str]] = []

    if not first_date and full_ytd_if_new:
        requests_plan.append(("FULL_YTD", year_start, end_date))
    else:
        if first_date:
            first_dt = date.fromisoformat(str(first_date))
            if first_dt > date(year, 1, 10):
                prefix_end = (first_dt - timedelta(days=1)).isoformat()
                requests_plan.append(("PREFIX", year_start, prefix_end))

        if last_date:
            next_dt = date.fromisoformat(str(last_date)) + timedelta(days=1)
            if next_dt <= date.fromisoformat(end_date):
                requests_plan.append(("SUFFIX", next_dt.isoformat(), end_date))
        elif not full_ytd_if_new:
            requests_plan.append(("LATEST", end_date, end_date))

    executed: list[dict[str, Any]] = []

    for kind, start, end in requests_plan:
        stats = tracker.collect_daily(
            ticker=ticker,
            start_date=start,
            end_date=end,
            db_path=str(DB_PATH),
            sleep_seconds=BACKFILL_SLEEP_SECONDS,
        )
        executed.append(
            {
                "kind": kind,
                "start": start,
                "end": end,
                "stats": stats,
            }
        )

    gap_before = 0
    gap_after = 0

    if repair_gaps:
        refs = reference_trade_dates(year, end_date)
        existing = set(available_trade_dates(ticker, end_date))
        missing = [d for d in refs if d not in existing]
        gap_before = len(missing)

        for ds in missing:
            tracker.collect_daily(
                ticker=ticker,
                start_date=ds,
                end_date=ds,
                db_path=str(DB_PATH),
                sleep_seconds=BACKFILL_SLEEP_SECONDS,
            )

        existing_after = set(available_trade_dates(ticker, end_date))
        gap_after = len([d for d in refs if d not in existing_after])

    checkpoint_db()
    after = ticker_status(ticker)

    return {
        "ticker": ticker,
        "safe_end_date": end_date,
        "before": before,
        "after": after,
        "executed": executed,
        "gap_before": gap_before,
        "gap_after": gap_after,
    }


# ============================================================
# BROKER SUMMARY ENGINE: LOT + VALUE
# ============================================================

@dataclass
class PeriodSummary:
    ticker: str
    label: str
    start_date: str
    end_date: str
    trading_days: int
    buy_lots: float
    sell_lots: float
    net_lots: float
    buy_value: float
    sell_value: float
    net_value: float
    avg_buy_price: float | None
    avg_sell_price: float | None
    lot_ratio: float
    value_ratio: float
    flow: str


def _period_range(
    ticker: str,
    period: str,
) -> tuple[str, str, int] | None:
    ticker = normalize_ticker(ticker)
    dates = available_trade_dates(ticker)

    if not dates:
        return None

    period = period.upper()

    if period == "YTD":
        end_date = dates[-1]
        year_start = f"{end_date[:4]}-01-01"
        ytd_dates = [d for d in dates if d >= year_start]
        if not ytd_dates:
            return None
        return ytd_dates[0], ytd_dates[-1], len(ytd_dates)

    match = re.fullmatch(r"(5|10|20)D", period)
    if not match:
        raise ValueError("Period hanya 5D, 10D, 20D, atau YTD.")

    n = int(match.group(1))
    selected = dates[-n:]
    return selected[0], selected[-1], len(selected)


def aggregate_period(
    ticker: str,
    period: str,
) -> tuple[PeriodSummary | None, list[dict[str, Any]]]:
    ticker = normalize_ticker(ticker)
    range_info = _period_range(ticker, period)

    if not range_info:
        return None, []

    start_date, end_date, trading_days = range_info

    with db_connect() as con:
        rows = con.execute(
            """
            SELECT
                broker,
                COALESCE(SUM(CASE WHEN side='BUY' THEN lots ELSE 0 END), 0)
                    AS buy_lots,
                COALESCE(SUM(CASE WHEN side='SELL' THEN lots ELSE 0 END), 0)
                    AS sell_lots,
                COALESCE(SUM(CASE WHEN side='BUY' THEN value ELSE 0 END), 0)
                    AS buy_value,
                COALESCE(SUM(CASE WHEN side='SELL' THEN value ELSE 0 END), 0)
                    AS sell_value
            FROM broker_daily
            WHERE ticker = ?
              AND trade_date BETWEEN ? AND ?
            GROUP BY broker
            ORDER BY broker
            """,
            (ticker, start_date, end_date),
        ).fetchall()

    if not rows:
        return None, []

    broker_rows: list[dict[str, Any]] = []
    for r in rows:
        item = dict(r)
        item["net_lots"] = float(item["buy_lots"]) - float(item["sell_lots"])
        item["net_value"] = float(item["buy_value"]) - float(item["sell_value"])
        broker_rows.append(item)

    buy_lots = sum(float(r["buy_lots"]) for r in broker_rows)
    sell_lots = sum(float(r["sell_lots"]) for r in broker_rows)
    buy_value = sum(float(r["buy_value"]) for r in broker_rows)
    sell_value = sum(float(r["sell_value"]) for r in broker_rows)

    net_lots = buy_lots - sell_lots
    net_value = buy_value - sell_value

    avg_buy = buy_value / (buy_lots * 100) if buy_lots else None
    avg_sell = sell_value / (sell_lots * 100) if sell_lots else None

    lot_denom = buy_lots + sell_lots
    value_denom = buy_value + sell_value

    summary = PeriodSummary(
        ticker=ticker,
        label=period.upper(),
        start_date=start_date,
        end_date=end_date,
        trading_days=trading_days,
        buy_lots=buy_lots,
        sell_lots=sell_lots,
        net_lots=net_lots,
        buy_value=buy_value,
        sell_value=sell_value,
        net_value=net_value,
        avg_buy_price=avg_buy,
        avg_sell_price=avg_sell,
        lot_ratio=(net_lots / lot_denom if lot_denom else 0.0),
        value_ratio=(net_value / value_denom if value_denom else 0.0),
        flow=flow_label(net_lots, net_value),
    )

    return summary, broker_rows


def get_latest_price(
    ticker: str,
) -> tuple[float | None, str]:
    ticker = normalize_ticker(ticker)

    # ========================================================
    # 1. PRIORITAS: FRESH LAST PRICE DARI ZAPI SUMMARY
    # ========================================================
    try:
        payload = tracker.fetch_multi_quote([ticker])

        if isinstance(payload, dict):
            items = payload.get("items", [])

            if items:
                item = items[0]

                raw_price = item.get("lastPrice")

                if raw_price is not None:
                    price = float(
                        str(raw_price)
                        .replace(",", "")
                        .strip()
                    )

                    if price > 0:
                        return price, "ZAPI"

    except Exception as exc:
        print(
            f"[PRICE] Fresh price {ticker} gagal: {exc}"
        )

    # ========================================================
    # 2. FALLBACK: CACHE SQLITE
    # ========================================================
    try:
        with db_connect() as con:
            row = con.execute(
                """
                SELECT last_price
                FROM scan_results
                WHERE ticker = ?
                  AND last_price IS NOT NULL
                ORDER BY scan_time DESC
                LIMIT 1
                """,
                (ticker,),
            ).fetchone()

        if row and row[0] is not None:
            return float(row[0]), "CACHE"

    except sqlite3.Error as exc:
        print(
            f"[PRICE] Cache price {ticker} gagal: {exc}"
        )

    return None, "NONE"

def latest_daily_broker_data(
    ticker: str,
) -> tuple[
    PeriodSummary | None,
    list[dict[str, Any]],
]:
    ticker = normalize_ticker(ticker)

    dates = available_trade_dates(ticker)

    if not dates:
        return None, []

    last_date = dates[-1]

    with db_connect() as con:
        rows = con.execute(
            """
            SELECT
                broker,

                COALESCE(
                    SUM(
                        CASE
                            WHEN side='BUY'
                            THEN lots
                            ELSE 0
                        END
                    ),
                    0
                ) AS buy_lots,

                COALESCE(
                    SUM(
                        CASE
                            WHEN side='SELL'
                            THEN lots
                            ELSE 0
                        END
                    ),
                    0
                ) AS sell_lots,

                COALESCE(
                    SUM(
                        CASE
                            WHEN side='BUY'
                            THEN value
                            ELSE 0
                        END
                    ),
                    0
                ) AS buy_value,

                COALESCE(
                    SUM(
                        CASE
                            WHEN side='SELL'
                            THEN value
                            ELSE 0
                        END
                    ),
                    0
                ) AS sell_value

            FROM broker_daily

            WHERE ticker = ?
              AND trade_date = ?

            GROUP BY broker
            """,
            (
                ticker,
                last_date,
            ),
        ).fetchall()

    if not rows:
        return None, []

    broker_rows: list[dict[str, Any]] = []

    for row in rows:
        item = dict(row)

        item["buy_lots"] = float(
            item.get("buy_lots", 0) or 0
        )

        item["sell_lots"] = float(
            item.get("sell_lots", 0) or 0
        )

        item["buy_value"] = float(
            item.get("buy_value", 0) or 0
        )

        item["sell_value"] = float(
            item.get("sell_value", 0) or 0
        )

        item["net_lots"] = (
            item["buy_lots"]
            - item["sell_lots"]
        )

        item["net_value"] = (
            item["buy_value"]
            - item["sell_value"]
        )

        broker_rows.append(item)

    buy_lots = sum(
        row["buy_lots"]
        for row in broker_rows
    )

    sell_lots = sum(
        row["sell_lots"]
        for row in broker_rows
    )

    buy_value = sum(
        row["buy_value"]
        for row in broker_rows
    )

    sell_value = sum(
        row["sell_value"]
        for row in broker_rows
    )

    net_lots = buy_lots - sell_lots
    net_value = buy_value - sell_value

    summary = PeriodSummary(
        ticker=ticker,
        label="LATEST",
        start_date=last_date,
        end_date=last_date,
        trading_days=1,

        buy_lots=buy_lots,
        sell_lots=sell_lots,
        net_lots=net_lots,

        buy_value=buy_value,
        sell_value=sell_value,
        net_value=net_value,

        avg_buy_price=(
            buy_value / (buy_lots * 100)
            if buy_lots
            else None
        ),

        avg_sell_price=(
            sell_value / (sell_lots * 100)
            if sell_lots
            else None
        ),

        lot_ratio=(
            net_lots
            / (buy_lots + sell_lots)
            if (buy_lots + sell_lots)
            else 0.0
        ),

        value_ratio=(
            net_value
            / (buy_value + sell_value)
            if (buy_value + sell_value)
            else 0.0
        ),

        flow=flow_label(
            net_lots,
            net_value,
        ),
    )

    return summary, broker_rows


def latest_daily_summary(
    ticker: str,
) -> PeriodSummary | None:

    summary, _ = latest_daily_broker_data(
        ticker
    )

    return summary


def top_brokers(
    broker_rows: list[dict[str, Any]],
    *,
    field: str = "net_value",
    n: int = 3,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = sorted(
        broker_rows,
        key=lambda x: float(x.get(field, 0) or 0),
        reverse=True,
    )
    buyers = [r for r in rows if float(r.get(field, 0) or 0) > 0][:n]
    sellers = [
        r for r in reversed(rows)
        if float(r.get(field, 0) or 0) < 0
    ][:n]
    return buyers, sellers


# ============================================================
# FORMATTERS
# ============================================================

def format_period(summary: PeriodSummary) -> str:
    return "\n".join(
        [
            f"{summary.label} | {summary.start_date} → {summary.end_date}",
            f"> Net Lot: {format_int(summary.net_lots, signed=True)} lot",
            (
                f"> Buy/Sell Lot: {format_int(summary.buy_lots)} / "
                f"{format_int(summary.sell_lots)}"
            ),
            f"> Net Value: {format_rp(summary.net_value, signed=True)}",
            (
                f"> Buy/Sell Value: {format_rp(summary.buy_value)} / "
                f"{format_rp(summary.sell_value)}"
            ),
            (
                f"> Avg Buy/Sell: {format_price(summary.avg_buy_price)} / "
                f"{format_price(summary.avg_sell_price)}"
            ),
            (
                f"> Lot Ratio: {summary.lot_ratio:+.2%} | "
                f"Value Ratio: {summary.value_ratio:+.2%}"
            ),
            f"> Flow: {summary.flow}",
        ]
    )


def format_full_report(ticker: str) -> str:
    ticker = normalize_ticker(ticker)
    status = ticker_status(ticker)

    if not status.get("first_date"):
        return (
            f"❌ {ticker} belum memiliki data di SQLite.\n"
            f"Ketik /add {ticker} untuk menambahkan + backfill YTD."
        )

    price, price_source = get_latest_price(ticker)

    lines = [
        f"📊 BROKER SUMMARY — {ticker}",
        (
            f"Data: {status.get('first_date')} → "
            f"{status.get('last_date')}"
        ),
        (
            f"Trading Days: {status.get('trading_days', 0)} | "
            f"Broker: {status.get('broker_count', 0)}"
        ),
    ]

    if price is not None:
        if price_source == "ZAPI":
            lines.append(
                f"Latest Price: {format_price(price)}"
            )
        else:
            lines.append(
                f"Cached Last Price (fallback): "
                f"{format_price(price)}"
            )

    lines.append("")

    period_data: dict[str, tuple[PeriodSummary | None, list[dict[str, Any]]]] = {}

    for period in ("5D", "10D", "20D", "YTD"):
        summary, brokers = aggregate_period(ticker, period)
        period_data[period] = (summary, brokers)
        if summary:
            lines.append(format_period(summary))
            lines.append("")

        latest, latest_brokers = (
        latest_daily_broker_data(ticker)
    )

    if latest:
        lines.extend(
            [
                "📊 LATEST BROKER ACTION",
                f"> Date: {latest.end_date}",
                (
                    f"> Net Lot: "
                    f"{format_int(
                        latest.net_lots,
                        signed=True,
                    )} lot"
                ),
                (
                    f"> Net Value: "
                    f"{format_rp(
                        latest.net_value,
                        signed=True,
                    )}"
                ),
                f"> Flow: {latest.flow}",
            ]
        )

        buyers, sellers = top_brokers(
            latest_brokers,
            field="net_value",
            n=3,
        )

        flow_upper = str(
            latest.flow
        ).upper()

        # ================================================
        # ACCUMULATION
        # ================================================

        if "ACCUMULATION" in flow_upper:

            lines.extend(
                [
                    "",
                    "🟢 DOMINANT BUYER",
                ]
            )

            for idx, row in enumerate(
                buyers,
                start=1,
            ):
                avg_buy = _broker_avg_buy(
                    row
                )

                lines.append(
                    (
                        f"{idx}. "
                        f"{row.get('broker', '-')}"
                        f" | "
                        f"{format_rp(
                            row.get('net_value', 0),
                            signed=True,
                        )}"
                        f" | "
                        f"{format_int(
                            row.get('net_lots', 0),
                            signed=True,
                        )} lot"
                        f" | Avg Buy "
                        f"{format_price(avg_buy)}"
                    )
                )

        # ================================================
        # DISTRIBUTION
        # ================================================

        elif "DISTRIBUTION" in flow_upper:

            lines.extend(
                [
                    "",
                    "🔴 DOMINANT SELLER",
                ]
            )

            for idx, row in enumerate(
                sellers,
                start=1,
            ):
                avg_sell = _broker_avg_sell(
                    row
                )

                lines.append(
                    (
                        f"{idx}. "
                        f"{row.get('broker', '-')}"
                        f" | "
                        f"{format_rp(
                            row.get('net_value', 0),
                            signed=True,
                        )}"
                        f" | "
                        f"{format_int(
                            row.get('net_lots', 0),
                            signed=True,
                        )} lot"
                        f" | Avg Sell "
                        f"{format_price(avg_sell)}"
                    )
                )

        # ================================================
        # MIXED / NEUTRAL
        # ================================================

        else:

            if buyers:
                lines.extend(
                    [
                        "",
                        "🟢 TOP BUYER",
                    ]
                )

                for idx, row in enumerate(
                    buyers,
                    start=1,
                ):
                    avg_buy = _broker_avg_buy(
                        row
                    )

                    lines.append(
                        (
                            f"{idx}. "
                            f"{row.get('broker', '-')}"
                            f" | "
                            f"{format_rp(
                                row.get('net_value', 0),
                                signed=True,
                            )}"
                            f" | Avg Buy "
                            f"{format_price(avg_buy)}"
                        )
                    )

            if sellers:
                lines.extend(
                    [
                        "",
                        "🔴 TOP SELLER",
                    ]
                )

                for idx, row in enumerate(
                    sellers,
                    start=1,
                ):
                    avg_sell = _broker_avg_sell(
                        row
                    )

                    lines.append(
                        (
                            f"{idx}. "
                            f"{row.get('broker', '-')}"
                            f" | "
                            f"{format_rp(
                                row.get('net_value', 0),
                                signed=True,
                            )}"
                            f" | Avg Sell "
                            f"{format_price(avg_sell)}"
                        )
                    )

        lines.append("")

    summary20, brokers20 = period_data.get("20D", (None, []))
    if summary20 and brokers20:
        buyers, sellers = top_brokers(
            brokers20,
            field="net_value",
            n=TOP_BROKERS,
        )

        lines.append("20D TOP BROKER — NET VALUE")

        if buyers:
            lines.append("Net Buy:")
            for r in buyers:
                lines.append(
                    f"> {r['broker']}: "
                    f"{format_rp(r['net_value'], signed=True)} | "
                    f"{format_int(r['net_lots'], signed=True)} lot"
                )

        if sellers:
            lines.append("Net Sell:")
            for r in sellers:
                lines.append(
                    f"> {r['broker']}: "
                    f"{format_rp(r['net_value'], signed=True)} | "
                    f"{format_int(r['net_lots'], signed=True)} lot"
                )

    return "\n".join(lines).strip()


def format_single_period(ticker: str, period: str) -> str:
    ticker = normalize_ticker(ticker)
    summary, brokers = aggregate_period(ticker, period)
    if not summary:
        return f"❌ Tidak ada data {period.upper()} untuk {ticker}."

    buyers, sellers = top_brokers(
        brokers,
        field="net_value",
        n=TOP_BROKERS,
    )

    lines = [
        f"📊 {ticker} — {period.upper()}",
        format_period(summary),
        "",
        "TOP NET VALUE BROKER",
    ]

    if buyers:
        lines.append("Net Buy:")
        for r in buyers:
            lines.append(
                f"> {r['broker']}: {format_rp(r['net_value'], signed=True)} "
                f"| {format_int(r['net_lots'], signed=True)} lot"
            )

    if sellers:
        lines.append("Net Sell:")
        for r in sellers:
            lines.append(
                f"> {r['broker']}: {format_rp(r['net_value'], signed=True)} "
                f"| {format_int(r['net_lots'], signed=True)} lot"
            )

    return "\n".join(lines)


def _broker_avg_buy(row: dict[str, Any]) -> float | None:
    buy_lots = float(row.get("buy_lots", 0) or 0)
    buy_value = float(row.get("buy_value", 0) or 0)

    if buy_lots <= 0:
        return None

    return buy_value / (buy_lots * 100)


def _broker_avg_sell(row: dict[str, Any]) -> float | None:
    sell_lots = float(row.get("sell_lots", 0) or 0)
    sell_value = float(row.get("sell_value", 0) or 0)

    if sell_lots <= 0:
        return None

    return sell_value / (sell_lots * 100)


def _price_vs_avg_pct(
    last_price: float | None,
    avg_price: float | None,
) -> float | None:
    if (
        last_price is None
        or avg_price is None
        or avg_price == 0
    ):
        return None

    return ((last_price / avg_price) - 1) * 100

def format_daily_broker_report(
    ticker: str,
) -> str:
    ticker = normalize_ticker(ticker)

    summary, brokers = latest_daily_broker_data(
        ticker
    )

    if not summary or not brokers:
        return (
            f"❌ Tidak ada data broker harian "
            f"untuk {ticker}."
        )

    last_price, price_source = get_latest_price(
        ticker
    )

    buyers, sellers = top_brokers(
        brokers,
        field="net_value",
        n=5,
    )

    lines = [
        f"📅 DAILY BROKER SUMMARY — {ticker}",
        f"Date: {summary.end_date}",
    ]

    if last_price is not None:
        if price_source == "ZAPI":
            lines.append(
                f"Latest Price: {format_price(last_price)}"
            )
        else:
            lines.append(
                f"Cached Price (fallback): "
                f"{format_price(last_price)}"
            )

    lines.extend(
        [
            "",
            f"Flow: {summary.flow}",
            "",
            "TOTAL FLOW",
            f"> Buy Value: {format_rp(summary.buy_value)}",
            f"> Sell Value: {format_rp(summary.sell_value)}",
            (
                f"> Net Value: "
                f"{format_rp(summary.net_value, signed=True)}"
            ),
            (
                f"> Buy/Sell Lot: "
                f"{format_int(summary.buy_lots)} / "
                f"{format_int(summary.sell_lots)}"
            ),
            (
                f"> Net Lot: "
                f"{format_int(summary.net_lots, signed=True)} lot"
            ),
            (
                f"> Avg Buy/Sell: "
                f"{format_price(summary.avg_buy_price)} / "
                f"{format_price(summary.avg_sell_price)}"
            ),
        ]
    )

    if buyers:
        lines.extend(
            [
                "",
                "🟢 TOP NET BUY",
            ]
        )

        for idx, row in enumerate(
            buyers,
            start=1,
        ):
            avg_buy = _broker_avg_buy(row)

            lines.append(
                (
                    f"{idx}. {row.get('broker', '-')}"
                    f" | "
                    f"{format_rp(
                        row.get('net_value', 0),
                        signed=True,
                    )}"
                    f" | "
                    f"{format_int(
                        row.get('net_lots', 0),
                        signed=True,
                    )} lot"
                    f" | Avg Buy "
                    f"{format_price(avg_buy)}"
                )
            )

    if sellers:
        lines.extend(
            [
                "",
                "🔴 TOP NET SELL",
            ]
        )

        for idx, row in enumerate(
            sellers,
            start=1,
        ):
            avg_sell = _broker_avg_sell(row)

            lines.append(
                (
                    f"{idx}. {row.get('broker', '-')}"
                    f" | "
                    f"{format_rp(
                        row.get('net_value', 0),
                        signed=True,
                    )}"
                    f" | "
                    f"{format_int(
                        row.get('net_lots', 0),
                        signed=True,
                    )} lot"
                    f" | Avg Sell "
                    f"{format_price(avg_sell)}"
                )
            )

    return "\n".join(lines)

def format_broker_code_report(
    ticker: str,
    broker_code: str,
    period: str = "20D",
) -> str:
    ticker = normalize_ticker(ticker)
    broker_code = broker_code.strip().upper()
    period = period.strip().upper()

    if period not in {"5D", "10D", "20D", "YTD"}:
        return (
            "❌ Period tidak valid.\n"
            "Gunakan: 5D, 10D, 20D, atau YTD."
        )

    summary, brokers = aggregate_period(
        ticker,
        period,
    )

    if not summary:
        return (
            f"❌ Tidak ada data {ticker} "
            f"untuk periode {period}."
        )

    broker = next(
        (
            row
            for row in brokers
            if str(row.get("broker", "")).upper()
            == broker_code
        ),
        None,
    )

    if not broker:
        return (
            f"❌ Broker {broker_code} tidak memiliki "
            f"data pada {ticker} {period}."
        )

    buy_lots = float(
        broker.get("buy_lots", 0) or 0
    )
    sell_lots = float(
        broker.get("sell_lots", 0) or 0
    )
    net_lots = float(
        broker.get("net_lots", 0) or 0
    )

    buy_value = float(
        broker.get("buy_value", 0) or 0
    )
    sell_value = float(
        broker.get("sell_value", 0) or 0
    )
    net_value = float(
        broker.get("net_value", 0) or 0
    )

    avg_buy = _broker_avg_buy(broker)
    avg_sell = _broker_avg_sell(broker)

    last_price, price_source = get_latest_price(
        ticker
    )

    price_vs_buy = _price_vs_avg_pct(
        last_price,
        avg_buy,
    )

    price_vs_sell = _price_vs_avg_pct(
        last_price,
        avg_sell,
    )

    if net_value > 0:
        action = "NET BUY"
    elif net_value < 0:
        action = "NET SELL"
    else:
        action = "NEUTRAL"

    lines = [
        f"🏦 BROKER {broker_code} — {ticker} {period}",
        f"Period: {summary.start_date} → {summary.end_date}",
        f"Action: {action}",
        "",
        f"> Buy Value: {format_rp(buy_value)}",
        f"> Sell Value: {format_rp(sell_value)}",
        f"> Net Value: {format_rp(net_value, signed=True)}",
        "",
        f"> Buy Lot: {format_int(buy_lots)} lot",
        f"> Sell Lot: {format_int(sell_lots)} lot",
        f"> Net Lot: {format_int(net_lots, signed=True)} lot",
        "",
        f"> Weighted Avg Buy: {format_price(avg_buy)}",
        f"> Weighted Avg Sell: {format_price(avg_sell)}",
    ]

    if last_price is not None:
        if price_source == "ZAPI":
            lines.append(
                f"> Latest Price: {format_price(last_price)}"
            )
        else:
            lines.append(
                f"> Cached Price (fallback): "
                f"{format_price(last_price)}"
            )

    if price_vs_buy is not None:
        lines.append(
            f"> Price vs Avg Buy: {price_vs_buy:+.2f}%"
        )

    if price_vs_sell is not None:
        lines.append(
            f"> Price vs Avg Sell: {price_vs_sell:+.2f}%"
        )

    return "\n".join(lines)

def _dominant_broker_by_value(
    brokers: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not brokers:
        return None

    return max(
        brokers,
        key=lambda row: abs(
            float(row.get("net_value", 0) or 0)
        ),
    )


def format_value_report(ticker: str) -> str:
    ticker = normalize_ticker(ticker)
    last_price, price_source = get_latest_price(ticker)

    lines = [
        f"💰 VALUE FLOW — {ticker}",
    ]

    if last_price is not None:
        if price_source == "ZAPI":
            lines.append(
                f"Latest Price: {format_price(last_price)}"
            )
        else:
            lines.append(
                f"Cached Last Price (fallback): "
                f"{format_price(last_price)}"
            )

    lines.append("")

    found = False

    for period in ("5D", "10D", "20D", "YTD"):
        summary, brokers = aggregate_period(ticker, period)

        if not summary:
            continue

        found = True

        lines.extend(
            [
                f"{period} | {summary.start_date} → {summary.end_date}",
                f"> Buy Value: {format_rp(summary.buy_value)}",
                f"> Sell Value: {format_rp(summary.sell_value)}",
                f"> Net Value: {format_rp(summary.net_value, signed=True)}",
                f"> Value Ratio: {summary.value_ratio:+.2%}",
                f"> Net Lot: {format_int(summary.net_lots, signed=True)} lot",
                (
                    f"> Overall Avg Buy/Sell: "
                    f"{format_price(summary.avg_buy_price)} / "
                    f"{format_price(summary.avg_sell_price)}"
                ),
                f"> Flow: {summary.flow}",
            ]
        )

        dominant = _dominant_broker_by_value(brokers)

        if dominant:
            dominant_net_value = float(
                dominant.get("net_value", 0) or 0
            )
            dominant_net_lots = float(
                dominant.get("net_lots", 0) or 0
            )

            dominant_side = (
                "NET BUY"
                if dominant_net_value > 0
                else "NET SELL"
                if dominant_net_value < 0
                else "NEUTRAL"
            )

            dominant_avg_buy = _broker_avg_buy(dominant)
            dominant_avg_sell = _broker_avg_sell(dominant)

            price_vs_dominant = _price_vs_avg_pct(
                last_price,
                dominant_avg_buy,
            )

            lines.extend(
                [
                    "",
                    "🏦 DOMINANT CC — NET VALUE",
                    (
                        f"> {dominant.get('broker', '-')}"
                        f" | {dominant_side}"
                    ),
                    (
                        f"> Net Value: "
                        f"{format_rp(dominant_net_value, signed=True)}"
                    ),
                    (
                        f"> Net Lot: "
                        f"{format_int(dominant_net_lots, signed=True)} lot"
                    ),
                    (
                        f"> Avg Buy/Sell: "
                        f"{format_price(dominant_avg_buy)} / "
                        f"{format_price(dominant_avg_sell)}"
                    ),
                ]
            )

            if price_vs_dominant is not None:
                lines.append(
                    f"> Price vs CC Avg Buy: "
                    f"{price_vs_dominant:+.2f}%"
                )

        top_buyers, _ = top_brokers(
            brokers,
            field="net_value",
            n=3,
        )

        if top_buyers:
            lines.extend(
                [
                    "",
                    "TOP 3 NET BUY CC — VALUE",
                ]
            )

            for idx, row in enumerate(top_buyers, start=1):
                avg_buy = _broker_avg_buy(row)
                avg_sell = _broker_avg_sell(row)
                price_vs_avg = _price_vs_avg_pct(
                    last_price,
                    avg_buy,
                )

                lines.append(
                    (
                        f"{idx}. {row.get('broker', '-')}"
                        f" | {format_rp(row.get('net_value', 0), signed=True)}"
                        f" | {format_int(row.get('net_lots', 0), signed=True)} lot"
                    )
                )

                lines.append(
                    (
                        f"   Avg Buy/Sell: "
                        f"{format_price(avg_buy)} / "
                        f"{format_price(avg_sell)}"
                    )
                )

                if price_vs_avg is not None:
                    lines.append(
                        f"   Price vs Avg Buy: {price_vs_avg:+.2f}%"
                    )

        lines.append("")

    if not found:
        return f"❌ Tidak ada data value untuk {ticker}."

    return "\n".join(lines).strip()


def format_broker_report(
    ticker: str,
    period: str = "20D",
) -> str:

    ticker = normalize_ticker(ticker)
    period = period.upper()

    summary, brokers = aggregate_period(
        ticker,
        period,
    )

    if not summary:
        return (
            f"❌ Tidak ada broker data "
            f"{period} untuk {ticker}."
        )

    last_price, price_source = (
        get_latest_price(ticker)
    )

    buyers, sellers = top_brokers(
        brokers,
        field="net_value",
        n=5,
    )

    total_positive_value = sum(
        float(row.get("net_value", 0) or 0)
        for row in brokers
        if float(
            row.get("net_value", 0) or 0
        ) > 0
    )

    total_negative_value = sum(
        abs(
            float(
                row.get("net_value", 0) or 0
            )
        )
        for row in brokers
        if float(
            row.get("net_value", 0) or 0
        ) < 0
    )

    lines = [
        (
            f"🏦 BROKER DOMINANCE — "
            f"{ticker} {period}"
        ),
        (
            f"Period: "
            f"{summary.start_date} "
            f"→ {summary.end_date}"
        ),
        f"Flow: {summary.flow}",
    ]

    if last_price is not None:

        if price_source == "ZAPI":
            lines.append(
                f"Latest Price: "
                f"{format_price(last_price)}"
            )

        else:
            lines.append(
                f"Cached Price (fallback): "
                f"{format_price(last_price)}"
            )

    # ====================================================
    # TOP NET BUY
    # ====================================================

    if buyers:

        lines.extend(
            [
                "",
                "🟢 TOP NET BUY",
            ]
        )

        for idx, row in enumerate(
            buyers,
            start=1,
        ):

            net_value = float(
                row.get("net_value", 0) or 0
            )

            net_lots = float(
                row.get("net_lots", 0) or 0
            )

            avg_buy = _broker_avg_buy(
                row
            )

            contribution = (
                net_value
                / total_positive_value
                * 100
                if total_positive_value
                else 0.0
            )

            price_vs_avg = (
                _price_vs_avg_pct(
                    last_price,
                    avg_buy,
                )
            )

            lines.append(
                (
                    f"{idx}. "
                    f"{row.get('broker', '-')}"
                )
            )

            lines.append(
                (
                    f"   Net Value: "
                    f"{format_rp(
                        net_value,
                        signed=True,
                    )}"
                )
            )

            lines.append(
                (
                    f"   Net Lot: "
                    f"{format_int(
                        net_lots,
                        signed=True,
                    )} lot"
                )
            )

            lines.append(
                (
                    f"   Avg Buy: "
                    f"{format_price(avg_buy)}"
                )
            )

            lines.append(
                (
                    f"   Contribution: "
                    f"{contribution:.2f}%"
                )
            )

            if price_vs_avg is not None:
                lines.append(
                    (
                        f"   Price vs Avg Buy: "
                        f"{price_vs_avg:+.2f}%"
                    )
                )

    # ====================================================
    # TOP NET SELL
    # ====================================================

    if sellers:

        lines.extend(
            [
                "",
                "🔴 TOP NET SELL",
            ]
        )

        for idx, row in enumerate(
            sellers,
            start=1,
        ):

            net_value = float(
                row.get("net_value", 0) or 0
            )

            net_lots = float(
                row.get("net_lots", 0) or 0
            )

            avg_sell = _broker_avg_sell(
                row
            )

            contribution = (
                abs(net_value)
                / total_negative_value
                * 100
                if total_negative_value
                else 0.0
            )

            price_vs_avg = (
                _price_vs_avg_pct(
                    last_price,
                    avg_sell,
                )
            )

            lines.append(
                (
                    f"{idx}. "
                    f"{row.get('broker', '-')}"
                )
            )

            lines.append(
                (
                    f"   Net Value: "
                    f"{format_rp(
                        net_value,
                        signed=True,
                    )}"
                )
            )

            lines.append(
                (
                    f"   Net Lot: "
                    f"{format_int(
                        net_lots,
                        signed=True,
                    )} lot"
                )
            )

            lines.append(
                (
                    f"   Avg Sell: "
                    f"{format_price(avg_sell)}"
                )
            )

            lines.append(
                (
                    f"   Contribution: "
                    f"{contribution:.2f}%"
                )
            )

            if price_vs_avg is not None:
                lines.append(
                    (
                        f"   Price vs Avg Sell: "
                        f"{price_vs_avg:+.2f}%"
                    )
                )

    return "\n".join(lines)


def format_status(ticker: str) -> str:
    ticker = normalize_ticker(ticker)
    s = ticker_status(ticker)

    if not s.get("first_date"):
        return (
            f"ℹ️ STATUS {ticker}\n"
            "> Belum ada data SQLite.\n"
            f"> Gunakan /add {ticker}"
        )

    end_date = safe_end_date()
    refs = reference_trade_dates(int(end_date[:4]), end_date)
    existing = set(available_trade_dates(ticker, end_date))
    missing = [d for d in refs if d not in existing]

    return "\n".join(
        [
            f"ℹ️ STATUS — {ticker}",
            f"> First: {s.get('first_date')}",
            f"> Last: {s.get('last_date')}",
            f"> Trading Days: {s.get('trading_days', 0)}",
            f"> Broker Count: {s.get('broker_count', 0)}",
            f"> Rows: {s.get('rows_count', 0)}",
            f"> Internal Gap: {len(missing)}",
            (
                f"> Total Net Lot: "
                f"{format_int(
                    float(s.get('total_buy_lots') or 0)
                    - float(s.get('total_sell_lots') or 0),
                    signed=True,
                )}"
            ),
            (
                f"> Total Net Value: "
                f"{format_rp(
                    float(s.get('total_buy_value') or 0)
                    - float(s.get('total_sell_value') or 0),
                    signed=True,
                )}"
            ),
        ]
    )


HELP_TEXT = """🤖 BROKER QUERY BOT

Query:
ADRO
→ Full 5D/10D/20D/YTD + Lot + Value

ADRO TODAY
→ Broker summary khusus 1 hari perdagangan terakhir

ADRO 5D
ADRO 10D
ADRO 20D
ADRO YTD
→ Detail per period

ADRO VALUE
→ Buy/Sell/Net Value 5D/10D/20D/YTD

ADRO BROKER
→ Dominant broker 20D berdasarkan Value + Lot

ADRO CC NI
→ Detail broker NI, default 20D

ADRO CC NI 5D
ADRO CC NI 10D
ADRO CC NI 20D
ADRO CC NI YTD
→ AVG Buy/Sell + Net Value/Lot broker tertentu

Commands:
/add BBRI
→ Tambah watchlist + smart YTD backfill

/remove BBRI
→ Hapus dari watchlist; histori SQLite tetap disimpan

/list
→ Lihat watchlist aktif

/status BBRI
→ Audit histori SQLite

/refresh BBRI
→ Update data terbaru + repair gap satu ticker

/refresh ALL
→ Update seluruh ticker di watchlist + repair gap

/help
→ Bantuan
"""


# ============================================================
# TELEGRAM COMMAND PROCESSOR
# ============================================================

def parse_update_from_event(path: str) -> dict[str, Any]:
    event = json.loads(Path(path).read_text(encoding="utf-8"))

    if event.get("action") == "repository_dispatch":
        # defensive; standard GITHUB_EVENT_PATH does not always include action
        pass

    client_payload = event.get("client_payload", {})
    update = client_payload.get("telegram_update")

    if not isinstance(update, dict):
        raise ValueError("telegram_update tidak ditemukan pada client_payload.")

    return update


def extract_message(update: dict[str, Any]) -> tuple[str, str, int | None]:
    message = update.get("message") or update.get("edited_message")
    if not isinstance(message, dict):
        raise ValueError("Update Telegram bukan text message.")

    text = str(message.get("text") or "").strip()
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    update_id = update.get("update_id")

    return text, chat_id, update_id


def authorized(chat_id: str) -> bool:
    if not TELEGRAM_CHAT_ID:
        return False
    return str(chat_id) == str(TELEGRAM_CHAT_ID)

def refresh_all_watchlist(
    chat_id: str,
) -> str:
    tickers = load_watchlist()

    if not tickers:
        return "📋 Watchlist kosong."

    total = len(tickers)
    target_date = safe_end_date()

    success: list[dict[str, Any]] = []
    partial: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    send_telegram(
        (
            "⏳ REFRESH ALL DIMULAI\n"
            f"> Total Ticker: {total}\n"
            f"> Target Date: {target_date}"
        ),
        chat_id,
    )

    for idx, ticker in enumerate(
        tickers,
        start=1,
    ):
        try:
            send_telegram(
                (
                    f"⏳ [{idx}/{total}] "
                    f"Refresh {ticker}..."
                ),
                chat_id,
            )

            result = update_ticker_history(
                ticker,
                full_ytd_if_new=False,
                repair_gaps=True,
            )

            after = result.get("after", {})

            last_date = after.get(
                "last_date"
            )

            trading_days = int(
                after.get("trading_days", 0)
                or 0
            )

            gap_after = int(
                result.get("gap_after", 0)
                or 0
            )

            row = {
                "ticker": ticker,
                "last_date": last_date,
                "trading_days": trading_days,
                "gap_after": gap_after,
            }

            if (
                last_date == target_date
                and gap_after == 0
            ):
                success.append(row)

                send_telegram(
                    (
                        f"✅ [{idx}/{total}] "
                        f"{ticker}\n"
                        f"> Last: {last_date}\n"
                        f"> Gap: {gap_after}"
                    ),
                    chat_id,
                )

            else:
                partial.append(row)

                send_telegram(
                    (
                        f"⚠️ [{idx}/{total}] "
                        f"{ticker} PARTIAL\n"
                        f"> Last: {last_date}\n"
                        f"> Target: {target_date}\n"
                        f"> Gap: {gap_after}"
                    ),
                    chat_id,
                )

        except Exception as exc:
            failed.append(
                {
                    "ticker": ticker,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )

            send_telegram(
                (
                    f"❌ [{idx}/{total}] "
                    f"{ticker} FAILED\n"
                    f"> {type(exc).__name__}: "
                    f"{exc}"
                ),
                chat_id,
            )

    checkpoint_db()

    lines = [
        "✅ REFRESH ALL SELESAI",
        "",
        f"> Target Date: {target_date}",
        f"> Total Ticker: {total}",
        f"> Success: {len(success)}",
        f"> Partial: {len(partial)}",
        f"> Failed: {len(failed)}",
    ]

    if success:
        lines.extend(
            [
                "",
                "✅ SUCCESS",
                ", ".join(
                    row["ticker"]
                    for row in success
                ),
            ]
        )

    if partial:
        lines.extend(
            [
                "",
                "⚠️ PARTIAL",
            ]
        )

        for row in partial:
            lines.append(
                (
                    f"> {row['ticker']} "
                    f"| Last {row['last_date']} "
                    f"| Gap {row['gap_after']}"
                )
            )

    if failed:
        lines.extend(
            [
                "",
                "❌ FAILED",
            ]
        )

        for row in failed:
            lines.append(
                (
                    f"> {row['ticker']} "
                    f"| {row['error_type']}"
                )
            )

    return "\n".join(lines)

def command_result(text: str, chat_id: str) -> str:
    raw = text.strip()
    if not raw:
        return HELP_TEXT

    # Telegram slash command can arrive as /help@BotName
    parts = raw.split()
    first = parts[0].split("@")[0]
    first_lower = first.lower()

    if first_lower in {"/start", "/help", "help"}:
        return HELP_TEXT

    if first_lower == "/list":
        tickers = load_watchlist()
        if not tickers:
            return "📋 Watchlist kosong."
        return "📋 WATCHLIST\n" + "\n".join(
            f"{i}. {ticker}"
            for i, ticker in enumerate(tickers, start=1)
        )

    if first_lower in {
        "/add",
        "/remove",
        "/status",
        "/refresh",
        "/ticker",
    }:
        if len(parts) < 2:
            return f"Format: {first_lower} TICKER"

        target = parts[1].strip().upper()

        # /refresh ALL harus diproses sebelum
        # ALL dianggap sebagai kode ticker.
        if (
            first_lower == "/refresh"
            and target == "ALL"
        ):
            return refresh_all_watchlist(
                chat_id
            )

        ticker = normalize_ticker(
            target
        )

        if first_lower == "/add":
            ok, reason = validate_ticker_with_api(ticker)
            if not ok:
                return (
                    f"❌ {ticker} gagal divalidasi oleh ZAPI.\n"
                    f"Detail: {reason}"
                )

            was_added = add_watchlist(ticker)

            send_telegram(
                (
                    f"⏳ {ticker} "
                    f"{'ditambahkan ke watchlist.' if was_added else 'sudah ada di watchlist.'}\n"
                    "Smart backfill/update sedang dijalankan di GitHub Actions."
                ),
                chat_id,
            )

            result = update_ticker_history(
                ticker,
                full_ytd_if_new=True,
                repair_gaps=True,
            )

            after = result["after"]
            return "\n".join(
                [
                    f"✅ {ticker} READY",
                    f"> First: {after.get('first_date')}",
                    f"> Last: {after.get('last_date')}",
                    f"> Trading Days: {after.get('trading_days', 0)}",
                    f"> Rows: {after.get('rows_count', 0)}",
                    f"> Gap before/after: "
                    f"{result.get('gap_before', 0)} / "
                    f"{result.get('gap_after', 0)}",
                    "",
                    f"Ketik {ticker} untuk full broker summary.",
                ]
            )

        if first_lower == "/remove":
            removed = remove_watchlist(ticker)
            if removed:
                return (
                    f"✅ {ticker} dihapus dari watchlist.\n"
                    "Histori di broker_tracker.db TIDAK dihapus."
                )
            return f"ℹ️ {ticker} tidak ada di watchlist."

        if first_lower == "/status":
            return format_status(ticker)

        if first_lower == "/refresh":
            send_telegram(
                f"⏳ Refresh {ticker} sedang dijalankan...",
                chat_id,
            )
            result = update_ticker_history(
                ticker,
                full_ytd_if_new=False,
                repair_gaps=True,
            )
            after = result["after"]
            return "\n".join(
                [
                    f"✅ REFRESH {ticker} SELESAI",
                    f"> Last: {after.get('last_date')}",
                    f"> Trading Days: {after.get('trading_days', 0)}",
                    f"> Internal Gap: {result.get('gap_after', 0)}",
                ]
            )

        if first_lower == "/ticker":
            return format_full_report(ticker)

    # Free-form:
    # ADRO
    # ADRO 5D
    # ADRO VALUE
    # ADRO BROKER
    ticker = normalize_ticker(parts[0])

    if len(parts) == 1:
        return format_full_report(ticker)

    tokens = [p.upper() for p in parts[1:]]

        # Specific broker / CC query
    # Contoh:
    # BKSL CC NI
    # BKSL CC NI 5D
    # BKSL CC NI 10D
    # BKSL CC NI 20D
    # BKSL CC NI YTD

    if "DAILY" in tokens or "TODAY" in tokens:
        return format_daily_broker_report(
            ticker
        )

    if "CC" in tokens:
        cc_index = tokens.index("CC")

        if cc_index + 1 >= len(tokens):
            return (
                "❌ Format salah.\n"
                "Contoh: BKSL CC NI 20D"
            )

        broker_code = tokens[cc_index + 1]

        period = next(
            (
                p
                for p in ("5D", "10D", "20D", "YTD")
                if p in tokens
            ),
            "20D",
        )

        return format_broker_code_report(
            ticker,
            broker_code,
            period,
        )

    for period in ("5D", "10D", "20D", "YTD"):
        if period in tokens and "BROKER" not in tokens:
            return format_single_period(ticker, period)

    if "VALUE" in tokens:
        return format_value_report(ticker)

    if "BROKER" in tokens:
        period = next(
            (p for p in ("5D", "10D", "20D", "YTD") if p in tokens),
            "20D",
        )
        return format_broker_report(ticker, period)

    return HELP_TEXT


def process_telegram_update(
    update: dict[str, Any],
) -> int:

    text, chat_id, update_id = extract_message(
        update
    )

    if not authorized(chat_id):
        print(
            f"Ignored unauthorized "
            f"chat_id={chat_id}"
        )
        return 0

    # ========================================================
    # DUPLICATE PROTECTION
    # ========================================================

    try:
        is_new = claim_telegram_update(
            update_id,
            chat_id,
            text,
        )

    except Exception as exc:
        traceback.print_exc()

        try:
            send_telegram(
                (
                    "❌ BOT PROTECTION ERROR\n"
                    f"{type(exc).__name__}: {exc}\n\n"
                    "Command tidak dijalankan."
                ),
                chat_id,
            )
        except Exception:
            pass

        return 1

    if not is_new:
        print(
            "Ignored duplicate Telegram update "
            f"update_id={update_id} "
            f"chat_id={chat_id}"
        )

        return 0

    # ========================================================
    # PROCESS COMMAND
    # ========================================================

    try:
        result = command_result(
            text,
            chat_id,
        )

        send_telegram(
            result,
            chat_id,
        )

        checkpoint_db()

        return 0

    except Exception as exc:
        traceback.print_exc()

        try:
            send_telegram(
                (
                    "❌ Bot mengalami error.\n"
                    f"{type(exc).__name__}: {exc}"
                ),
                chat_id,
            )
        except Exception:
            pass

        return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event-file")
    parser.add_argument("--text")
    parser.add_argument("--chat-id")
    args = parser.parse_args()

    ensure_schema()

    if args.event_file:
        update = parse_update_from_event(args.event_file)
        return process_telegram_update(update)

    if args.text:
        chat_id = str(args.chat_id or TELEGRAM_CHAT_ID)
        if not authorized(chat_id):
            raise RuntimeError("chat_id manual tidak sesuai TELEGRAM_CHAT_ID.")

        try:
            result = command_result(args.text, chat_id)
            print(result)
            send_telegram(result, chat_id)
            checkpoint_db()
            return 0
        except Exception:
            traceback.print_exc()
            return 1

    parser.error("Gunakan --event-file atau --text.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
