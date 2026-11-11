import unittest
from datetime import datetime
from unittest.mock import patch, Mock
from zoneinfo import ZoneInfo

import orderflow_tape as tape
import broker_ytd_dispatch as dispatch


def sample():
    return {'prints':[
        {'price':18,'lots':250000,'time':'11:36:50','action':'BUY','sequence':3},
        {'price':17,'lots':32500,'time':'09:25:52','action':'SELL','sequence':2},
        {'price':17,'lots':22897,'time':'09:22:51','action':'SELL','sequence':1}],
        'tradebook':{'data':{'byPrice':[
            {'price':18,'buyLots':405000,'sellLots':10000},
            {'price':17,'buyLots':0,'sellLots':117775}]}},
        'summary':{'items':[{'code':'ELTY','lastPrice':19,'open':18}]},
        'chart':{'items':[{'startTime':'2026-11-11T02:00:00Z'}]},
        'pages':1,'requests':5,'has_more':True}


class TapeTests(unittest.TestCase):
    def test_lot_to_value_and_separate_snapshot_tape(self):
        m=tape.analyze('ELTY',sample())
        self.assertEqual(m['buy'],729_000_000)
        self.assertEqual(m['sell'],218_217_500)
        self.assertEqual(m['poc'],18)
        self.assertEqual(m['vwap_buy'],18)
        self.assertAlmostEqual(m['cvd'][-1],450_000_000-55_250_000-38_924_900)
        self.assertNotEqual(m['net'],m['cvd'][-1])
        self.assertEqual(m['minutes_buy'],1)
        self.assertFalse(m['date_verified'])
        self.assertIn('PAR',tape.text_report(m))

    def test_filter_window_relative_to_last_available_print(self):
        m=tape.analyze('ELTY',sample(),window=60)
        self.assertEqual(len(m['trades']),1)
        self.assertEqual(m['basis'],'TAPE PARSIAL')
        self.assertEqual(m['buy'],450_000_000)
        self.assertEqual(m['sell'],0)

    def test_deduplicate_only_stable_sequence_and_skip_unknown_side(self):
        one={'price':100,'lots':1,'time':'09:00:00','action':'BUY'}
        items,skipped=tape.normalize_trades([one,one,{**one,'sequence':1},{**one,'sequence':1},
                                            {**one,'action':'UNKNOWN'},{**one,'board':'NG'},
                                            {**one,'price':'NaN'},{**one,'lots':-1}])
        self.assertEqual(len(items),3)
        self.assertEqual(skipped,4)

    def test_mixed_dates_never_combined_and_utc_time_is_wib(self):
        one={'price':100,'lots':1,'action':'SELL'}
        items,_=tape.normalize_trades([{**one,'time':'2026-11-10T02:00:00Z','sequence':1},
                                     {**one,'time':'2026-11-11T02:00:00Z','sequence':1}])
        self.assertEqual(len(items),1)
        self.assertEqual(items[0]['seconds'],9*3600)
        self.assertEqual(items[0]['date'],'2026-11-11')

    def test_no_trades_never_invents_cvd(self):
        payload=sample();payload['prints']=[]
        m=tape.analyze('ELTY',payload)
        self.assertEqual(m['cvd'],[])
        self.assertIn('CVD tape PARSIAL: N/A',tape.text_report(m))
        with self.assertRaises(ValueError):
            tape.analyze('ELTY',{})

    def test_pagination_bound_no_action_or_lot_filter(self):
        calls=[]
        def fetch(endpoint,params):
            calls.append((endpoint,params))
            return {'items':[{'price':18,'lots':1,'time':'09:00:00','action':'BUY'}],
                    'nextCursor':str(len(calls))}
        out=tape.fetch_payloads('ELTY',fetch)
        self.assertEqual(len(calls),7)
        self.assertEqual(out['pages'],3)
        self.assertTrue(out['has_more'])
        for endpoint,params in calls:
            if endpoint=='running-trades':
                self.assertEqual(params['minLot'],1)
                self.assertNotIn('action',params)

    def test_repeated_cursor_stops_and_partial_failure_survives(self):
        def fetch(endpoint,params):
            if endpoint=='running-trades':
                return {'items':sample()['prints'],'nextCursor':'same'}
            raise RuntimeError('secret-do-not-expose')
        out=tape.fetch_payloads('ELTY',fetch)
        self.assertEqual(out['pages'],2)
        m=tape.analyze('ELTY',out)
        self.assertNotIn('secret-do-not-expose',tape.text_report(m))
        self.assertEqual(m['basis'],'TAPE PARSIAL')

    @patch.object(dispatch.legacy,'authorized',return_value=True)
    @patch.object(dispatch,'build_tape_report',return_value='ok')
    def test_aliases_and_strict_grammar(self,build,auth):
        for text in ['/tape ELTY','/tape@Buddy ELTY','TAPE ELTY','/footprint ELTY','/haka ELTY','/dna ELTY']:
            self.assertEqual(dispatch.command_result(text,'owner'),'ok')
        self.assertEqual(dispatch.command_result('/tape ELTY 60','owner'),'ok')
        build.assert_called_with('ELTY',window=60)
        for text in ['/tape','/tape ELTY 5','/tape ELTY 60 extra','/tape ../x']:
            self.assertIn('Format:',dispatch.command_result(text,'owner'))

    @patch.object(dispatch.legacy,'authorized',return_value=False)
    @patch.object(dispatch,'build_tape_report')
    def test_authorize_before_network(self,build,auth):
        self.assertIn('chat pribadi',dispatch.command_result('/tape ELTY','group'))
        build.assert_not_called()

    @patch('requests.post')
    def test_photo_must_have_api_confirmation(self,post):
        post.return_value.ok=True
        post.return_value.json.return_value={'ok':True,'result':{'message_id':12,'photo':[{}]}}
        tape.send_photo('test','owner',b'png','ELTY')
        self.assertEqual(post.call_count,1)
        post.return_value.json.return_value={'ok':False}
        with self.assertRaises(RuntimeError):
            tape.send_photo('test','owner',b'png','ELTY')

    @patch.object(dispatch,'_original_send_telegram')
    @patch.object(dispatch,'send_photo',side_effect=RuntimeError('bad-token-url'))
    def test_photo_failure_keeps_text_without_secret(self,photo,send):
        report=tape.TapeReport('numbers');report.dashboard=b'png';report.metrics={'ticker':'ELTY'}
        dispatch.send_telegram_with_tape(report,'owner')
        self.assertIn('numbers',send.call_args.args[0])
        self.assertNotIn('bad-token-url',send.call_args.args[0])

    def test_chart_png_is_valid_and_telegram_dimensions(self):
        from orderflow_tape_chart import render_dashboard
        from PIL import Image
        from io import BytesIO
        payload=sample();m=tape.analyze('ELTY',payload)
        image=Image.open(BytesIO(render_dashboard(m,payload)))
        self.assertEqual(image.format,'PNG')
        self.assertLess(sum(image.size),10000)


if __name__=='__main__':
    unittest.main()
