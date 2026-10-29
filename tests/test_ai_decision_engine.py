"""Fully offline, zero-neon/zero-ZAPI/zero-API tests."""
import os
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from ai_decision_engine.__main__ import DataNotReady, completed_day, ticker_code, make_snapshot, gate, decide, parse_decision

def sample(bull=True, broker=True):
    day=date(2026,10,28)
    history=[{"trade_date":(day-timedelta(days=40-i)).isoformat(),
              "close_price":100+i,"volume_shares":1000} for i in range(40)]
    summary={"trade_date":day,"open_price":138,"close_price":141,"volume_shares":1600}
    universe=[{"OpenPrice":100,"Close":102 if bull else 98,
               "foreignBuyValue":200 if bull else 100,
               "foreignSellValue":100 if bull else 200} for _ in range(120)]
    b={"coverage":"OK" if broker else "NO_DATA","sessions":6,
       "net_value_idr":3_000_000_000,"last_date":day.isoformat()}
    return make_snapshot("TEST",day,summary,history,universe,b)

class Tests(unittest.TestCase):
    def test_safe_eod_boundary(self):
        self.assertEqual(completed_day(datetime(2026,10,29,16,59,tzinfo=ZoneInfo("Asia/Jakarta"))),
                         date(2026,10,28))
    def test_ticker_sanitized(self):
        self.assertEqual(ticker_code("bbri.jk"),"BBRI")
        with self.assertRaises(ValueError):
            ticker_code("../../bad")
    def test_no_mismatched_date(self):
        with self.assertRaises(DataNotReady):
            make_snapshot("TEST",date(2026,10,27),{"trade_date":"2026-10-28"},[],[],{})
    def test_dry_run_does_not_call_llm(self):
        s=sample()
        self.assertEqual(s["market_regime"]["label"],"BULLISH")
        self.assertEqual(s["history_last_date"],"2026-10-28")
        self.assertGreater(s["volume_ratio20"],1)
        self.assertEqual(gate(s),[])
        self.assertEqual(decide(s)["ai_calls"],0)
        self.assertEqual(decide(s)["action"],"WAIT")
    def test_bearish_prevents_api(self):
        s=sample(bull=False)
        with patch.dict(os.environ,{"BOT1_AI_ENABLED":"1","BOT1_AI_REMOTE":"1"}):
            d=decide(s,remote=True,invoke=lambda _:self.fail("API must not be called"))
        self.assertEqual(d["gate"],"BLOCK")
        self.assertEqual(d["ai_calls"],0)
    def test_missing_broker_prevents_api(self):
        s=sample(broker=False)
        with patch.dict(os.environ,{"BOT1_AI_ENABLED":"1","BOT1_AI_REMOTE":"1"}):
            d=decide(s,remote=True,invoke=lambda _:self.fail("API must not be called"))
        self.assertEqual(d["action"],"WAIT")
    def test_remote_requires_enable_flags(self):
        with patch.dict(os.environ,{"BOT1_AI_ENABLED":"0","BOT1_AI_REMOTE":"0"}):
            with self.assertRaises(PermissionError):
                decide(sample(),remote=True,invoke=lambda _:"{}")
    def test_max_three_calls_and_json(self):
        replies=["bull","bear",'{"action":"WAIT","confidence":60,"reason":"Unconfirmed","risk":"Drawdown"}']
        calls=[]
        def invoke(prompt):
            calls.append(prompt)
            return replies.pop(0)
        with patch.dict(os.environ,{"BOT1_AI_ENABLED":"1","BOT1_AI_REMOTE":"1"}):
            d=decide(sample(),remote=True,invoke=invoke)
        self.assertEqual(d["ai_calls"],3)
        self.assertEqual(len(calls),3)
        self.assertEqual(d["action"],"WAIT")
    def test_invalid_json_fails_closed(self):
        with patch.dict(os.environ,{"BOT1_AI_ENABLED":"1","BOT1_AI_REMOTE":"1"}):
            d=decide(sample(),remote=True,invoke=lambda _:"BAD")
        self.assertEqual(d["action"],"WAIT")
    def test_reject_invalid_action(self):
        with self.assertRaises(ValueError):
            parse_decision('{"action":"EXECUTE","confidence":100,"reason":"r","risk":"r"}')

if __name__=="__main__":
    unittest.main()
