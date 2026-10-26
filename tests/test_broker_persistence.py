import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from broker_persistence import analyze_persistence, format_persistence


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'cache.db')
        with sqlite3.connect(self.path) as c:
            c.executescript('''CREATE TABLE broker_daily(trade_date TEXT,ticker TEXT,broker TEXT,side TEXT,value REAL);
            CREATE TABLE stock_summary_snapshots(trade_date TEXT);
            CREATE TABLE stock_daily(trade_date TEXT,ticker TEXT,value REAL);''')
            for day in ('05','06','07','08','09'):
                d = '2026-10-' + day
                c.execute('INSERT INTO stock_summary_snapshots VALUES(?)', (d,))
                c.execute('INSERT INTO stock_daily VALUES(?,?,?)', (d,'ABCD',1000))
                c.execute('INSERT INTO broker_daily VALUES(?,?,?,?,?)', (d,'ABCD','NI','BUY',100))
            c.execute("INSERT INTO broker_daily VALUES('2026-10-09','ABCD','NI','SELL',200)")
            c.execute("INSERT INTO broker_daily VALUES('2026-01-05','ABCD','NI','BUY',900)")
            c.execute("INSERT INTO broker_daily VALUES('2026-10-12','ABCD','NI','BUY',99999)")

    def test_asof_windows_net_days_and_recent_vs_ytd(self):
        r = analyze_persistence('ABCD', self.path, '2026-10-09')
        ni = r['brokers'][0]
        w = ni['windows'][5]
        self.assertEqual(w['buy_days'], 4)
        self.assertEqual(w['sell_days'], 1)
        self.assertEqual(w['net_value'], 300)
        self.assertEqual(w['trailing_buy_days'], 0)
        self.assertEqual(w['unknown_days'], 0)
        self.assertAlmostEqual(w['intensity_pct'], 6)
        self.assertEqual(ni['ytd_net_value'], 1200)
        self.assertEqual(ni['windows'][10]['status'], 'PARTIAL')
        self.assertIn('4/5', format_persistence('ABCD', self.path, '2026-10-09'))

    def test_absent_broker_is_unknown_and_breaks_streak(self):
        with sqlite3.connect(self.path) as c:
            c.execute("DELETE FROM broker_daily WHERE trade_date='2026-10-07'")
        w = analyze_persistence('ABCD', self.path, '2026-10-09')['brokers'][0]['windows'][5]
        self.assertEqual(w['unknown_days'], 1)
        self.assertEqual(w['buy_days'], 3)
        self.assertIsNone(w['intensity_pct'])
        self.assertEqual(w['status'], 'PARTIAL')

    def test_missing_turnover_never_uses_partial_denominator(self):
        with sqlite3.connect(self.path) as c:
            c.execute("DELETE FROM stock_daily WHERE trade_date='2026-10-06'")
        w = analyze_persistence('ABCD', self.path, '2026-10-09')['brokers'][0]['windows'][5]
        self.assertIsNone(w['intensity_pct'])

    def test_missing_db_is_not_created(self):
        path = str(Path(self.tmp.name) / 'missing.db')
        self.assertIn('belum siap', format_persistence('ABCD', path, '2026-10-09'))
        self.assertFalse(Path(path).exists())

    def test_command_routes_without_legacy_fetch(self):
        import broker_ytd_dispatch as d
        with patch.object(d.legacy, 'DB_PATH', self.path), patch.object(d.legacy, 'safe_end_date', return_value='2026-10-09'), patch.object(d, '_original_command_result', side_effect=AssertionError('legacy')):
            self.assertIn('PERSISTENSI', d.command_result('BROKER PERSIST ABCD', '123'))
            self.assertIn('Format:', d.command_result('BROKER PERSIST ABCD EXTRA', '123'))

    def test_invalid_legacy_rows_are_quarantined_and_reported(self):
        with sqlite3.connect(self.path) as c:
            c.execute(
                "INSERT INTO broker_daily VALUES"
                "('2026-07-08','ABCD','SH','SELL',-32000)"
            )
        report = analyze_persistence('ABCD', self.path, '2026-10-09')
        self.assertEqual(report['invalid_rows'], 1)
        self.assertEqual(report['invalid_dates'], ['2026-07-08'])
        self.assertEqual(report['brokers'][0]['ytd_net_value'], 1200)
        message = format_persistence('ABCD', self.path, '2026-10-09')
        self.assertIn('DATA QUALITY: 1 baris legacy dikecualikan', message)
        self.assertIn('YTD PARTIAL', message)

    def test_invalid_row_in_recent_window_marks_affected_window_partial(self):
        with sqlite3.connect(self.path) as c:
            c.execute(
                "UPDATE broker_daily SET value=-1 "
                "WHERE trade_date='2026-10-09' AND side='SELL'"
            )
        report = analyze_persistence('ABCD', self.path, '2026-10-09')
        ni = report['brokers'][0]
        self.assertEqual(ni['windows'][5]['invalid_rows'], 1)
        self.assertEqual(ni['windows'][5]['status'], 'PARTIAL/DATA QUALITY')
        self.assertIsNone(ni['windows'][5]['intensity_pct'])

    def test_invalid_lots_or_average_are_quarantined_when_columns_exist(self):
        with sqlite3.connect(self.path) as c:
            c.execute('ALTER TABLE broker_daily ADD COLUMN lots INTEGER')
            c.execute('ALTER TABLE broker_daily ADD COLUMN avg_price REAL')
            c.execute('UPDATE broker_daily SET lots=10, avg_price=100')
            c.execute(
                "UPDATE broker_daily SET lots=-1, avg_price=-100 "
                "WHERE trade_date='2026-10-09' AND side='SELL'"
            )
        report = analyze_persistence('ABCD', self.path, '2026-10-09')
        self.assertEqual(report['invalid_rows'], 1)
