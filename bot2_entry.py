"""Bot 2 market ranks/correction AND Bot 1 foreign entry gates."""
from datetime import datetime
from zoneinfo import ZoneInfo
import math
import tracker
from foreign_entry import _number, evaluate_foreign_entry, format_result


def load_market(day):
    rows, seen = [], set()
    page_size = 5000  # Zapi stock-summary maximum; usually the entire market fits in one call.
    for page in range(20):
        payload = tracker.fetch_stock_summary(date=day, length=page_size, start=len(rows))
        if isinstance(payload, list):
            batch, stamp = payload, None
        elif isinstance(payload, dict):
            batch = payload.get('items', payload.get('data', []))
            stamp = payload.get('date') or payload.get('Date')
        else:
            raise ValueError('Respons universe tidak valid.')
        if not isinstance(batch, list):
            raise ValueError('Daftar universe tidak valid.')
        total = payload.get('total') if isinstance(payload, dict) else None
        if total is not None:
            try:
                total = int(total)
            except (ValueError, TypeError):
                raise ValueError('Total universe tidak valid.') from None
            if total < 1 or total < len(rows) + len(batch):
                raise ValueError('Total universe tidak konsisten.')
        if not batch:
            if not rows:
                raise ValueError('Universe pasar kosong.')
            if total is not None and len(rows) < total:
                raise ValueError('Pagination berhenti sebelum seluruh universe diterima.')
            return rows
        for row in batch:
            if not isinstance(row, dict):
                raise ValueError('Baris universe tidak valid.')
            code = str(row.get('StockCode') or row.get('stockCode') or row.get('code') or '').strip().upper()
            date = row.get('Date') or row.get('date') or row.get('TradingDate') or stamp
            if not date or str(date)[:10] != day:
                raise ValueError('Tanggal universe belum sesuai tanggal analisis.')
            if not code or code in seen:
                raise ValueError('Ticker kosong/duplikat; pagination universe tidak lengkap.')
            seen.add(code)
            rows.append(dict(row, ticker=code))
        # Prefer the API's total. Without it, a short page ends pagination.
        if total is not None and len(rows) >= total:
            return rows
        if total is None and len(batch) < page_size:
            return rows
    raise ValueError('Batas pagination tercapai; ranking pasar tidak diterbitkan.')


def rank_market(rows):
    market = {}
    for row in rows:
        code = row['ticker']
        frequency = _number(row, 'Frequency', 'frequency')
        close = _number(row, 'Close', 'close')
        opening = _number(row, 'OpenPrice', 'Open', 'open')
        net = _number(row, 'netForeignValue', 'NetForeignValue')
        buy = _number(row, 'foreignBuyValue', 'ForeignBuyValue')
        sell = _number(row, 'foreignSellValue', 'ForeignSellValue')
        estimated = False
        if buy is not None and sell is not None:
            if min(buy, sell) < 0 or (net is not None and not math.isclose(net, buy-sell, rel_tol=1e-6, abs_tol=1)):
                raise ValueError('Foreign value universe tidak konsisten.')
            net = buy-sell
        if net is None:
            buy = _number(row, 'ForeignBuy', 'foreignBuy', 'ForeignBuyShares', 'foreignBuyShares')
            sell = _number(row, 'ForeignSell', 'foreignSell', 'ForeignSellShares', 'foreignSellShares')
            if buy is not None and sell is not None and close is not None and close > 0 and min(buy,sell) >= 0:
                net, estimated = (buy-sell)*close, True
        if frequency is None or frequency < 0 or frequency != int(frequency) or net is None:
            raise ValueError('Frequency/foreign universe tidak lengkap; ranking ditahan.')
        correction = (close/opening-1)*100 if close is not None and close > 0 and opening is not None and opening > 0 else None
        market[code] = dict(ticker=code, frequency=frequency, net=net, estimated=estimated,
                            correction=correction, close=close, opening=opening)
    # Deterministic ordinal ranks: ties break alphabetically, exactly top N.
    for rank, item in enumerate(sorted(market.values(), key=lambda x:(-x['frequency'],x['ticker'])),1):
        item['frequency_rank'] = rank
    for rank, item in enumerate(sorted(market.values(), key=lambda x:(-x['net'],x['ticker'])),1):
        item['foreign_rank'] = rank
    return market


