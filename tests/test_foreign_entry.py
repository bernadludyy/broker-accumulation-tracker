import unittest
from unittest.mock import patch

import pandas as pd

import foreign_entry as feature
import telegram_query_bot as bot


def history():
    n = 60
    return pd.DataFrame({'date': pd.bdate_range(end='2026-10-01', periods=n),
                         'open': [100] * n, 'high': [102] * n, 'low': [99] * n,
                         'close': [101] * n, 'volume': [1000] * n,
                         'foreignBuyValue': [3000000] * n,
                         'foreignSellValue': [1000000] * n})


def entry():
    return {'score': 75, 'filter_status': 'PASS', 'filters': [], 'selected_rr': 2,
            'atr14': 2, 'close': 101, 'selected_plan': 'PULLBACK',
            'entry1_low': 99, 'entry1_high': 101, 'invalidation': 97,
            'pullback_tp1': 106, 'pullback_tp2': 110}


class ForeignTests(unittest.TestCase):
    def test_exact_values_confirm(self):
        result = feature.foreign_confirmation(history())
        self.assertTrue(result['confirmed'])
        self.assertEqual(result['net_5d'], 10000000)
        self.assertEqual(result['positive_days'], 5)

    def test_explicit_net_only_is_accepted(self):
        df = history().drop(columns=['foreignBuyValue', 'foreignSellValue'])
        df['netForeignValue'] = 1
        self.assertTrue(feature.foreign_confirmation(df)['confirmed'])

    def test_net_only_zero_is_not_replaced_or_confirmed(self):
        df = history().drop(columns=['foreignBuyValue', 'foreignSellValue'])
        df['netForeignValue'] = 0
        result = feature.foreign_confirmation(df)
        self.assertFalse(result['confirmed'])
        self.assertEqual(result['net_5d'], 0)

    def test_missing_or_estimated_data_never_confirms(self):
        df = history().drop(columns=['foreignBuyValue', 'foreignSellValue'])
        df['foreignBuyShares'] = 10000
        df['foreignSellShares'] = 0
        self.assertFalse(feature.foreign_confirmation(df)['confirmed'])
        df = history()
        df.loc[df.index[-3], 'foreignBuyValue'] = float('nan')
        self.assertFalse(feature.foreign_confirmation(df)['confirmed'])

    def test_inconsistent_net_is_rejected(self):
        df = history()
        df['netForeignValue'] = 0
        self.assertFalse(feature.foreign_confirmation(df)['confirmed'])

    def test_negative_latest_or_inconsistent_days_fail(self):
        df = history()
        df.loc[df.index[-1], 'foreignBuyValue'] = 0
        self.assertFalse(feature.foreign_confirmation(df)['confirmed'])
        df = history()
        df.loc[df.index[-5:-2], 'foreignBuyValue'] = 0
        self.assertFalse(feature.foreign_confirmation(df)['confirmed'])

    def test_invalid_numeric_fails_closed(self):
        df = history().astype({'foreignBuyValue': object})
        for value in ('inf', 'invalid'):
            df.loc[df.index[-1], 'foreignBuyValue'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                feature.foreign_confirmation(df)


class CombinedTests(unittest.TestCase):
    def evaluate(self, df=None, score=None):
        with patch.object(feature, 'load_stock_history', return_value=df if df is not None else history()) as get, \
             patch.object(feature, 'build_entry_score', return_value=score if score is not None else entry()) as build, \
             patch.object(feature.tracker, 'get_safe_end_date', return_value='2026-10-01'):
            result = feature.evaluate_foreign_entry('TEST')
        return result, get, build

    def test_all_gates_and_single_fetch(self):
        result, get, build = self.evaluate()
        self.assertTrue(result['passed'])
        get.assert_called_once()
        self.assertEqual(len(build.call_args.kwargs['history']), 60)
        self.assertIn('Zona entry', feature.format_result(result))

    def test_high_score_does_not_override_wait_or_block(self):
        for status in ('WAIT', 'BLOCK', 'READY', 'WATCH'):
            e = entry()
            e.update(score=99, filter_status=status)
            result, _, _ = self.evaluate(score=e)
            self.assertFalse(result['passed'], status)
            self.assertNotIn('Zona entry:', feature.format_result(result))

    def test_threshold_and_rr_are_hard_gates(self):
        for field, value in (('score', 69), ('selected_rr', 1.49), ('selected_rr', float('nan')), ('atr14', 0)):
            e = entry()
            e[field] = value
            self.assertFalse(self.evaluate(score=e)[0]['passed'])
        e = entry()
        e.update(score=70, selected_rr=1.5)
        self.assertTrue(self.evaluate(score=e)[0]['passed'])

    def test_bad_foreign_does_not_pass_high_score(self):
        df = history()
        df['foreignBuyValue'] = 0
        result, _, _ = self.evaluate(df=df)
        self.assertFalse(result['passed'])

    def test_stale_price_rejected_before_scoring(self):
        result, _, build = self.evaluate(df=history().iloc[:-1])
        self.assertFalse(result['passed'])
        build.assert_not_called()

    def test_future_rows_excluded_from_both_analyses(self):
        df = history()
        row = df.iloc[[-1]].copy()
        row['date'] = pd.Timestamp('2026-10-02')
        row['foreignBuyValue'] = 999999999
        result, _, build = self.evaluate(df=pd.concat([df, row], ignore_index=True))
        self.assertTrue(result['passed'])
        self.assertEqual(result['foreign']['net_5d'], 10000000)
        self.assertEqual(len(build.call_args.kwargs['history']), 60)

    def test_incomplete_history_and_invalid_ohlcv_do_not_score(self):
        df = history()
        df.loc[df.index[-1], 'volume'] = float('nan')
        for frame in (history().tail(49), df):
            result, _, build = self.evaluate(df=frame)
            self.assertFalse(result['passed'])
            build.assert_not_called()

    def test_scan_isolates_error_and_reports_it(self):
        result, _, _ = self.evaluate()
        with patch.object(feature, 'evaluate_foreign_entry', side_effect=[result, ValueError('hidden')]):
            text = feature.format_foreign_entry_scan(['TEST', 'FAIL', 'TEST'])
        self.assertIn('Diperiksa: 2 | Lolos: 1', text)
        self.assertIn('Data gagal diperiksa: FAIL', text)
        self.assertNotIn('hidden', text)

    def test_new_commands_route_without_breaking_old_foreign(self):
        with patch.object(bot, 'format_foreign_entry', return_value='new') as single:
            self.assertEqual(bot.command_result('ADRO FOREIGN ENTRY DAYTRADE', 'test'), 'new')
            single.assert_called_once_with('ADRO', 'DAYTRADE')
        with patch.object(bot, 'load_watchlist', return_value=['ADRO']), \
             patch.object(bot, 'format_foreign_entry_scan', return_value='scan') as scan:
            self.assertEqual(bot.command_result('FOREIGN ENTRY', 'test'), 'scan')
            scan.assert_called_once_with(['ADRO'], 'SWING')
        with patch.object(bot, 'format_foreign_ticker', return_value='old'):
            self.assertEqual(bot.command_result('ADRO FOREIGN 5D', 'test'), 'old')


if __name__ == '__main__':
    unittest.main()
