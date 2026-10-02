import unittest
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
        with patch.object(b.tracker,'fetch_stock_summary',return_value={'items':[row()], 'total':1}) as fetch:
            self.assertEqual(len(b.load_market('2026-10-01')),1)
            fetch.assert_called_once_with(date='2026-10-01',length=5000,start=0)
        with patch.object(b.tracker,'fetch_stock_summary',side_effect=[
            {'items':[row('TEST')], 'total':2},
            {'items':[row('NEXT')], 'total':2}
        ]) as fetch:
            self.assertEqual(len(b.load_market('2026-10-01')),2)
            self.assertEqual(fetch.call_args.kwargs['start'],1)
        with patch.object(b.tracker,'fetch_stock_summary',side_effect=[
            {'items':[row()], 'total':2}, {'items':[], 'total':2}
        ]):
            with self.assertRaises(ValueError): b.load_market('2026-10-01')
        with patch.object(b.tracker,'fetch_stock_summary',return_value={'items':[row()]}):
            with self.assertRaises(ValueError): b.load_market('2026-09-30')

    def test_market_pass_does_not_override_entry_failure(self):
        result={'passed':False,'entry':{'score':90,'close':98},'reasons':['WAIT']}
        with patch.object(b,'load_market',return_value=[row()]), \
             patch.object(b,'evaluate_foreign_entry',return_value=result), \
             patch.object(b.tracker,'get_safe_end_date',return_value='2026-10-01'):
            text=b.format_bot2_entry()
        self.assertIn('Lolos gabungan: 0',text)
        self.assertIn('Belum lolos entry: 1',text)

    def test_price_mismatch_blocks_otherwise_passing_entry(self):
        result={'passed':True,'entry':{'score':90,'close':99},'reasons':[]}
        with patch.object(b,'load_market',return_value=[row()]), \
             patch.object(b,'evaluate_foreign_entry',return_value=result):
            text=b.format_bot2_entry()
        self.assertIn('Lolos gabungan: 0',text)

    def test_command_routes(self):
        with patch.object(bot,'format_bot2_entry',return_value='ok') as mock:
            self.assertEqual(bot.command_result('FOREIGN ENTRY BOT2','123'),'ok')
            mock.assert_called_once_with(mode='SWING')
        with patch.object(bot,'format_bot2_entry',return_value='ok') as mock:
            self.assertEqual(bot.command_result('ADRO FOREIGN ENTRY BOT2','123'),'ok')
            mock.assert_called_once_with('ADRO','SWING')

if __name__=='__main__': unittest.main()
