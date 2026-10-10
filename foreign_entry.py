"""Foreign accumulation + existing entry gates, evaluated on one daily snapshot."""
from __future__ import annotations

import math
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

import tracker
from entry_engine import build_entry_score, load_stock_history
from universe_decision import evaluate_universe, priority_score, summary_line


def _number(row, *keys):
    for key in keys:
        value = row.get(key)
        if value is None or pd.isna(value):
            continue
        try:
            value = float(str(value).replace(',', '').strip())
        except (TypeError, ValueError):
            raise ValueError(f'Foreign field {key} bukan angka valid.') from None
        if not math.isfinite(value):
            raise ValueError(f'Foreign field {key} tidak finite.')
        return value
    return None


def foreign_confirmation(history: pd.DataFrame) -> dict:
    """Use explicit reported monetary values only; missing is never zero."""
    result = {'confirmed': False, 'net_5d': None, 'net_1d': None,
              'positive_days': 0, 'reason': 'Data foreign 5 hari belum lengkap.'}
    rows = history.tail(5)
    if len(rows) != 5:
        return result
    nets = []
    for _, row in rows.iterrows():
        buy = _number(row, 'foreignBuyValue', 'ForeignBuyValue')
        sell = _number(row, 'foreignSellValue', 'ForeignSellValue')
        net = _number(row, 'netForeignValue', 'NetForeignValue')
        if any(v is not None and v < 0 for v in (buy, sell)):
            result['reason'] = 'Foreign buy/sell value negatif; data ditolak.'
            return result
        if buy is not None and sell is not None:
            calculated = buy - sell
            if net is not None and not math.isclose(net, calculated, rel_tol=1e-6, abs_tol=1):
                result['reason'] = 'Net foreign tidak konsisten dengan buy minus sell.'
                return result
            net = calculated
        if net is None:
            result['reason'] = 'Nilai foreign aktual tidak lengkap; estimasi shares × close tidak digunakan.'
            return result
        nets.append(net)
    result.update(net_5d=sum(nets), net_1d=nets[-1], positive_days=sum(x > 0 for x in nets))
    result['confirmed'] = result['net_5d'] > 0 and result['net_1d'] > 0 and result['positive_days'] >= 3
    result['reason'] = ('ACCUMULATION CONFIRMED (aturan bot)' if result['confirmed']
                        else 'Perlu net 5D > 0, minimal 3/5 hari net buy, dan net 1D > 0.')
    return result


def _threshold():
    value = int(os.getenv('FOREIGN_ENTRY_MIN_SCORE', '70'))
    if not 0 <= value <= 100:
        raise ValueError('FOREIGN_ENTRY_MIN_SCORE harus 0–100.')
    return value


def evaluate_foreign_entry(ticker: str, mode: str = 'SWING', *, as_of: str | None = None, market_row: dict | None = None) -> dict:
    ticker, mode = ticker.strip().upper(), mode.strip().upper()
    if mode not in {'SWING', 'DAYTRADE'}:
        raise ValueError('Mode harus SWING atau DAYTRADE.')
    minimum = _threshold()
    today = datetime.now(ZoneInfo('Asia/Jakarta')).date().isoformat()
    safe_date = tracker.get_safe_end_date(as_of or today)
    history = load_stock_history(ticker, 100)
    # The same closed daily rows feed both foreign and entry scoring.
    history = history.loc[history['date'].dt.strftime('%Y-%m-%d') <= safe_date].copy()
    result = {'ticker': ticker, 'mode': mode, 'passed': False, 'minimum_score': minimum,
              'date': None, 'safe_date': safe_date, 'entry': None, 'foreign': None, 'reasons': []}
    if len(history) < 50:
        result['reasons'].append('Histori harga belum 50 hari untuk EMA50.')
        return result
    result['date'] = history.iloc[-1]['date'].date().isoformat()
    if result['date'] != safe_date:
        result['reasons'].append(f'Data harga terakhir {result["date"]}; perlu {safe_date}.')
        return result
    numeric = history[['open', 'high', 'low', 'close', 'volume']].apply(pd.to_numeric, errors='coerce')
    valid = (numeric.apply(lambda column: column.map(math.isfinite)).all().all()
             and (numeric[['open', 'high', 'low', 'close']] > 0).all().all()
             and (numeric['volume'] >= 0).all()
             and (numeric['high'] >= numeric[['open', 'close', 'low']].max(axis=1)).all()
             and (numeric['low'] <= numeric[['open', 'close', 'high']].min(axis=1)).all())
    if not valid:
        result['reasons'].append('OHLCV tidak valid; perhitungan entry ditahan.')
        return result
    foreign = foreign_confirmation(history)
    result['foreign'] = foreign
    if not foreign['confirmed']:
        result['reasons'].append(foreign['reason'])
    entry = build_entry_score(ticker, mode, history=history)
    result['entry'] = entry
    if entry['score'] < minimum:
        result['reasons'].append(f'Skor {entry["score"]}/100 < {minimum}.')
    if entry['filter_status'] != 'PASS':
        result['reasons'].append(f'Filter entry {entry["filter_status"]}: ' + '; '.join(entry['filters']))
    if not math.isfinite(entry['selected_rr']) or entry['selected_rr'] < 1.5:
        result['reasons'].append('Risk/reward TP1 harus minimal 1,50.')
    if not math.isfinite(entry['atr14']) or entry['atr14'] <= 0:
        result['reasons'].append('ATR tidak valid atau nol.')
    result['passed'] = not result['reasons']
    # Soft ranking only: missing or historical Universe signals do not defeat
    # the verified five-day foreign, entry-plan or risk/reward hard gates.
    intelligence = evaluate_universe(ticker, market_row=market_row, as_of=safe_date)
    result['universe'] = intelligence
    result['priority_score'] = priority_score(entry['score'], intelligence)
    return result


