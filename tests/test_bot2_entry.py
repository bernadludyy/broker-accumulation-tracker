import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import patch
import bot2_entry as b
import telegram_query_bot as bot


def row(code='TEST', **kwargs):
    return dict(ticker=code, StockCode=code, Date='2026-10-01', Frequency=100,
                OpenPrice=100, Close=98, NetForeignValue=1000000, **kwargs)


class CombinedTests(unittest.TestCase):
    def test_rank_all_before_correction_filter(self):
        rows=[row(f'T{i:03}') for i in range(60)]
        for i,r in enumerate(rows):
            r['Frequency']=1000-i
            r['NetForeignValue']=1000000-i
        market=b.rank_market(rows)
        self.assertEqual(market['T059']['frequency_rank'],60)
        self.assertTrue(b.reasons_for(market['T059']))
        self.assertFalse(b.reasons_for(market['T000']))

    def test_correction_is_open_close_and_boundaries(self):
        for close,passed in [(99.7,True),(95,True),(99.8,False),(94.9,False),(101,False)]:
            r=row();r['Close']=close
            result=b.rank_market([r])['TEST']
            self.assertEqual(not b.reasons_for(result),passed)

    def test_missing_data_blocks_rank_instead_of_zero_fill(self):
        r=row();r.pop('Frequency')
        with self.assertRaises(ValueError): b.rank_market([r])

    def test_share_estimate_is_identified(self):
        r=row();r.pop('NetForeignValue');r.update(ForeignBuy=100,ForeignSell=20)
        ranked=b.rank_market([r])['TEST']
        self.assertTrue(ranked['estimated'])
        self.assertEqual(ranked['net'],80*98)

    def test_pagination_and_dates(self):
        with patch.object(b.tracker,'load_stock_summary_snapshot',return_value=None), \
             patch.object(b.tracker,'save_stock_summary_snapshot') as save, \
             patch.object(b.tracker,'fetch_stock_summary',return_value={'items':[row()], 'total':1}) as fetch:
            self.assertEqual(len(b.load_market('2026-10-01')),1)
            fetch.assert_called_once_with(date='2026-10-01',length=5000,start=0)
            save.assert_called_once()
        with patch.object(b.tracker,'load_stock_summary_snapshot',return_value=None), \
             patch.object(b.tracker,'save_stock_summary_snapshot'), \
             patch.object(b.tracker,'fetch_stock_summary',side_effect=[
            {'items':[row('TEST')], 'total':2},
            {'items':[row('NEXT')], 'total':2}
        ]) as fetch:
            self.assertEqual(len(b.load_market('2026-10-01')),2)
            self.assertEqual(fetch.call_args.kwargs['start'],1)
        with patch.object(b.tracker,'load_stock_summary_snapshot',return_value=None), \
             patch.object(b.tracker,'save_stock_summary_snapshot'), \
             patch.object(b.tracker,'fetch_stock_summary',side_effect=[
            {'items':[row()], 'total':2}, {'items':[], 'total':2}
        ]):
            with self.assertRaises(ValueError): b.load_market('2026-10-01')
        with patch.object(b.tracker,'load_stock_summary_snapshot',return_value=None), \
             patch.object(b.tracker,'save_stock_summary_snapshot'), \
             patch.object(b.tracker,'fetch_stock_summary',return_value={'items':[row()]}):
            with self.assertRaises(ValueError): b.load_market('2026-09-30')

    def test_database_snapshot_avoids_zapi_request(self):
        cached = [row()]
        with patch.object(b.tracker,'load_stock_summary_snapshot',return_value=cached), \
             patch.object(b.tracker,'fetch_stock_summary') as fetch:
            rows, source = b.load_market('2026-10-01', with_source=True)
        self.assertEqual(rows, cached)
        self.assertEqual(source, 'DATABASE SNAPSHOT')
        fetch.assert_not_called()

    def test_refresh_bypasses_cache_and_saves_fresh_response(self):
        with patch.object(b.tracker,'load_stock_summary_snapshot') as load, \
             patch.object(b.tracker,'save_stock_summary_snapshot') as save, \
             patch.object(b.tracker,'fetch_stock_summary',return_value={'items':[row()], 'total':1}):
            rows, source = b.load_market('2026-10-01', force_refresh=True, with_source=True)
        load.assert_not_called()
        save.assert_called_once_with('2026-10-01', rows)
        self.assertEqual(source, 'ZAPI FRESH + TERSIMPAN')

    def test_market_pass_does_not_override_entry_failure(self):
        result={'passed':False,'entry':{'score':90,'close':98},'reasons':['WAIT']}
        with patch.object(b,'load_market',return_value=([row()],'DATABASE SNAPSHOT')), \
             patch.object(b,'evaluate_foreign_entry',return_value=result), \
             patch.object(b.tracker,'get_safe_end_date',return_value='2026-10-01'):
            text=b.format_bot2_entry()
        self.assertIn('Lolos gabungan: 0',text)
        self.assertIn('Belum lolos entry: 1',text)

    def test_price_mismatch_blocks_otherwise_passing_entry(self):
        result={'passed':True,'entry':{'score':90,'close':99},'reasons':[]}
        with patch.object(b,'load_market',return_value=([row()],'DATABASE SNAPSHOT')), \
             patch.object(b,'evaluate_foreign_entry',return_value=result):
            text=b.format_bot2_entry()
        self.assertIn('Lolos gabungan: 0',text)

    def test_offhours_market_uses_latest_complete_daily_snapshot_without_live_fetch(self):
        saturday = datetime(
            2026, 10, 10, 1, 18,
            tzinfo=ZoneInfo('Asia/Jakarta'),
        )
        cached = [
            dict(row('AAAA'), Date='2026-10-09'),
            dict(row('BBBB'), Date='2026-10-09'),
        ]

        with patch.object(
            b.tracker,
            'latest_stock_summary_snapshot_date',
            return_value='2026-10-09',
        ), patch.object(
            b.tracker,
            'load_stock_summary_snapshot',
            return_value=cached,
        ) as load_snapshot, patch.object(
            b.tracker,
            'peek_shared_stock_summary',
            return_value=None,
        ), patch.object(
            b.tracker,
            'fetch_stock_summary',
        ) as live_fetch:
            rows, day, source = b.load_shared_live_market(
                with_source=True,
                now=saturday,
            )

        self.assertEqual(day, '2026-10-09')
        self.assertEqual(rows, cached)
        self.assertIn('LATEST DAILY DATABASE SNAPSHOT', source)
        load_snapshot.assert_called_once_with('2026-10-09')
        live_fetch.assert_not_called()

    def test_offhours_market_reuses_stale_neon_full_market_before_zapi(self):
        saturday = datetime(
            2026, 10, 10, 1, 18,
            tzinfo=ZoneInfo('Asia/Jakarta'),
        )
        shared_rows = [
            dict(row('AAAA'), Date='2026-10-08'),
            dict(row('BBBB'), Date='2026-10-08'),
        ]

        with patch.object(
            b.tracker,
            'latest_stock_summary_snapshot_date',
            return_value=None,
        ), patch.object(
            b.tracker,
            'peek_shared_stock_summary',
            return_value={'data': shared_rows},
        ), patch.object(
            b.tracker,
            'save_stock_summary_snapshot',
        ) as save_snapshot, patch.object(
            b,
            'load_market',
        ) as load_market:
            rows, day, source = b.load_shared_live_market(
                with_source=True,
                now=saturday,
            )

        self.assertEqual(day, '2026-10-08')
        self.assertEqual(rows, shared_rows)
        self.assertIn('NEON SHARED LAST MARKET SNAPSHOT', source)
        save_snapshot.assert_called_once()
        load_market.assert_not_called()

    def test_offhours_market_calls_historical_zapi_only_when_no_recent_cache_exists(self):
        saturday = datetime(
            2026, 10, 10, 1, 18,
            tzinfo=ZoneInfo('Asia/Jakarta'),
        )
        completed = [
            dict(row('AAAA'), Date='2026-10-09'),
            dict(row('BBBB'), Date='2026-10-09'),
        ]

        with patch.object(
            b.tracker,
            'latest_stock_summary_snapshot_date',
            return_value=None,
        ), patch.object(
            b.tracker,
            'peek_shared_stock_summary',
            return_value=None,
        ), patch.object(
            b,
            'load_market',
            return_value=(completed, 'ZAPI FRESH + TERSIMPAN'),
        ) as load_market:
            rows, day, source = b.load_shared_live_market(
                with_source=True,
                now=saturday,
            )

        self.assertEqual(day, '2026-10-09')
        self.assertEqual(rows, completed)
        self.assertIn('OFF-HOURS 2026-10-09', source)
        load_market.assert_called_once_with(
            '2026-10-09',
            force_refresh=False,
            with_source=True,
        )

    def test_trading_day_still_uses_shared_live_stock_summary(self):
        friday = datetime(
            2026, 10, 9, 10, 5,
            tzinfo=ZoneInfo('Asia/Jakarta'),
        )
        live_rows = [
            dict(row('AAAA'), Date='2026-10-09'),
            dict(row('BBBB'), Date='2026-10-09'),
        ]

        with patch.object(
            b.tracker,
            'fetch_stock_summary',
            return_value={'data': live_rows},
        ) as live_fetch, patch.object(
            b.tracker,
            'load_stock_summary_snapshot',
        ) as load_snapshot:
            rows, day, source = b.load_shared_live_market(
                with_source=True,
                now=friday,
            )

        self.assertEqual(day, '2026-10-09')
        self.assertEqual(len(rows), 2)
        self.assertIn('SHARED LIVE', source)
        live_fetch.assert_called_once_with(length=5000, start=0)
        load_snapshot.assert_not_called()

    def test_shared_market_views_use_shared_stock_summary(self):
        a = row('AAAA')
        a['Frequency'] = 500
        a['NetForeignValue'] = 5_000_000
        b_row = row('BBBB')
        b_row['Frequency'] = 300
        b_row['NetForeignValue'] = 3_000_000
        rows = [a, b_row]
        with patch.object(
            b,
            'load_shared_live_market',
            return_value=(
                rows,
                '2026-10-01',
                'PROJECT FABRIC SHARED LIVE STOCK-SUMMARY',
            ),
        ):
            text = b.format_bot2_market_command('FREQ', limit=2)

        self.assertIn('TOP FREQUENCY IDX', text)
        self.assertIn('AAAA', text)
        self.assertIn('Freq #1', text)

    def test_market_confluence_and_ticker_view(self):
        a = row('AAAA')
        a['Frequency'] = 500
        a['NetForeignValue'] = 5_000_000
        b_row = row('BBBB')
        b_row['Frequency'] = 300
        b_row['NetForeignValue'] = 3_000_000
        rows = [a, b_row]
        with patch.object(
            b,
            'load_shared_live_market',
            return_value=(rows, '2026-10-01', 'PROJECT FABRIC SHARED STOCK-SUMMARY'),
        ):
            market_text = b.format_bot2_market_command('MARKET', limit=10)
            ticker_text = b.format_bot2_market_command('MARKET', ticker='AAAA')

        self.assertIn('BOT2 MARKET CONFLUENCE', market_text)
        self.assertIn('AAAA', market_text)
        self.assertIn('BOT2 DATA — AAAA', ticker_text)
        self.assertIn('Status prefilter: LOLOS', ticker_text)

    def test_command_routes(self):
        with patch.object(bot,'format_bot2_entry',return_value='ok') as mock:
            self.assertEqual(bot.command_result('FOREIGN ENTRY BOT2','123'),'ok')
            mock.assert_called_once_with(mode='SWING')
        with patch.object(bot,'format_bot2_entry',return_value='ok') as mock:
            self.assertEqual(bot.command_result('ADRO FOREIGN ENTRY BOT2','123'),'ok')
            mock.assert_called_once_with('ADRO','SWING')
        with patch.object(bot,'format_bot2_entry',return_value='ok') as mock:
            self.assertEqual(bot.command_result('FOREIGN ENTRY BOT2 REFRESH','123'),'ok')
            mock.assert_called_once_with(mode='SWING', force_refresh=True)
        with patch.object(bot,'format_bot2_entry',return_value='ok') as mock:
            self.assertEqual(bot.command_result('ADRO FOREIGN ENTRY BOT2 REFRESH','123'),'ok')
            mock.assert_called_once_with('ADRO','SWING', force_refresh=True)
        with patch.object(bot,'format_bot2_market_command',return_value='market') as mock:
            self.assertEqual(bot.command_result('TOP FREQ 20','123'),'market')
            mock.assert_called_once_with('FREQ', limit=20)
        with patch.object(bot,'format_bot2_market_command',return_value='foreign') as mock:
            self.assertEqual(bot.command_result('NET FOREIGN 15','123'),'foreign')
            mock.assert_called_once_with('FOREIGN', limit=15)
        with patch.object(bot,'format_bot2_market_command',return_value='correction') as mock:
            self.assertEqual(bot.command_result('KOREKSI 12','123'),'correction')
            mock.assert_called_once_with('KOREKSI', limit=12)
        with patch.object(bot,'format_bot2_market_command',return_value='confluence') as mock:
            self.assertEqual(bot.command_result('BOT2 MARKET 10','123'),'confluence')
            mock.assert_called_once_with('MARKET', limit=10)
        with patch.object(bot,'format_bot2_market_command',return_value='ticker') as mock:
            self.assertEqual(bot.command_result('ADRO BOT2 DATA','123'),'ticker')
            mock.assert_called_once_with('MARKET', ticker='ADRO')

if __name__=='__main__': unittest.main()
