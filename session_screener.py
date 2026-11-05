"""On-demand IDX session screens using the existing shared snapshot and EOD archive.

Rules are research hypotheses. No intraday 30-minute bars or auction fills are
inferred from daily OHLCV. EOD output is a next-session watchlist only.
"""
from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from bot2_entry import load_shared_live_market
from cache_commands import _expected_eod_date, load_cached_market, _connect
from market_combos import _num, _ticker

WIB = ZoneInfo("Asia/Jakarta")


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


def format_session_screener(kind: str, mode: str, limit: int = 10,
                            *, now: datetime | None = None) -> str:
    kind, mode = kind.upper(), mode.upper()
    if kind not in {"BSJP", "BPJS"} or mode not in {"LIVE", "EOD"} or not 1 <= limit <= 20:
        return "Format: BSJP|BPJS LIVE|EOD [1-20]"
    now = (now or datetime.now(WIB)).astimezone(WIB)
    current = now.time()
    if mode == "LIVE":
        if now.weekday() >= 5 or not (time(9) <= current <= time(15, 50)):
            return f"{kind} LIVE: di luar jam bursa. Gunakan {kind} EOD untuk pantauan."
        if kind == "BSJP" and current < time(14, 30):
            return "BSJP LIVE tersedia mulai 14.30 WIB; volume sore belum teramati."
        if kind == "BPJS" and current < time(9, 30):
            return "BPJS LIVE tersedia mulai 09.30 WIB; tunggu pembukaan stabil."
        rows, day, source = load_shared_live_market(with_source=True, now=now)
        if day != now.date().isoformat():
            return f"{kind} LIVE: snapshot tanggal {day}, bukan sesi hari ini."
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
    baselines = load_baselines(baseline_day)
    ranked = screen_rows(rows, baselines, kind, mode)
    lines = [f"🔎 {kind} {mode} | {day}", f"Sumber: {source}",
             f"Diperiksa {len(rows)} saham | lolos {len(ranked)} | tampil {min(limit, len(ranked))}"]
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
    if not ranked:
        lines.append("Belum ada kandidat dengan data lengkap dan filter likuiditas yang sesuai.")
    lines.append("Filter riset, belum teruji profit bersih; cek spread dan harga eksekusi.")
    return "\n".join(lines)
