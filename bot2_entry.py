"""Bot 2 market ranks/correction AND Bot 1 foreign entry gates."""
from datetime import datetime
from zoneinfo import ZoneInfo
import math
import tracker
from foreign_entry import _number, evaluate_foreign_entry, format_result


def load_market(day, force_refresh=False, with_source=False):
    if not force_refresh:
        cached = tracker.load_stock_summary_snapshot(day)
        if cached is not None:
            return (cached, 'DATABASE SNAPSHOT') if with_source else cached

    rows, seen = [], set()
    page_size = 5000  # Zapi stock-summary maximum; usually the entire market fits in one call.

    def finish():
        try:
            tracker.save_stock_summary_snapshot(day, rows)
        except Exception as exc:
            raise ValueError('Universe valid tetapi gagal disimpan ke database.') from exc
        return (rows, 'ZAPI FRESH + TERSIMPAN') if with_source else rows

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
            return finish()
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
            normalized = dict(row, ticker=code)
            normalized.setdefault('Date', str(date)[:10])
            rows.append(normalized)
        # Prefer the API's total. Without it, a short page ends pagination.
        if total is not None and len(rows) >= total:
            return finish()
        if total is None and len(batch) < page_size:
            return finish()
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


def _market_rows_from_payload(payload):
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get('items')
        if not isinstance(rows, list):
            rows = payload.get('data')
    else:
        rows = None

    if not isinstance(rows, list):
        raise ValueError('Daftar stock-summary tidak valid.')

    normalized = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(
            row.get('StockCode')
            or row.get('stockCode')
            or row.get('Code')
            or row.get('code')
            or ''
        ).strip().upper()
        if not code:
            continue
        normalized.append(dict(row, ticker=code))

    if not normalized:
        raise ValueError('Stock-summary kosong.')
    return normalized


def load_shared_live_market(with_source=False):
    """Read the common Project Fabric stock-summary snapshot.

    tracker.fetch_stock_summary is cache-first through Neon shared-v1 and only
    refreshes ZAPI when the shared snapshot is stale or missing.
    """
    payload = tracker.fetch_stock_summary(length=5000, start=0)
    rows = _market_rows_from_payload(payload)

    trade_dates = sorted({
        str(
            row.get('Date')
            or row.get('date')
            or row.get('TradingDate')
            or ''
        )[:10]
        for row in rows
        if (
            row.get('Date')
            or row.get('date')
            or row.get('TradingDate')
        )
    })
    day = trade_dates[-1] if trade_dates else datetime.now(ZoneInfo('Asia/Jakarta')).date().isoformat()
    source = 'PROJECT FABRIC SHARED STOCK-SUMMARY'
    return (rows, day, source) if with_source else rows


def _compact_rupiah(value):
    value = float(value or 0)
    sign = '+' if value > 0 else '-' if value < 0 else ''
    amount = abs(value)
    if amount >= 1_000_000_000_000:
        return f'Rp {sign}{amount / 1_000_000_000_000:.2f} T'
    if amount >= 1_000_000_000:
        return f'Rp {sign}{amount / 1_000_000_000:.2f} B'
    if amount >= 1_000_000:
        return f'Rp {sign}{amount / 1_000_000:.2f} M'
    if amount >= 1_000:
        return f'Rp {sign}{amount / 1_000:.2f} K'
    return f'Rp {sign}{amount:,.0f}'


def _limit(value, default=10, maximum=50):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(maximum, parsed))


def _correction_text(row):
    value = row.get('correction')
    return f'{value:+.2f}%' if value is not None else 'N/A'


def _estimated_note(rows):
    if any(bool(row.get('estimated')) for row in rows):
        return (
            'Catatan: Net Foreign Value diturunkan dari '
            '(Foreign Buy Shares - Foreign Sell Shares) × Close '
            'jika stock-summary tidak menyediakan value langsung.'
        )
    return None


