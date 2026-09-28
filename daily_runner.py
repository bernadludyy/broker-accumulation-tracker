from __future__ import annotations

import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

# Windows / Task Scheduler UTF-8 protection
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)

import tracker


# ============================================================
# CONFIG
# ============================================================

WATCHLIST_FILE = BASE_DIR / "watchlist.txt"

TELEGRAM_WATCHLIST_FILE = BASE_DIR / "telegram_watchlist.txt"

LOOKBACKS = (5, 10, 20)
TOP_N_TELEGRAM = 5
BROKER_TOP_N = 5
SLEEP_SECONDS = 2

# Test dulu dengan False. Setelah lolos, ubah True.
SEND_TELEGRAM = True
FORCE_SEND = False

# Smart incremental backfill.
# True = ticker baru diisi YTD; ticker lama hanya mengisi histori awal yang belum ada.
AUTO_BACKFILL_NEW_TICKERS = True

ANALYZE_ONLY_TELEGRAM_WATCHLIST = False

TIMEZONE = ZoneInfo("Asia/Jakarta")
LOG_DIR = BASE_DIR / "logs"
SNAPSHOT_DIR = BASE_DIR / "snapshots"
STATE_FILE = BASE_DIR / ".last_sent_safe_end_date"


# ============================================================
# WATCHLIST
# ============================================================

def load_watchlist() -> list[str]:
    if not WATCHLIST_FILE.exists():
        raise FileNotFoundError(
            f"Watchlist tidak ditemukan: {WATCHLIST_FILE}"
        )

    with WATCHLIST_FILE.open("r", encoding="utf-8") as f:
        tickers = [
            line.strip().upper()
            for line in f
            if line.strip()
            and not line.strip().startswith("#")
        ]

    tickers = list(dict.fromkeys(tickers))

    if not tickers:
        raise RuntimeError("watchlist.txt kosong.")

    return tickers


WATCHLIST_MAIN = load_watchlist()

def load_telegram_watchlist():
    if not TELEGRAM_WATCHLIST_FILE.exists():
        return []

    with open(
        TELEGRAM_WATCHLIST_FILE,
        "r",
        encoding="utf-8"
    ) as f:
        tickers = [
            line.strip().upper()
            for line in f
            if line.strip()
            and not line.strip().startswith("#")
        ]

    return list(dict.fromkeys(tickers))


TELEGRAM_WATCHLIST = load_telegram_watchlist()

if ANALYZE_ONLY_TELEGRAM_WATCHLIST:
    ACTIVE_WATCHLIST = TELEGRAM_WATCHLIST
else:
    ACTIVE_WATCHLIST = WATCHLIST_MAIN

# ============================================================
# LOGGING
# ============================================================

def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    log_date = datetime.now(TIMEZONE).strftime("%Y%m%d")
    log_file = LOG_DIR / f"broker_tracker_{log_date}.log"

    logger = logging.getLogger("broker_daily_runner")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s"
    )

    file_handler = logging.FileHandler(
        log_file,
        encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)

    return logger


# ============================================================
# VALIDATION
# ============================================================

def validate_tracker_functions() -> None:
    required = [
        "init_db",
        "get_connection",
        "collect_daily",
        "get_safe_end_date",
        "collect_missing_dates",
        "scan_watchlist_trading_days",
        "create_final_broker_score",
        "scan_watchlist_ytd",
        "scan_watchlist_broker_dominance",
        "format_dashboard_telegram_message",
        "send_telegram",
    ]

    missing = [
        name
        for name in required
        if not hasattr(tracker, name)
    ]

    if missing:
        raise RuntimeError(
            "Fungsi berikut belum tersedia di tracker.py: "
            + ", ".join(missing)
        )


# ============================================================
# HISTORY / AUTO BACKFILL
# ============================================================

