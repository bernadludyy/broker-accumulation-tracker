import copy
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
import requests
import bot_io
import tracker
import entry_engine


def snapshot(day='2026-09-30', buyer='AA'):
    return {'code': 'TEST', 'startDate': day, 'endDate': day,
            'buyers': [{'broker': buyer, 'lots': 100, 'value': 1000000, 'averagePrice': 100}],
            'sellers': [{'broker': 'ZZ', 'lots': 50, 'value': 500000, 'averagePrice': 100}]}


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / 'test.db')
        tracker.init_db(self.db)
        tracker.save_broker_snapshot(snapshot(), self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self):
        with sqlite3.connect(self.db) as con:
            return con.execute('SELECT broker,side,lots FROM broker_daily ORDER BY broker').fetchall()

    def test_refresh_removes_old_top_broker_and_is_idempotent(self):
        tracker.save_broker_snapshot(snapshot(buyer='BB'), self.db)
        tracker.save_broker_snapshot(snapshot(buyer='BB'), self.db)
        self.assertEqual(self.rows(), [('BB', 'BUY', 100), ('ZZ', 'SELL', 50)])

    def test_empty_response_preserves_existing(self):
        before = self.rows()
        self.assertEqual(tracker.save_broker_snapshot({'buyers': [], 'sellers': []}, self.db), 0)
        self.assertEqual(self.rows(), before)

    def test_bad_snapshot_never_partially_overwrites(self):
        before = self.rows()
        for bad in ('nan', 'inf', -1, None, 'abc', 0.5):
            p = snapshot(buyer='BB')
            p['sellers'][0]['lots'] = bad
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                tracker.save_broker_snapshot(p, self.db)
            self.assertEqual(self.rows(), before)

    def test_rejects_missing_side_duplicate_and_date_range(self):
        for mutation in ('side', 'duplicate', 'range'):
            p = snapshot()
            if mutation == 'side': del p['sellers']
            elif mutation == 'duplicate': p['buyers'] *= 2
            else: p['startDate'] = '2026-09-01'
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                tracker.save_broker_snapshot(p, self.db)

    def test_transaction_rolls_back_delete_on_insert_failure(self):
        before = self.rows()
        with sqlite3.connect(self.db) as con:
            con.execute("CREATE TRIGGER fail_insert BEFORE INSERT ON broker_daily BEGIN SELECT RAISE(ABORT, 'failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            tracker.save_broker_snapshot(snapshot(buyer='BB'), self.db)
        self.assertEqual(self.rows(), before)

    def test_flow_cannot_use_future_dates(self):
        tracker.save_broker_snapshot(snapshot(day='2026-10-01'), self.db)
        with patch.object(tracker, 'DB_PATH', self.db):
            result = entry_engine.get_broker_flow('TEST', 1, '2026-09-30')
        self.assertEqual(result['end_date'], '2026-09-30')
        self.assertEqual(result['net_lots'], 50)
        self.assertEqual(result['data_status'], 'COMPLETE')

    def test_stale_and_short_history_do_not_get_broker_points(self):
        with patch.object(tracker, 'DB_PATH', self.db):
            short = entry_engine.get_broker_flow('TEST', 5, '2026-09-30')
            stale = entry_engine.get_broker_flow('TEST', 1, '2026-10-01')
            missing = entry_engine.get_broker_flow('NONE', 1, '2026-10-01')
        self.assertEqual(short['actual_days'], 1)
        for result, status in ((short, 'INCOMPLETE'), (stale, 'STALE'), (missing, 'NO DATA')):
            self.assertEqual(result['data_status'], status)
            self.assertEqual(entry_engine._broker_flow_points(result['flow'], 15), 0)


class IOTests(unittest.TestCase):
    def response(self, status=200, payload=None):
        r = Mock(status_code=status, headers={}, ok=status < 400)
        r.json.return_value = payload if payload is not None else {'data': {'items': []}}
        if status >= 400: r.raise_for_status.side_effect = requests.HTTPError(str(status))
        return r

    @patch('bot_io.time.sleep')
    @patch('bot_io.requests.get')
    def test_retries_rate_limit_then_success(self, get, sleep):
        r = self.response(429)
        r.headers = {'Retry-After': '5'}
        get.side_effect = [r, self.response()]
        self.assertEqual(bot_io.get_json('https://example.test', headers={}, params={}), {'items': []})
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once_with(5)

    @patch('bot_io.time.sleep')
    @patch('bot_io.requests.get')
    def test_auth_error_is_not_retried(self, get, sleep):
        get.return_value = self.response(401)
        with self.assertRaises(requests.HTTPError):
            bot_io.get_json('https://example.test', headers={}, params={})
        self.assertEqual(get.call_count, 1)
        sleep.assert_not_called()

    @patch('bot_io.time.sleep')
    @patch('bot_io.requests.get', side_effect=requests.Timeout)
    def test_timeout_attempts_are_bounded(self, get, sleep):
        with self.assertRaises(requests.Timeout):
            bot_io.get_json('https://example.test', headers={}, params={})
        self.assertEqual(get.call_count, 3)

    @patch('bot_io.requests.get')
    def test_application_failure_is_not_data(self, get):
        get.return_value = self.response(payload={'success': False, 'data': {}})
        with self.assertRaises(ValueError):
            bot_io.get_json('https://example.test', headers={}, params={})

    def test_long_line_emoji_and_newlines_preserved(self):
        original = '🚀' * 4000 + '\n' + 'a' * 8000 + '\n'
        chunks = bot_io.split_message(original)
        self.assertEqual(''.join(chunks), original)
        self.assertTrue(all(len(x.encode('utf-16-le')) // 2 <= 3500 for x in chunks))

    @patch('bot_io.requests.post')
    def test_delivery_checks_ok_for_every_chunk(self, post):
        post.side_effect = [self.response(payload={'ok': True}), self.response(payload={'ok': False})]
        with self.assertRaises(RuntimeError):
            bot_io.send_message('test-token', '123', 'a' * 4000)
        self.assertEqual(post.call_count, 2)

    @patch('bot_io.requests.post')
    def test_http_error_omits_token(self, post):
        post.return_value = self.response(400)
        with self.assertRaises(RuntimeError) as error:
            bot_io.send_message('private-token', '123', 'message')
        self.assertNotIn('private-token', str(error.exception))
        self.assertEqual(post.call_count, 1)


class EntryTests(unittest.TestCase):
    def history(self):
        prices = [100 + i * .2 for i in range(59)] + [116]
        return pd.DataFrame({'date': pd.date_range('2026-07-01', periods=60),
                             'open': prices, 'high': [p + 1 for p in prices],
                             'low': [p - 1 for p in prices], 'close': prices,
                             'volume': [1000]*59 + [3000]})

    def test_breakout_wait_gate_and_single_history_request(self):
        bad_flow = {'available': True, 'days': 20, 'actual_days': 1,
                    'flow': 'STALE', 'data_status': 'STALE', 'end_date': '2026-07-01'}
        with patch.object(entry_engine, 'load_stock_history', return_value=self.history()) as fetch, \
             patch.object(entry_engine, 'get_broker_flow', return_value=bad_flow):
            result = entry_engine.build_entry_score('TEST')
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(result['breakout_status'], 'BREAKOUT CONFIRMED')
        self.assertEqual(result['filter_status'], 'WAIT')
        self.assertTrue(any('STALE' in f for f in result['filters']))

    def test_other_wait_filters_block_breakout_even_with_high_score(self):
        flow = {'available': True, 'days': 20, 'actual_days': 20,
                'flow': 'ACCUMULATION CONFIRMED', 'data_status': 'COMPLETE', 'end_date': '2026-08-29'}
        with patch.object(entry_engine, 'load_stock_history', return_value=self.history()), \
             patch.object(entry_engine, 'get_broker_flow', return_value=flow):
            result = entry_engine.build_entry_score('TEST')
        self.assertGreaterEqual(result['score'], 65)
        self.assertGreaterEqual(result['selected_rr'], 1.5)
        self.assertGreaterEqual(result['extension_atr'], 2)
        self.assertEqual(result['filter_status'], 'WAIT')

    def test_weekend_and_future_dates_are_clamped(self):
        with patch.object(tracker, 'datetime') as clock:
            clock.now.return_value = datetime(2026, 10, 3, 20)
            self.assertEqual(tracker.get_safe_end_date('2026-12-31'), '2026-10-02')
            self.assertEqual(tracker.get_safe_end_date('2026-10-03'), '2026-10-02')
            clock.now.return_value = datetime(2026, 10, 5, 9)
            self.assertEqual(tracker.get_safe_end_date('2026-10-05'), '2026-10-02')


if __name__ == '__main__':
    unittest.main()
