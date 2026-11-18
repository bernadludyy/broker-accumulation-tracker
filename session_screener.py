"""On-demand IDX session screens using the existing shared snapshot and EOD archive.

Rules are research hypotheses. No intraday 30-minute bars or auction fills are
inferred from daily OHLCV. EOD output is a next-session watchlist only.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import tracker
from bot2_entry import _market_rows_from_payload, load_shared_live_market
from cache_commands import _connect, _expected_eod_date, _load_archived_market, load_cached_market
from market_combos import _num, _ticker

WIB = ZoneInfo("Asia/Jakarta")
SESSION_USAGE = "Format: BSJP|BPJS LIVE|EOD [QUALITY] [1-20]"
QUALITY_CACHE_MAX_AGE_SECONDS = 20 * 60


@dataclass(frozen=True)
class SessionHistory:
    previous_day: str
    previous_close: float
    ma5: float | None
    ma10: float | None
    ma20: float | None
    average_volume: float | None
    average_value: float
    value_sessions: int


def load_baselines(day: str) -> dict[str, tuple[float, int]]:
    """Read completed prior sessions only; one bounded query, zero ZAPI calls."""
    with _connect() as con:
        with con.cursor() as cur:
            cur.execute("""WITH ranked AS (
                SELECT ticker, turnover_idr, row_number() OVER
                    (PARTITION BY ticker ORDER BY trade_date DESC) AS rn
                FROM project_fabric.stock_summary_daily
                WHERE trade_date < %s::date AND trade_date >= %s::date - 45
                  AND turnover_idr > 0
            )
            SELECT ticker, avg(turnover_idr), count(*)
            FROM ranked WHERE rn <= 20 GROUP BY ticker HAVING count(*) >= 5
            """, (day, day))
            return {code: (float(value), int(count)) for code, value, count in cur.fetchall()}


def load_quality_history(day: str) -> dict[str, SessionHistory]:
    """Average only prior completed sessions and require full MA/RVOL windows."""
    with _connect() as con, con.cursor() as cur:
        cur.execute("""WITH ranked AS (
            SELECT ticker, trade_date, close_price, volume_shares, turnover_idr,
                   dense_rank() OVER (ORDER BY trade_date DESC) AS session
            FROM project_fabric.stock_summary_daily
            WHERE trade_date < %s::date AND trade_date >= %s::date - 45
        )
        SELECT ticker, max(trade_date) FILTER (WHERE session = 1),
               (max(close_price) FILTER (WHERE session = 1 AND close_price > 0))::float8,
               CASE WHEN count(*) FILTER (WHERE close_price > 0 AND session <= 5) = 5
                    THEN (avg(close_price) FILTER (WHERE session <= 5))::float8 END,
               CASE WHEN count(*) FILTER (WHERE close_price > 0 AND session <= 10) = 10
                    THEN (avg(close_price) FILTER (WHERE session <= 10))::float8 END,
               CASE WHEN count(*) FILTER (WHERE close_price > 0) = 20
                    THEN avg(close_price)::float8 END,
               CASE WHEN count(*) FILTER (WHERE volume_shares >= 0) = 20
                    THEN nullif(avg(volume_shares), 0)::float8 END,
               (avg(turnover_idr) FILTER (WHERE turnover_idr > 0))::float8,
               count(*) FILTER (WHERE turnover_idr > 0)
        FROM ranked WHERE session <= 20
        GROUP BY ticker
        HAVING count(*) FILTER (WHERE session = 1 AND close_price > 0) = 1
           AND count(*) FILTER (WHERE turnover_idr > 0) >= 5
        """, (day, day))
        return {
            code: SessionHistory(str(previous_day), previous_close, ma5, ma10,
                                 ma20, avg_volume, avg_value, count)
            for code, previous_day, previous_close, ma5, ma10, ma20,
                avg_volume, avg_value, count in cur.fetchall()
        }


def screen_rows(rows: list[dict], baselines: dict[str, tuple[float, int]],
                kind: str, mode: str) -> list[dict]:
    """Use fields observable at evaluation time; never assume final auction data."""
    found = []
    for row in rows:
        code = _ticker(row)
        if not code or code not in baselines:
            continue
        opening = _num(row, "OpenPrice", "Open", "open")
        price = _num(row, "Close", "close", "Last", "last")
        high = _num(row, "High", "HighPrice", "high")
        low = _num(row, "Low", "LowPrice", "low")
        value = _num(row, "Value", "value")
        freq = _num(row, "Frequency", "frequency")
        if not all(x is not None for x in (opening, price, high, low, value, freq)):
            continue
        if opening <= 0 or low <= 0 or high <= low or not low <= price <= high:
            continue
        if value < 2_000_000_000 or freq < 200 or price < 50:
            continue
        avg, sessions = baselines[code]
        if avg <= 0:
            continue
        location = (price - low) / (high - low)
        change = 100 * (price / opening - 1)
        relative_value = value / avg
        if kind == "BSJP":
            eligible = (location >= .70 and 0 <= change <= 6
                        and value >= 5_000_000_000 and freq >= 500
                        and relative_value >= .8)
            score = location * 40 + min(relative_value, 2) * 20 + min(value / 1e10, 2) * 10
        else:
            # The daily summary cannot establish first-30-minute momentum.
            # This is an open-to-current proxy in LIVE and a prior-day watchlist in EOD.
            eligible = (location >= .60 and .5 <= change <= 5
                        and relative_value >= (.15 if mode == "LIVE" else .8))
            score = location * 35 + min(relative_value, 2) * 20 + min(freq / 2000, 2) * 10
        if eligible:
            found.append(dict(ticker=code, price=price, change=change,
                              location=location, value=value, relative_value=relative_value,
                              baseline_sessions=sessions, score=score))
    return sorted(found, key=lambda x: (-x["score"], x["ticker"]))


def quality_metrics(row: dict, item: dict, history: SessionHistory,
                    kind: str, mode: str, day: str) -> tuple[float, float, float | None] | None:
    price = item["price"]
    previous = _num(row, "Previous", "previous", "PrevClose", "prev_close")
    if previous is not None and previous != history.previous_close:
        # A stale payload, corporate action or inconsistent reference needs review.
        return None
    raw_day = str(row.get("Date") or row.get("date") or row.get("TradingDate") or "")[:10]
    if mode == "LIVE" and raw_day != day:
        # Some live payloads retain yesterday's date. Require corroboration from
        # Previous in that case, which would usually disagree on yesterday's bar.
        if raw_day not in ("", history.previous_day) or previous is None:
            return None
    return_pct = 100 * (price - history.previous_close) / history.previous_close
    volume = _num(row, "Volume", "volume")
    relative_volume = (volume / history.average_volume
                       if volume is not None and volume >= 0 and history.average_volume else None)

    if kind == "BPJS":
        if history.ma5 is None or price < history.ma5 or return_pct < 2:
            return None
        if mode == "EOD" and (relative_volume is None or relative_volume < 1.5):
            return None
        return return_pct, (price - history.ma5) / history.ma5 * 100, relative_volume

    if history.ma10 is None or history.ma20 is None or relative_volume is None:
        return None
    if (history.ma10 < history.ma20 or price < history.ma20 or return_pct < 3
            or price > history.ma10 * 1.10 or relative_volume < 1.5):
        return None
    return return_pct, (price - history.ma20) / history.ma20 * 100, relative_volume


def format_session_screener(kind: str, mode: str, limit: int = 10,
                            *, now: datetime | None = None, quality: bool = False) -> str:
    kind, mode = kind.upper(), mode.upper()
    if kind not in {"BSJP", "BPJS"} or mode not in {"LIVE", "EOD"} or not 1 <= limit <= 20:
        return SESSION_USAGE
    now = (now or datetime.now(WIB)).astimezone(WIB)
    current = now.time()
    if mode == "LIVE":
        if (now.weekday() >= 5 or not time(9) <= current <= time(15, 50)
                or (quality and current == time(15, 50))):
            return f"{kind} LIVE: di luar jam bursa. Gunakan {kind} EOD untuk pantauan."
        if kind == "BSJP" and current < time(14, 30):
            return "BSJP LIVE tersedia mulai 14.30 WIB; volume sore belum teramati."
        if kind == "BPJS" and current < time(9, 30):
            return "BPJS LIVE tersedia mulai 09.30 WIB; tunggu pembukaan stabil."
        if quality and kind == "BPJS" and current >= time(11, 30):
            return "BPJS LIVE QUALITY tersedia 09.30–11.29 WIB. Gunakan BPJS EOD QUALITY untuk daftar pantauan."
        if quality:
            payload = tracker.peek_shared_stock_summary(max_age_seconds=QUALITY_CACHE_MAX_AGE_SECONDS)
            if payload is None:
                return f"{kind} LIVE QUALITY: cache stock-summary maksimal 20 menit belum tersedia."
            rows, day, source = (_market_rows_from_payload(payload), now.date().isoformat(),
                                 "PROJECT FABRIC SHARED LIVE CACHE (maks. 20 menit)")
        else:
            rows, day, source = load_shared_live_market(with_source=True, now=now)
        if day != now.date().isoformat():
            return f"{kind} LIVE: snapshot tanggal {day}, bukan sesi hari ini."
    elif quality:
        archive = _load_archived_market(now)
        if archive is None:
            return f"{kind} EOD QUALITY: arsip final Neon belum tersedia. Cek /eod status."
        rows, day = archive
        source = "NEON DAILY EOD SNAPSHOT"
    else:
        rows, day, source = load_cached_market(now=now, completed_only=True)
        if source != "NEON DAILY EOD SNAPSHOT":
            return (f"{kind} EOD: arsip final Neon belum tersedia (sumber: {source}). "
                    "Jalankan /eod status; snapshot intraday tidak dipakai sebagai EOD.")
        if day > _expected_eod_date(now):
            return f"{kind} EOD: snapshot {day} belum final."
    if len(rows) < 100:
        return f"{kind} {mode}: snapshot hanya {len(rows)} saham; universe belum lengkap."
    baseline_day = day if mode == "EOD" else now.date().isoformat()
    if quality:
        history = load_quality_history(baseline_day)
        if not history:
            return f"{kind} {mode} QUALITY: riwayat sesi sebelumnya belum tersedia untuk {day}."
        previous_day = next(iter(history.values())).previous_day
        if (date.fromisoformat(day) - date.fromisoformat(previous_day)).days > 7:
            return f"{kind} {mode} QUALITY: close acuan {previous_day} lebih dari 7 hari sebelum {day}."
        comparable = screen_rows(rows, {code: (h.average_value, h.value_sessions)
                                       for code, h in history.items()}, kind, mode)
        by_ticker = {_ticker(row): row for row in rows}
        ranked = []
        for item in comparable:
            code = item["ticker"]
            metrics = quality_metrics(by_ticker[code], item, history[code], kind, mode, day)
            if metrics is not None:
                daily_return, ma_distance, relative_volume = metrics
                ranked.append(dict(item, daily_return=daily_return, ma_distance=ma_distance,
                                   relative_volume=relative_volume))
    else:
        ranked = screen_rows(rows, load_baselines(baseline_day), kind, mode)

    profile = " QUALITY" if quality else ""
    lines = [f"🔎 {kind} {mode}{profile} | {day}", f"Sumber: {source}",
             f"Diperiksa {len(rows)} saham | lolos {len(ranked)} | tampil {min(limit, len(ranked))}"]
    if quality:
        lines.append(f"Lolos filter sesi sebelum QUALITY {len(comparable)} | close acuan {previous_day}.")
    if mode == "EOD":
        if day < _expected_eod_date(now):
            lines.append(f"⚠️ DATA LAMA: EOD target {_expected_eod_date(now)}; tersedia {day}.")
        lines.append("Daftar pantauan sesi berikutnya; harga penutupan sudah berlalu.")
    elif kind == "BPJS":
        lines.append("Proxy open→harga saat ini; volume 30 menit pertama belum tersedia.")
    else:
        lines.append("Snapshot menjelang tutup; harga lelang akhir belum diketahui.")
    for rank, item in enumerate(ranked[:limit], 1):
        lines.append(f"{rank}. {item['ticker']} Rp{item['price']:,.0f} | open→harga {item['change']:+.1f}% "
                     f"| posisi range {item['location']:.0%} | value Rp{item['value']/1e9:.1f} miliar "
                     f"({item['relative_value']:.1f}x rerata {item['baseline_sessions']} sesi)")
        if quality:
            ma = "MA5" if kind == "BPJS" else "MA20"
            volume = item["relative_volume"]
            rvol = f"{volume:.2f}x" if volume is not None else "N/A"
            lines.append(f"   vs close {item['daily_return']:+.1f}% | vs {ma} {item['ma_distance']:+.1f}% "
                         f"| volume/rerata harian 20D {rvol}")
    if quality and not ranked:
        lines.append("Belum ada kandidat dengan konfirmasi harga, riwayat, dan aktivitas yang sesuai.")
    elif not ranked:
        lines.append("Belum ada kandidat dengan data lengkap dan filter likuiditas yang sesuai.")
    lines.append("Filter riset, belum teruji profit bersih; cek spread dan harga eksekusi.")
    return "\n".join(lines)