def get_ticker_history_status(
    ticker: str,
) -> dict:
    """
    Membaca histori broker yang sudah tersimpan di SQLite.
    Tidak memakan API.
    """
    con = tracker.get_connection()

    try:
        row = con.execute(
            """
            SELECT
                MIN(trade_date) AS first_date,
                MAX(trade_date) AS last_date,
                COUNT(DISTINCT trade_date) AS trading_days,
                COUNT(*) AS rows_count
            FROM broker_daily
            WHERE ticker = ?
            """,
            (ticker.upper(),),
        ).fetchone()

    finally:
        con.close()

    return {
        "ticker": ticker.upper(),
        "first_date": row[0] if row else None,
        "last_date": row[1] if row else None,
        "trading_days": int(row[2] or 0) if row else 0,
        "rows_count": int(row[3] or 0) if row else 0,
    }

# ============================================================
# INTERNAL GAP DETECTOR
# ============================================================

def get_reference_trading_dates(
    safe_end_date: str,
) -> list[str]:
    """
    Mengambil daftar hari perdagangan yang sudah diketahui
    dari database.

    Tidak menggunakan API.
    """

    year = safe_end_date[:4]
    year_start = f"{year}-01-01"

    con = tracker.get_connection()

    try:
        rows = con.execute(
            """
            SELECT trade_date
            FROM broker_daily
            WHERE trade_date BETWEEN ? AND ?
            GROUP BY trade_date
            HAVING COUNT(DISTINCT ticker) >= 2
            ORDER BY trade_date
            """,
            (
                year_start,
                safe_end_date,
            ),
        ).fetchall()

    finally:
        con.close()

    return [
        row[0]
        for row in rows
    ]


def get_ticker_trade_dates(
    ticker: str,
    safe_end_date: str,
) -> set[str]:

    year = safe_end_date[:4]
    year_start = f"{year}-01-01"

    con = tracker.get_connection()

    try:
        rows = con.execute(
            """
            SELECT DISTINCT trade_date
            FROM broker_daily
            WHERE ticker = ?
              AND trade_date BETWEEN ? AND ?
            ORDER BY trade_date
            """,
            (
                ticker.upper(),
                year_start,
                safe_end_date,
            ),
        ).fetchall()

    finally:
        con.close()

    return {
        row[0]
        for row in rows
    }


def repair_internal_gaps(
    tickers: list[str],
    safe_end_date: str,
    logger: logging.Logger,
) -> pd.DataFrame:

    reference_dates = get_reference_trading_dates(
        safe_end_date
    )

    logger.info(
        "Reference trading days=%s",
        len(reference_dates),
    )

    results = []

    for ticker in tickers:

        existing_dates = get_ticker_trade_dates(
            ticker,
            safe_end_date,
        )

        missing_dates = [
            trade_date
            for trade_date in reference_dates
            if trade_date not in existing_dates
        ]

        if not missing_dates:

            logger.info(
                "%s | INTERNAL GAPS=0 | COMPLETE",
                ticker,
            )

            status = get_ticker_history_status(
                ticker
            )

            results.append({
                "ticker": ticker,
                "missing_before": 0,
                "missing_after": 0,
                "trading_days": status["trading_days"],
                "status": "COMPLETE",
            })

            continue

        logger.warning(
            "%s | INTERNAL GAPS=%s | %s -> %s",
            ticker,
            len(missing_dates),
            missing_dates[0],
            missing_dates[-1],
        )

        for trade_date in missing_dates:

            try:

                tracker.collect_daily(
                    ticker=ticker,
                    start_date=trade_date,
                    end_date=trade_date,
                    sleep_seconds=SLEEP_SECONDS,
                )

            except KeyboardInterrupt:

                logger.warning(
                    "Internal gap repair dihentikan "
                    "manual pada %s.",
                    ticker,
                )

                raise

            except Exception as exc:

                logger.error(
                    "%s | %s | gap repair error: %s",
                    ticker,
                    trade_date,
                    exc,
                )

        # Check ulang setelah repair
        existing_after = get_ticker_trade_dates(
            ticker,
            safe_end_date,
        )

        missing_after = [
            trade_date
            for trade_date in reference_dates
            if trade_date not in existing_after
        ]

        status = get_ticker_history_status(
            ticker
        )

        logger.info(
            "%s | GAP REPAIR DONE | "
            "before=%s | after=%s | days=%s",
            ticker,
            len(missing_dates),
            len(missing_after),
            status["trading_days"],
        )

        results.append({
            "ticker": ticker,
            "missing_before": len(missing_dates),
            "missing_after": len(missing_after),
            "trading_days": status["trading_days"],
            "status": (
                "COMPLETE"
                if not missing_after
                else "PARTIAL"
            ),
        })

    return pd.DataFrame(results)

