"""Cache-only research diagnostics; visible broker activity is not inventory.

Inspired by Toth et al. (2015), https://arxiv.org/abs/1108.1632.
Daily 5/10/20-window adaptation, not a replication of their intraday study.
"""
from __future__ import annotations

import math
import sqlite3
from datetime import date, timedelta

from broker_ytd_matrix import (
    BROKER_PATTERN,
    TICKER_PATTERN,
    _read_db,
    _reference_dates,
    rupiah,
)


def _finite_number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _weighted_avg_price(value: float, lots: float) -> float | None:
    if value <= 0 or lots <= 0:
        return None
    average = value / (lots * 100)
    return average if math.isfinite(average) and average > 0 else None


def _format_price(value: float | None) -> str:
    if value is None:
        return 'N/A'
    return f'Rp {value:,.2f}' if value != int(value) else f'Rp {value:,.0f}'


def analyze_persistence(ticker: str, db_path: str, asof: str) -> dict:
    ticker = ticker.strip().upper().removesuffix('.JK')
    if not TICKER_PATTERN.fullmatch(ticker):
        raise ValueError('Ticker tidak valid')
    end = date.fromisoformat(asof)
    start = (end - timedelta(days=120)).isoformat()
    with _read_db(db_path) as con:
        dates = sorted(_reference_dates(con, start, end.isoformat()))[-20:]
        if not dates:
            return dict(
                ticker=ticker,
                asof=asof,
                dates=[],
                brokers=[],
                invalid_rows=0,
                invalid_dates=[],
            )
        # One bounded cache query, including YTD only for this ticker.
        lower = min(dates[0], f'{end.year}-01-01')
        columns = {
            str(row['name'])
            for row in con.execute('PRAGMA table_info(broker_daily)')
        }
        optional = [name for name in ('lots', 'avg_price') if name in columns]
        select_columns = ','.join(
            ['trade_date', 'broker', 'side', 'value', *optional]
        )
        rows = con.execute(
            f'SELECT {select_columns} FROM broker_daily '
            'WHERE ticker=? AND trade_date BETWEEN ? AND ?',
            (ticker, lower, asof),
        ).fetchall()

        # Old databases can contain malformed rows that predate the atomic
        # snapshot validator. Quarantine the entire broker-day if one side is
        # invalid; keeping the other side alone would fabricate a daily net.
        staged: dict[tuple[str, str], float] = {}
        staged_buy_value: dict[tuple[str, str], float] = {}
        staged_buy_lots: dict[tuple[str, str], float] = {}
        invalid_keys: set[tuple[str, str]] = set()
        invalid_dates: set[str] = set()
        invalid_rows = 0
        for r in rows:
            code = str(r['broker'] or '').strip().upper()
            day = str(r['trade_date'] or '')
            side = r['side']
            amount = _finite_number(r['value'])
            lots = _finite_number(r['lots']) if 'lots' in optional else None
            average = (
                _finite_number(r['avg_price'])
                if 'avg_price' in optional and r['avg_price'] is not None
                else None
            )
            invalid = (
                not BROKER_PATTERN.fullmatch(code)
                or side not in ('BUY', 'SELL')
                or amount is None
                or amount < 0
                or ('lots' in optional and (
                    lots is None or lots < 0 or lots != int(lots)
                ))
                or ('avg_price' in optional and r['avg_price'] is not None and (
                    average is None or average < 0
                ))
            )
            if invalid:
                invalid_rows += 1
                invalid_dates.add(day)
                invalid_keys.add((code, day))
                continue
            signed = amount if r['side'] == 'BUY' else -amount
            key = (code, day)
            staged[key] = staged.get(key, 0) + signed
            if side == 'BUY':
                staged_buy_value[key] = staged_buy_value.get(key, 0) + amount
                if 'lots' in optional:
                    staged_buy_lots[key] = staged_buy_lots.get(key, 0) + lots

        daily: dict[str, dict[str, float]] = {}
        daily_buy_value: dict[str, dict[str, float]] = {}
        daily_buy_lots: dict[str, dict[str, float]] = {}
        ytd: dict[str, float] = {}
        ytd_buy_value: dict[str, float] = {}
        ytd_buy_lots: dict[str, float] = {}
        for (code, day), signed in staged.items():
            if (code, day) in invalid_keys:
                continue
            daily.setdefault(code, {})[day] = signed
            daily_buy_value.setdefault(code, {})[day] = staged_buy_value.get(
                (code, day), 0
            )
            if 'lots' in optional:
                daily_buy_lots.setdefault(code, {})[day] = staged_buy_lots.get(
                    (code, day), 0
                )
            if day >= f'{end.year}-01-01':
                ytd[code] = ytd.get(code, 0) + signed
                ytd_buy_value[code] = (
                    ytd_buy_value.get(code, 0)
                    + staged_buy_value.get((code, day), 0)
                )
                if 'lots' in optional:
                    ytd_buy_lots[code] = (
                        ytd_buy_lots.get(code, 0)
                        + staged_buy_lots.get((code, day), 0)
                    )
        turnover = {}
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='stock_daily'").fetchone():
            for r in con.execute('SELECT trade_date,value FROM stock_daily WHERE ticker=? '
                                 'AND trade_date BETWEEN ? AND ?', (ticker, dates[0], asof)):
                v = r['value']
                if v is not None and math.isfinite(float(v)) and float(v) > 0:
                    turnover[r['trade_date']] = float(v)
    brokers = []
    for code, values in daily.items():
        if not any(d in values for d in dates):
            continue
        windows = {}
        for n in (5,10,20):
            window = dates[-n:]
            seen = [values[d] for d in window if d in values]
            unknown = len(window) - len(seen)
            invalid_in_window = sum(
                (code, day) in invalid_keys for day in window
            )
            trailing = 0
            for d in reversed(window):
                if d not in values or values[d] <= 0:
                    break
                trailing += 1
            net = sum(seen)
            complete = (
                len(window) == n
                and unknown == 0
                and invalid_in_window == 0
                and dates[-1] == asof
            )
            denominator = sum(turnover.get(d, 0) for d in window)
            intensity = (100 * net / denominator if complete and
                         all(d in turnover for d in window) and denominator > 0 else None)
            buy_value = sum(daily_buy_value.get(code, {}).get(d, 0) for d in window)
            buy_lots = sum(daily_buy_lots.get(code, {}).get(d, 0) for d in window)
            windows[n] = dict(buy_days=sum(v > 0 for v in seen), sell_days=sum(v < 0 for v in seen),
                              neutral_days=sum(v == 0 for v in seen), unknown_days=unknown,
                              observed_days=len(seen), reference_days=len(window),
                              trailing_buy_days=trailing, net_value=net,
                              avg_buy_price=_weighted_avg_price(buy_value, buy_lots),
                              intensity_pct=intensity,
                              invalid_rows=invalid_in_window,
                              status=('UNVERIFIED' if complete else
                                      'PARTIAL/DATA QUALITY' if invalid_in_window else
                                      'PARTIAL'))
        brokers.append(dict(
            code=code,
            windows=windows,
            ytd_net_value=ytd.get(code, 0),
            ytd_avg_buy_price=_weighted_avg_price(
                ytd_buy_value.get(code, 0), ytd_buy_lots.get(code, 0)
            ),
            ytd_invalid_rows=sum(key[0] == code for key in invalid_keys),
        ))
    brokers.sort(key=lambda b: (-b['windows'][20]['net_value'], b['code']))
    return dict(
        ticker=ticker,
        asof=asof,
        dates=dates,
        brokers=brokers,
        invalid_rows=invalid_rows,
        invalid_dates=sorted(invalid_dates),
    )