def reasons_for(row):
    if row is None:
        return ['Ticker tidak ditemukan dalam universe tanggal tersebut.']
    reasons = []
    if row['frequency_rank'] > 50 or row['frequency'] <= 0:
        reasons.append('Tidak masuk Top 50 frequency positif.')
    if row['foreign_rank'] > 30 or row['net'] <= 0:
        reasons.append('Tidak masuk Top 30 net foreign buy positif.')
    correction = row['correction']
    if correction is None or not -5.000000001 <= correction <= -0.299999999:
        reasons.append('Koreksi open→close harus -5% sampai -0,3%.')
    return reasons


def rank_text(row):
    return (f'Frequency #{row["frequency_rank"]} ({row["frequency"]:,.0f}) | '
            f'Foreign #{row["foreign_rank"]} (Rp {row["net"]:+,.0f})\n'
            f'Koreksi open→close: {row["correction"]:+.2f}%' if row['correction'] is not None
            else f'Frequency #{row["frequency_rank"]} | Foreign #{row["foreign_rank"]} | OHLC tidak lengkap')


def format_bot2_entry(ticker=None, mode='SWING'):
    day = tracker.get_safe_end_date(datetime.now(ZoneInfo('Asia/Jakarta')).date().isoformat())
    try:
        market = rank_market(load_market(day))
    except Exception as exc:
        # Expected validation messages are ours; upstream errors can contain URLs.
        detail = str(exc) if type(exc) is ValueError else 'API universe gagal; coba lagi.'
        return '⏳ FOREIGN ENTRY BOT2 — DATA BELUM VALID\n' + detail
    header = [f'🌍 FOREIGN ENTRY BOT2 — {day} | {mode}', f'Universe API: {len(market)} saham',
              'Top 50 frequency ∩ Top 30 foreign ∩ koreksi open→close -0,3% s.d. -5%.']
    if any(row['estimated'] for row in market.values()):
        header += ['Ranking foreign mencakup estimasi (net shares × close); konfirmasi foreign 5D tetap wajib nilai aktual.']
    targets = [ticker.upper()] if ticker else [code for code, row in market.items() if not reasons_for(row)]
    passed, failed, rejected = [], [], 0
    for code in targets:
        row = market.get(code)
        reasons = reasons_for(row)
        if reasons:
            return '\n'.join(header + [f'⏳ {code} BELUM LOLOS', *reasons] + ([rank_text(row)] if row else []))
        try:
            result = evaluate_foreign_entry(code, mode, as_of=day)
            # Price snapshots must agree as well as dates.
            if result['entry'] and not math.isclose(result['entry']['close'],row['close'],abs_tol=1e-8,rel_tol=1e-8):
                result['passed'] = False
                result['reasons'].append('Harga summary berbeda dari stock-history; tunggu sinkronisasi.')
            if ticker:
                return '\n'.join(header + [rank_text(row), format_result(result)])
            if result['passed']:
                passed.append((row,result))
            else:
                rejected += 1
        except Exception:
            failed.append(code)
    passed.sort(key=lambda item:(-item[1]['entry']['score'],item[0]['ticker']))
    header += [f'Lolos Bot2: {len(targets)} | Lolos gabungan: {len(passed)} | Belum lolos entry: {rejected} | Error: {len(failed)}']
    for row,result in passed[:10]:
        header += ['',rank_text(row),format_result(result)]
    if not passed:
        header += ['Belum ada saham lolos seluruh filter.']
    if len(passed)>10:
        header += ['Ditampilkan 10 skor tertinggi.']
    if failed:
        header += ['Gagal diperiksa: '+', '.join(failed)]
    return '\n'.join(header)