def get_smart_backfill_range(
    status: dict,
    safe_end_date: str,
) -> tuple[str | None, str | None, str]:
    """
    Tentukan rentang historical backfill TANPA mengambil ulang data yang
    sudah tersimpan.

    Kasus:
    1. Ticker belum punya data:
       1 Jan -> SAFE_END_DATE
    2. Ticker punya data tetapi first_date terlambat:
       1 Jan -> sehari sebelum first_date
    3. Histori sudah mulai awal Januari:
       tidak perlu historical backfill

    Gap setelah last_date tetap ditangani oleh collect_missing_dates()
    setelah fungsi ini selesai.
    """

    year = int(safe_end_date[:4])
    year_start = f"{year}-01-01"

    first_date = status.get("first_date")

    # Ticker benar-benar baru
    if not first_date:
        return (
            year_start,
            safe_end_date,
            "NEW TICKER - FULL YTD",
        )

    first_dt = datetime.strptime(
        first_date,
        "%Y-%m-%d",
    ).date()

    # Anggap YTD sudah memiliki awal tahun bila first stored date
    # sudah berada pada 10 hari kalender pertama Januari.
    # Ini memberi toleransi untuk libur Tahun Baru / hari non-bursa.
    early_january_limit = datetime(
        year,
        1,
        10,
    ).date()

    if first_dt <= early_january_limit:
        return (
            None,
            None,
            "YTD HISTORY OK",
        )

    # Ticker lama/partial:
    # hanya ambil periode sebelum first_date yang sudah ada.
    backfill_end = (
        first_dt
        - timedelta(days=1)
    ).isoformat()

    return (
        year_start,
        backfill_end,
        "PARTIAL - EARLY GAP ONLY",
    )


def auto_backfill_watchlist(
    tickers: list[str],
    safe_end_date: str,
    logger: logging.Logger,
) -> pd.DataFrame:
    """
    SMART INCREMENTAL BACKFILL

    Tidak lagi selalu request 1 Jan -> SAFE_END_DATE untuk ticker partial.

    - NEW TICKER:
      1 Jan -> SAFE_END_DATE

    - PARTIAL EXISTING TICKER:
      1 Jan -> sehari sebelum first_date yang sudah tersimpan

    - YTD HISTORY OK:
      0 historical request

    Sesudah fungsi ini, collect_missing_dates() menangani tanggal terbaru
    setelah last_date.
    """

    rows = []

    for ticker in tickers:

        before = get_ticker_history_status(
            ticker
        )

        if not AUTO_BACKFILL_NEW_TICKERS:

            rows.append({
                **before,
                "backfill_start": None,
                "backfill_end": None,
                "action": "AUTO BACKFILL OFF",
                "saved_rows": 0,
            })

            continue

        (
            backfill_start,
            backfill_end,
            action,
        ) = get_smart_backfill_range(
            before,
            safe_end_date,
        )

        # Histori awal tahun sudah cukup.
        if backfill_start is None:

            logger.info(
                "%s | %s | first=%s | last=%s",
                ticker,
                action,
                before["first_date"],
                before["last_date"],
            )

            rows.append({
                **before,
                "backfill_start": None,
                "backfill_end": None,
                "action": action,
                "saved_rows": 0,
            })

            continue

        logger.info(
            "%s | %s | Backfill %s -> %s | existing first=%s last=%s",
            ticker,
            action,
            backfill_start,
            backfill_end,
            before["first_date"],
            before["last_date"],
        )

        try:

            result = tracker.collect_daily(
                ticker=ticker,
                start_date=backfill_start,
                end_date=backfill_end,
                sleep_seconds=SLEEP_SECONDS,
            )

            after = get_ticker_history_status(
                ticker
            )

            saved_rows = (
                result.get("saved_rows", 0)
                if isinstance(result, dict)
                else None
            )

            rows.append({
                "ticker": ticker,
                "first_date": after["first_date"],
                "last_date": after["last_date"],
                "trading_days": after["trading_days"],
                "rows_count": after["rows_count"],
                "backfill_start": backfill_start,
                "backfill_end": backfill_end,
                "action": action,
                "saved_rows": saved_rows,
            })

            logger.info(
                "%s backfill selesai | first=%s | last=%s | "
                "days=%s | saved_rows=%s",
                ticker,
                after["first_date"],
                after["last_date"],
                after["trading_days"],
                saved_rows,
            )

        except KeyboardInterrupt:
            logger.warning(
                "Backfill dihentikan manual pada ticker %s.",
                ticker,
            )
            raise

        except Exception as exc:

            logger.exception(
                "Backfill %s gagal: %s",
                ticker,
                exc,
            )

            rows.append({
                **before,
                "backfill_start": backfill_start,
                "backfill_end": backfill_end,
                "action": "BACKFILL ERROR",
                "saved_rows": 0,
            })

    return pd.DataFrame(rows)