def format_persistence(ticker: str, db_path: str, asof: str) -> str:
    try:
        report = analyze_persistence(ticker, db_path, asof)
    except sqlite3.Error:
        return 'Cache broker SQLite belum siap. Periksa /status TICKER.'
    except (ValueError, TypeError, OverflowError):
        return 'Ticker, tanggal, atau nilai cache tidak valid; analisis ditahan.'
    lines = [f"PERSISTENSI BROKER — {report['ticker']}", f'Acuan cache: {asof}']
    if not report['brokers']:
        return '\n'.join(lines + ['Data broker belum tersedia dalam 120 hari terakhir.'])
    lines.append(f"Referensi cache: {report['dates'][0]} s.d. {report['dates'][-1]}")
    if report['dates'][-1] != asof:
        lines.append('STALE: tanggal terakhir cache berbeda dari acuan.')
    if report['invalid_rows']:
        lines.append(
            f"DATA QUALITY: {report['invalid_rows']} baris legacy dikecualikan "
            f"({len(report['invalid_dates'])} tanggal); hasil YTD PARTIAL."
        )
    selected = [b for b in report['brokers'] if b['windows'][20]['net_value'] > 0][:3]
    if not selected:
        lines.append('Tidak ada broker dengan net buy terlihat positif pada jendela 20 sesi cache.')
    for b in selected:
        ytd_quality = ' | YTD PARTIAL' if b['ytd_invalid_rows'] else ''
        lines += ['', f"{b['code']} | YTD Net {rupiah(b['ytd_net_value'])} | "
                  f"Avg Buy {_format_price(b['ytd_avg_buy_price'])}{ytd_quality}"]
        for n, w in b['windows'].items():
            quality = {
                'UNVERIFIED': 'DATA OK*',
                'PARTIAL': 'DATA SEBAGIAN',
                'PARTIAL/DATA QUALITY': 'DATA BERMASALAH',
            }[w['status']]
            lines.append(
                f"{n} sesi: NB {w['buy_days']} | NS {w['sell_days']} | "
                f"Net {rupiah(w['net_value'])} | "
                f"Avg Buy {_format_price(w['avg_buy_price'])} | "
                f"Data {w['observed_days']}/{n} • {quality}"
            )
    lines += ['', 'Arti singkat:',
              'NB/NS = jumlah hari broker lebih banyak beli/jual.',
              'Avg Buy = rata-rata tertimbang harga beli yang terlihat.',
              'DATA SEBAGIAN = broker tidak terlihat di semua sesi.',
              'DATA OK* = cache penuh, tetapi sumber hanya menampilkan broker teratas.',
              'Ini aktivitas broker yang terlihat, bukan saldo kepemilikan.']
    return '\n'.join(lines)
