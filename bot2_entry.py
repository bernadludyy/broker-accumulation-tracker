"""Bot 2 market ranks/correction AND Bot 1 foreign entry gates."""
from datetime import datetime, timedelta
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


def _rows_trade_date(rows):
    dates = sorted({
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
    if len(dates) != 1:
        return None
    return dates[0]


def _recent_snapshot_candidate(day, rows, source, now, max_age_days=7):
    if not day or not rows:
        return None
    try:
        parsed = datetime.strptime(day, '%Y-%m-%d').date()
    except ValueError:
        return None
    age_days = (now.date() - parsed).days
    if age_days < 0 or age_days > max_age_days:
        return None
    return (day, rows, source)


def _previous_business_day(day):
    current = day - timedelta(days=1)
    while current.weekday() >= 5:
        current -= timedelta(days=1)
    return current


def _market_day_in_progress(now):
    """True from IDX opening until final close, including the lunch break."""
    if now.weekday() >= 5:
        return False
    minute = now.hour * 60 + now.minute
    return 9 * 60 <= minute <= 15 * 60 + 50


def _latest_completed_market_day(now):
    """Pick the latest completed weekday for stable off-hours ranking."""
    if now.weekday() >= 5:
        return _previous_business_day(now.date())

    minute = now.hour * 60 + now.minute
    if minute > 15 * 60 + 50:
        return now.date()
    return _previous_business_day(now.date())


def load_shared_live_market(with_source=False, now=None):
    """Load Bot 2 ranking input without forcing a live ZAPI call off-hours.

    During the trading day, use the shared Project Fabric current stock-summary.
    Before the open, after the final close, and on weekends, prefer the complete
    persisted snapshot for the latest finished trading day. If it is not yet in
    SQLite, load the date-specific ZAPI snapshot once; Project Fabric then caches
    that historical request with the longer historical TTL.
    """
    now = now or datetime.now(ZoneInfo('Asia/Jakarta'))

    if _market_day_in_progress(now):
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
        day = (
            trade_dates[-1]
            if trade_dates
            else now.date().isoformat()
        )
        source = 'PROJECT FABRIC SHARED LIVE STOCK-SUMMARY'
        return (rows, day, source) if with_source else rows

    target_day = _latest_completed_market_day(now).isoformat()
    candidates = []

    local_day = tracker.latest_stock_summary_snapshot_date()
    if local_day:
        local_rows = tracker.load_stock_summary_snapshot(local_day)
        candidate = _recent_snapshot_candidate(
            local_day,
            local_rows,
            'LATEST DAILY DATABASE SNAPSHOT',
            now,
        )
        if candidate is not None:
            candidates.append(candidate)

    shared_payload = tracker.peek_shared_stock_summary()
    if shared_payload is not None:
        try:
            shared_rows = _market_rows_from_payload(shared_payload)
        except ValueError:
            shared_rows = []
        shared_day = _rows_trade_date(shared_rows)
        candidate = _recent_snapshot_candidate(
            shared_day,
            shared_rows,
            'NEON SHARED LAST MARKET SNAPSHOT',
            now,
        )
        if candidate is not None:
            candidates.append(candidate)

    if candidates:
        day, rows, source = max(candidates, key=lambda item: item[0])
        if source.startswith('NEON'):
            try:
                tracker.save_stock_summary_snapshot(
                    day,
                    rows,
                    source='project-fabric:shared-v1:stock-summary',
                )
            except Exception as exc:
                print(
                    f"[BOT2-MARKET-SNAPSHOT-WARN] "
                    f"{type(exc).__name__}: {str(exc)[:180]}"
                )
        result = (rows, day, f'{source} | OFF-HOURS')
        return result if with_source else rows

    rows, source = load_market(
        target_day,
        force_refresh=False,
        with_source=True,
    )
    result = (
        rows,
        target_day,
        f'{source} | OFF-HOURS {target_day}',
    )
    return result if with_source else rows


def _decimal_comma(value, digits=2):
    return f'{float(value):.{int(digits)}f}'.replace('.', ',')


def _compact_rupiah(value):
    """Human-readable IDR value with unambiguous Indonesian units."""
    value = float(value or 0)
    sign = '+' if value > 0 else '-' if value < 0 else ''
    amount = abs(value)
    if amount >= 1_000_000_000_000:
        return f'Rp {sign}{_decimal_comma(amount / 1_000_000_000_000)} Triliun'
    if amount >= 1_000_000_000:
        return f'Rp {sign}{_decimal_comma(amount / 1_000_000_000)} Miliar'
    if amount >= 1_000_000:
        return f'Rp {sign}{_decimal_comma(amount / 1_000_000)} Juta'
    if amount >= 1_000:
        return f'Rp {sign}{_decimal_comma(amount / 1_000)} Ribu'
    return f'Rp {sign}{amount:,.0f}'


def _limit(value, default=10, maximum=50):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(maximum, parsed))


def _correction_text(row):
    value = row.get('correction')
    if value is None:
        return 'N/A'
    sign = '+' if value > 0 else ''
    return f'{sign}{_decimal_comma(value)}%'


def _estimated_note(rows):
    if any(bool(row.get('estimated')) for row in rows):
        return (
            '* Net Foreign Value estimasi = '
            '(Foreign Buy Shares - Foreign Sell Shares) × Close, '
            'karena stock-summary tidak menyediakan value rupiah langsung.'
        )
    return None


def _foreign_value_text(row):
    text = _compact_rupiah(row.get('net'))
    return text + ('*' if row.get('estimated') else '')


def format_bot2_market_command(kind='MARKET', limit=10, ticker=None):
    """Expose Bot 2 market ranks as Bot 1 Telegram commands."""
    try:
        rows, day, source = load_shared_live_market(with_source=True)
        market = rank_market(rows)
    except Exception as exc:
        print(
            f"[BOT2-MARKET-ERROR] {type(exc).__name__}: "
            f"{str(exc)[:240]}"
        )
        detail = str(exc) if type(exc) is ValueError else 'Shared market data gagal; coba lagi.'
        return '⏳ BOT2 MARKET DATA — DATA BELUM VALID\n' + detail

    if ticker:
        code = ticker.upper().strip()
        row = market.get(code)
        if row is None:
            return f'❌ {code} tidak ditemukan pada stock-summary {day}.'
        close_text = (
            f'{row["close"]:,.0f}'
            if row.get('close') is not None
            else 'N/A'
        )
        lines = [
            f'📊 BOT2 DATA — {code}',
            f'Data: {day}',
            f'Sumber: {source}',
            '',
            f'→ Freq : #{row["frequency_rank"]} ({row["frequency"]:,.0f})',
            f'→ Net Foreign : #{row["foreign_rank"]} ({_foreign_value_text(row)})',
            f'→ Koreksi : {_correction_text(row)} (Close {close_text})',
            '',
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
        f'Data: {day}',
        f'Sumber: {source}',
        f'Universe: {len(market)} saham',
        '',
    ]

    if not selected:
        lines.append('Belum ada saham yang memenuhi tampilan ini.')
        return '\n'.join(lines)

    for index, row in enumerate(selected, 1):
        close_text = (
            f'{row["close"]:,.0f}'
            if row.get('close') is not None
            else 'N/A'
        )
        lines.extend([
            f'{index}. {row["ticker"]}',
            f'→ Freq : #{row["frequency_rank"]} ({row["frequency"]:,.0f})',
            f'→ Net Foreign : #{row["foreign_rank"]} ({_foreign_value_text(row)})',
            f'→ Koreksi : {_correction_text(row)} (Close {close_text})',
            '',
        ])

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