# ============================================================
# FLOW ALIGNMENT
# ============================================================

def classify_alignment_fallback(row: pd.Series) -> str:
    short_label = str(
        row.get("final_label", "NEUTRAL")
    ).upper()

    ytd_label = str(
        row.get("ytd_label", "NEUTRAL")
    ).upper()

    short_acc = "ACCUMULATION" in short_label
    short_dist = "DISTRIBUTION" in short_label
    ytd_acc = "ACCUMULATION" in ytd_label
    ytd_dist = "DISTRIBUTION" in ytd_label

    if short_acc and ytd_acc:
        return "CONSISTENT ACCUMULATION"

    if short_dist and ytd_dist:
        return "CONSISTENT DISTRIBUTION"

    if short_acc and ytd_dist:
        return "NEW ACCUMULATION / REVERSAL"

    if short_dist and ytd_acc:
        return "SHORT-TERM DISTRIBUTION"

    return "MIXED / NEUTRAL"


# ============================================================
# COMBINE RESULT
# ============================================================

def build_combined_result(
    final_multi: pd.DataFrame,
    ytd_result: pd.DataFrame,
    result_multi: pd.DataFrame,
) -> pd.DataFrame:

    if final_multi is None or final_multi.empty:
        raise RuntimeError(
            "final_multi kosong. Pipeline dihentikan."
        )

    if ytd_result is None or ytd_result.empty:
        raise RuntimeError(
            "ytd_result kosong. Pipeline dihentikan."
        )

    ytd_columns = [
        "ticker",
        "ytd_start",
        "ytd_end",
        "ytd_trading_days",
        "ytd_net_lots",
        "ytd_avg_buy",
        "ytd_accumulation_ratio",
        "ytd_score",
        "ytd_label",
    ]

    available_ytd_columns = [
        col
        for col in ytd_columns
        if col in ytd_result.columns
    ]

    if "ticker" not in available_ytd_columns:
        raise RuntimeError(
            "Kolom ticker tidak tersedia di ytd_result."
        )

    combined = final_multi.merge(
        ytd_result[available_ytd_columns],
        on="ticker",
        how="left",
    )

    consistency_available = [
        col
        for col in [
            "consistency_direction",
            "consistency_label",
        ]
        if col in result_multi.columns
        and col not in combined.columns
    ]

    if consistency_available:
        consistency_lookup = (
            result_multi[
                ["ticker"] + consistency_available
            ]
            .drop_duplicates(subset="ticker")
        )

        combined = combined.merge(
            consistency_lookup,
            on="ticker",
            how="left",
        )

    if hasattr(tracker, "classify_alignment"):
        combined["flow_alignment"] = (
            combined.apply(
                tracker.classify_alignment,
                axis=1,
            )
        )
    else:
        combined["flow_alignment"] = (
            combined.apply(
                classify_alignment_fallback,
                axis=1,
            )
        )

    return combined


# ============================================================
# DASHBOARD RESULT
# ============================================================