def _rupiah(value):
    return '-' if value is None else f'Rp {value:+,.0f}'


def format_result(result: dict) -> str:
    entry, foreign = result['entry'], result['foreign']
    lines = [f'🌍 FOREIGN ENTRY — {result["ticker"]} {result["mode"]}',
             f'Data harian: {result["date"] or "-"} | Acuan: {result["safe_date"]}',
             'Status: ✅ LOLOS FILTER ENTRY' if result['passed'] else 'Status: ⏳ BELUM LOLOS',
             f'Skor minimum: {result["minimum_score"]}/100']
    if foreign:
        lines += [f'Foreign: {foreign["reason"]}',
                  f'Net foreign 5D: {_rupiah(foreign["net_5d"])}',
                  f'Net foreign 1D: {_rupiah(foreign["net_1d"])}',
                  f'Hari net buy: {foreign["positive_days"]}/5']
    if entry:
        lines += [f'Skor entry: {entry["score"]}/100 | Filter: {entry["filter_status"]}',
                  f'R:R TP1: {entry["selected_rr"]:.2f} | Harga penutupan: {entry["close"]:,.0f}']
    if result.get('universe'):
        lines.append(summary_line(result['universe']))
        if entry:
            lines.append(f'Prioritas gabungan: {result["priority_score"]:.1f}/100 (bukan win rate)')
        if result['universe']['risk_high']:
            lines.append('⚠️ Perhatikan risiko distribusi; kelulusan filter dasar bukan rekomendasi beli.')
    if result['passed']:
        if entry['selected_plan'] == 'BREAKOUT':
            lines += [f'Rencana: BREAKOUT | Trigger > {entry["breakout_entry"]:,.2f}',
                      f'Stop acuan: {max(1, entry["breakout_level"] - entry["atr14"] * .5):,.2f}',
                      f'TP1 / TP2: {entry["breakout_tp1"]:,.2f} / {entry["breakout_tp2"]:,.2f}']
        else:
            lines += [f'Rencana: PULLBACK | Zona entry: {entry["entry1_low"]:,.2f}–{entry["entry1_high"]:,.2f}',
                      f'Invalidasi: < {entry["invalidation"]:,.2f}',
                      f'TP1 / TP2: {entry["pullback_tp1"]:,.2f} / {entry["pullback_tp2"]:,.2f}']
        lines += ['Level indikatif; belum dibulatkan ke fraksi harga dan R:R belum termasuk biaya.',
                  'Konfirmasi harga saat transaksi; data harian bukan sinyal real-time.']
    else:
        lines += ['Alasan:'] + [f'• {reason}' for reason in result['reasons']]
    lines += ['Konfirmasi = aturan bot, bukan jaminan harga naik.']
    return '\n'.join(lines)


def format_foreign_entry(ticker: str, mode: str = 'SWING') -> str:
    return format_result(evaluate_foreign_entry(ticker, mode))


def format_foreign_entry_scan(tickers, mode: str = 'SWING') -> str:
    tickers = list(dict.fromkeys(t.strip().upper() for t in tickers if t.strip()))
    passed, rejected, failed = [], 0, []
    for ticker in tickers:
        try:
            result = evaluate_foreign_entry(ticker, mode)
            if result['passed']:
                passed.append(result)
            else:
                rejected += 1
        except Exception:
            # Never expose token-bearing URLs/provider details to Telegram.
            failed.append(ticker)
    passed.sort(key=lambda r: (-r.get('priority_score', r['entry']['score']), r['ticker']))
    lines = [f'🌍 FOREIGN ENTRY — WATCHLIST {mode}',
             f'Diperiksa: {len(tickers)} | Lolos: {len(passed)} | Belum lolos: {rejected} | Error: {len(failed)}']
    if not tickers:
        lines.append('Watchlist kosong. Tambahkan saham melalui /add TICKER.')
    elif not passed:
        lines.append('Belum ada saham yang memenuhi seluruh filter. Tidak ada sinyal entry.')
    for result in passed[:10]:
        lines += ['', format_result(result)]
    if len(passed) > 10:
        lines.append('Ditampilkan 10 skor tertinggi; cek saham lain dengan TICKER FOREIGN ENTRY.')
    if failed:
        lines.append('Data gagal diperiksa: ' + ', '.join(failed))
    return '\n'.join(lines)