def format_bot2_market_command(kind='MARKET', limit=10, ticker=None):
    """Expose Bot 2 market ranks as Bot 1 Telegram commands."""
    try:
        rows, day, source = load_shared_live_market(with_source=True)
        market = rank_market(rows)
    except Exception as exc:
        detail = str(exc) if type(exc) is ValueError else 'Shared market data gagal; coba lagi.'
        return '⏳ BOT2 MARKET DATA — DATA BELUM VALID\n' + detail

    if ticker:
        code = ticker.upper().strip()
        row = market.get(code)
        if row is None:
            return f'❌ {code} tidak ditemukan pada stock-summary {day}.'
        lines = [
            f'📊 BOT2 DATA — {code}',
            f'Data: {day} | {source}',
            f'Close: {row["close"]:,.0f}' if row.get('close') is not None else 'Close: N/A',
            f'Koreksi open→close/current: {_correction_text(row)}',
            f'Frequency: #{row["frequency_rank"]} | {row["frequency"]:,.0f}',
            f'Net Foreign: #{row["foreign_rank"]} | {_compact_rupiah(row["net"])}',
            (
                'Status prefilter: LOLOS ✅'
                if not reasons_for(row)
                else 'Status prefilter: BELUM LOLOS ⏳'
            ),
        ]
        reasons = reasons_for(row)
        if reasons:
            lines.extend(reasons)
        note = _estimated_note([row])
        if note:
            lines += ['', note]
        return '\n'.join(lines)

    kind = str(kind or 'MARKET').upper().strip()
    max_limit = 30 if kind in {'FOREIGN', 'NET_FOREIGN'} else 50
    shown_limit = _limit(limit, maximum=max_limit)

    values = list(market.values())
    title = ''
    if kind in {'FREQ', 'FREQUENCY'}:
        selected = sorted(
            [row for row in values if row['frequency'] > 0],
            key=lambda row: (row['frequency_rank'], row['ticker']),
        )[:shown_limit]
        title = f'🔥 TOP FREQUENCY IDX — TOP {shown_limit}'
    elif kind in {'FOREIGN', 'NET_FOREIGN'}:
        selected = sorted(
            [row for row in values if row['net'] > 0],
            key=lambda row: (row['foreign_rank'], row['ticker']),
        )[:shown_limit]
        title = f'🌍 TOP NET FOREIGN BUY VALUE — TOP {shown_limit}'
    elif kind in {'CORRECTION', 'KOREKSI'}:
        selected = sorted(
            [
                row for row in values
                if row.get('correction') is not None
                and -5.000000001 <= row['correction'] <= -0.299999999
            ],
            key=lambda row: (row['correction'], row['frequency_rank'], row['ticker']),
        )[:shown_limit]
        title = f'📉 HARGA KOREKSI -0,3% s.d. -5% — TOP {shown_limit}'
    else:
        selected = sorted(
            [row for row in values if not reasons_for(row)],
            key=lambda row: (
                row['frequency_rank'] + row['foreign_rank'],
                row['frequency_rank'],
                row['ticker'],
            ),
        )[:shown_limit]
        title = f'🎯 BOT2 MARKET CONFLUENCE — TOP {shown_limit}'

    lines = [
        title,
        f'Data: {day} | {source}',
        f'Universe: {len(market)} saham',
        '',
    ]

    if not selected:
        lines.append('Belum ada saham yang memenuhi tampilan ini.')
        return '\n'.join(lines)

    for row in selected:
        lines.append(
            f'#{len(lines)-3} {row["ticker"]} | '
            f'Freq #{row["frequency_rank"]} ({row["frequency"]:,.0f}) | '
            f'Foreign #{row["foreign_rank"]} ({_compact_rupiah(row["net"])}) | '
            f'Koreksi {_correction_text(row)} | '
            f'Close {row["close"]:,.0f}' if row.get('close') is not None
            else (
                f'#{len(lines)-3} {row["ticker"]} | '
                f'Freq #{row["frequency_rank"]} | Foreign #{row["foreign_rank"]} | '
                f'Koreksi {_correction_text(row)} | Close N/A'
            )
        )

    note = _estimated_note(selected)
    if note:
        lines += ['', note]

    return '\n'.join(lines)


def format_bot2_entry(ticker=None, mode='SWING', force_refresh=False):
    day = tracker.get_safe_end_date(datetime.now(ZoneInfo('Asia/Jakarta')).date().isoformat())
    try:
        market_rows, universe_source = load_market(
            day,
            force_refresh=force_refresh,
            with_source=True,
        )
        market = rank_market(market_rows)
    except Exception as exc:
        # Expected validation messages are ours; upstream errors can contain URLs.
        detail = str(exc) if type(exc) is ValueError else 'API universe gagal; coba lagi.'
        return '⏳ FOREIGN ENTRY BOT2 — DATA BELUM VALID\n' + detail
    header = [f'🌍 FOREIGN ENTRY BOT2 — {day} | {mode}',
              f'Universe: {len(market)} saham | {universe_source}',
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