def build_dashboard_result(
    combined_result: pd.DataFrame,
    safe_end_date: str,
    logger: logging.Logger,
) -> pd.DataFrame:

    dominance_summary = (
        tracker.scan_watchlist_broker_dominance(
            WATCHLIST_MAIN,
            end_date=safe_end_date,
            top_n=BROKER_TOP_N,
        )
    )

    if (
        dominance_summary is None
        or dominance_summary.empty
    ):
        logger.warning(
            "Broker dominance kosong. "
            "Dashboard tetap dibuat tanpa dominance."
        )
        dashboard = combined_result.copy()

    else:
        dashboard = combined_result.merge(
            dominance_summary,
            on="ticker",
            how="left",
        )

    if hasattr(
        tracker,
        "add_dominant_broker_daily_action"
    ):
        dashboard = (
            tracker.add_dominant_broker_daily_action(
                dashboard,
                trade_date=safe_end_date,
            )
        )
    else:
        logger.warning(
            "add_dominant_broker_daily_action() "
            "belum ditemukan. Daily broker action dilewati."
        )

    return dashboard


# ============================================================
# SNAPSHOT
# ============================================================

def save_dashboard_snapshot(
    dashboard_result: pd.DataFrame,
    safe_end_date: str,
) -> Path:

    SNAPSHOT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    file_path = (
        SNAPSHOT_DIR
        / f"dashboard_{safe_end_date}.csv"
    )

    dashboard_result.to_csv(
        file_path,
        index=False,
        encoding="utf-8-sig",
    )

    return file_path


# ============================================================
# TELEGRAM STATE
# ============================================================

def already_sent(
    safe_end_date: str,
) -> bool:

    if FORCE_SEND:
        return False

    if not STATE_FILE.exists():
        return False

    last_sent = (
        STATE_FILE.read_text(
            encoding="utf-8"
        ).strip()
    )

    return last_sent == safe_end_date


def mark_as_sent(
    safe_end_date: str,
) -> None:

    STATE_FILE.write_text(
        safe_end_date,
        encoding="utf-8",
    )


# ============================================================
# MAIN
# ============================================================

def main() -> None:

    logger = setup_logging()
    started = time.perf_counter()

    logger.info(
        "========== DAILY RUN START =========="
    )

    validate_tracker_functions()
    tracker.init_db()

    today = (
        datetime.now(TIMEZONE)
        .date()
        .isoformat()
    )

    safe_end_date = (
        tracker.get_safe_end_date(today)
    )

    logger.info(
        "TODAY=%s | SAFE_END_DATE=%s",
        today,
        safe_end_date,
    )

    logger.info(
        "WATCHLIST=%s",
        ", ".join(WATCHLIST_MAIN),
    )

    # --------------------------------------------------------
    # 1. AUTO-BACKFILL TICKER BARU
    # --------------------------------------------------------

    logger.info(
        "Checking ticker history / auto-backfill..."
    )

    backfill_result = auto_backfill_watchlist(
        WATCHLIST_MAIN,
        safe_end_date,
        logger,
    )

    if not backfill_result.empty:
        logger.info(
            "Backfill result:\n%s",
            backfill_result.to_string(index=False),
        )

    # --------------------------------------------------------
    # 2. UPDATE GAP TERBARU
    # --------------------------------------------------------

    logger.info(
        "Updating missing broker data..."
    )

    gap_result = repair_internal_gaps(
        WATCHLIST_MAIN,
        safe_end_date,
        logger,
    )

    logger.info(
        "Internal gap result:\n%s",
        gap_result.to_string(index=False),
    )

    update_result = (
        tracker.collect_missing_dates(
            WATCHLIST_MAIN,
            end_date=safe_end_date,
            sleep_seconds=SLEEP_SECONDS,
        )
    )

    if update_result is not None:
        if hasattr(update_result, "to_string"):
            logger.info(
                "Update result:\n%s",
                update_result.to_string(index=False),
            )
        else:
            logger.info(
                "Update result=%s",
                update_result,
            )

    # --------------------------------------------------------
    # 3. 5D / 10D / 20D
    # --------------------------------------------------------

    logger.info(
        "Calculating 5D/10D/20D..."
    )

    result_multi = (
        tracker.scan_watchlist_trading_days(
            WATCHLIST_MAIN,
            end_date=safe_end_date,
            lookbacks=LOOKBACKS,
            fetch_prices=True,
        )
    )

    if (
        result_multi is None
        or result_multi.empty
    ):
        raise RuntimeError(
            "result_multi kosong."
        )

    final_multi = (
        tracker.create_final_broker_score(
            result_multi
        )
    )

    # --------------------------------------------------------
    # 4. YTD
    # --------------------------------------------------------

    logger.info(
        "Calculating YTD..."
    )

    ytd_result = (
        tracker.scan_watchlist_ytd(
            WATCHLIST_MAIN,
            end_date=safe_end_date,
            fetch_prices=False,
        )
    )

    # --------------------------------------------------------
    # 5. COMBINE
    # --------------------------------------------------------

    combined_result = (
        build_combined_result(
            final_multi=final_multi,
            ytd_result=ytd_result,
            result_multi=result_multi,
        )
    )

    # --------------------------------------------------------
    # 6. BROKER DOMINANCE + DAILY ACTION
    # --------------------------------------------------------

    logger.info(
        "Calculating broker dominance..."
    )

    dashboard_result = (
        build_dashboard_result(
            combined_result,
            safe_end_date,
            logger,
        )
    )

    # --------------------------------------------------------
    # 7. SNAPSHOT
    # --------------------------------------------------------

    snapshot_file = (
        save_dashboard_snapshot(
            dashboard_result,
            safe_end_date,
        )
    )

    logger.info(
        "Snapshot saved: %s",
        snapshot_file,
    )

    # --------------------------------------------------------
    # 8. TELEGRAM
    # --------------------------------------------------------

    # ============================================================
    # TELEGRAM FILTER
    # ============================================================

# ============================================================
# FILTER KHUSUS TELEGRAM
# ============================================================

    if not TELEGRAM_WATCHLIST:
        raise RuntimeError(
            "telegram_watchlist.txt kosong. "
            "Tidak ada ticker yang dipilih untuk Telegram."
        )

    telegram_result = dashboard_result[
        dashboard_result["ticker"].isin(
            TELEGRAM_WATCHLIST
        )
    ].copy()

    if telegram_result.empty:
        raise RuntimeError(
            "Tidak ada ticker dari telegram_watchlist.txt "
            "yang ditemukan di dashboard_result."
            )

 # Urutkan sesuai urutan di telegram_watchlist.txt
    telegram_result["telegram_order"] = (
        telegram_result["ticker"]
        .map({
            ticker: i
            for i, ticker in enumerate(
                TELEGRAM_WATCHLIST
            )
        })
    )

    telegram_result = (
        telegram_result
        .sort_values("telegram_order")
        .drop(columns=["telegram_order"])
        .reset_index(drop=True)
    )

    logger.info(
        "Telegram watchlist=%s",
        ", ".join(TELEGRAM_WATCHLIST),
    )

    logger.info(
        "Telegram result=%s",
        ", ".join(
            telegram_result["ticker"]
            .dropna()
            .astype(str)
            .tolist()
        ),
    )


    message_final = tracker.format_dashboard_telegram_message(
        telegram_result,
        end_date=safe_end_date,
        top_n=len(telegram_result),
    )

    logger.info(
        "Telegram message length=%s characters",
        len(message_final),
    )

    # Tidak print seluruh message agar aman di Task Scheduler.
    # print(message_final)

    if not SEND_TELEGRAM:
        logger.info(
            "SEND_TELEGRAM=False. "
            "Telegram tidak dikirim (test mode)."
        )

    elif already_sent(safe_end_date):
        logger.info(
            "Telegram untuk %s sudah pernah dikirim. "
            "Skip duplicate.",
            safe_end_date,
        )

    else:
        logger.info(
            "Sending Telegram..."
        )

        response = tracker.send_telegram(
            message_final
        )

        mark_as_sent(
            safe_end_date
        )

        logger.info(
            "Telegram SUCCESS | response=%s",
            response,
        )

    elapsed = (
        time.perf_counter()
        - started
    )

    logger.info(
        "Runtime %.2f seconds",
        elapsed,
    )

    logger.info(
        "========== DAILY RUN DONE =========="
    )


if __name__ == "__main__":
    try:
        main()

    except Exception:
        logger = logging.getLogger(
            "broker_daily_runner"
        )

        if logger.handlers:
            logger.exception(
                "DAILY RUN FAILED"
            )

        raise
