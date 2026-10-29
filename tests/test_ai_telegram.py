"""Telegram route tests: all offline, no Telegram sends and no paid AI requests."""
from __future__ import annotations
import os
import unittest
from unittest.mock import patch

import broker_ytd_dispatch as dispatch
import ai_decision_engine.telegram as ai
from scripts.ai_request_gate import should_install


def sample_snapshot():
    return {
        "ticker": "BBRI", "asof": "2026-10-29",
        "close": 4_500.0, "ma20": 4_300.0,
        "rsi14": 55.0, "volume_ratio20": 1.2,
        "market_regime": {"label": "BULLISH", "mode": "SWING", "sample": 963},
        "broker": {"coverage": "OK", "net_value_idr": 2_000_000_000, "sessions": 12},
    }


class TelegramAITests(unittest.TestCase):
    def test_aliases_and_strict_paid_grammar(self):
        cases = {
            "AI STATUS": ("STATUS", None),
            "/ai@MyBot BBRI": ("CHECK", "BBRI"),
            "AI CHECK BBRI": ("CHECK", "BBRI"),
            "BOT1 AI CHECK BBRI": ("CHECK", "BBRI"),
            "AI RUN BBRI": ("RUN", "BBRI"),
            "/ai_run BBRI": ("RUN", "BBRI"),
            "AI BBRI RUN": ("HELP", None),
            "AI RUN BBRI EXTRA": ("HELP", None),
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                actual = ai.parse_ai_command(text)
                self.assertEqual(tuple(actual), want)
        self.assertIsNone(ai.parse_ai_command("BULL DIV BBRI"))

    @patch("broker_ytd_dispatch.legacy.authorized", return_value=False)
    @patch("ai_decision_engine.telegram.load_snapshot")
    def test_reject_unauthorized_before_db(self, load, authorized):
        text = dispatch.command_result("AI CHECK BBRI", "group")
        self.assertIn("chat pribadi", text)
        load.assert_not_called()

    @patch("broker_ytd_dispatch.legacy.authorized", return_value=True)
    @patch("ai_decision_engine.telegram.load_snapshot", return_value=sample_snapshot())
    @patch("ai_decision_engine.telegram.decide")
    def test_dry_check_no_llm_or_zapi(self, decide, load, authorized):
        decide.return_value = {"gate": "PASS", "gate_reasons": [],
                               "ai_calls": 0, "action": "WAIT"}
        out = dispatch.command_result("AI CHECK BBRI", "owner")
        self.assertIn("AI action: NOT RUN", out)
        self.assertIn("AI requests: 0 | ZAPI requests: 0", out)
        self.assertIn("4,500.00", out)
        load.assert_called_once()
        self.assertEqual(decide.call_args.kwargs, {"remote": False})

    @patch("broker_ytd_dispatch.legacy.authorized", return_value=True)
    @patch("ai_decision_engine.telegram.load_snapshot")
    def test_remote_disabled_before_db(self, load, authorized):
        with patch.dict(os.environ, {"BOT1_AI_ENABLED": "0", "BOT1_AI_REMOTE": "0"}):
            out = dispatch.command_result("AI RUN BBRI", "owner")
        self.assertIn("BELUM DIAKTIFKAN", out)
        load.assert_not_called()

    @patch("broker_ytd_dispatch.legacy.authorized", return_value=True)
    @patch("ai_decision_engine.telegram.load_snapshot", return_value=sample_snapshot())
    @patch("ai_decision_engine.telegram.decide")
    def test_remote_optin_routes_to_engine(self, decide, load, authorized):
        decide.return_value = {"gate": "PASS", "gate_reasons": [],
                               "ai_calls": 3, "action": "WAIT", "confidence": 62,
                               "reason": "Need confirmation", "risk": "Pullback"}
        with patch.dict(os.environ, {
            "BOT1_AI_ENABLED":"1", "BOT1_AI_REMOTE":"1", "OPENAI_API_KEY":"dummy",
        }):
            out = dispatch.command_result("/ai_run BBRI", "owner")
        self.assertIn("AI action: WAIT", out)
        self.assertIn("AI requests: 3", out)
        self.assertEqual(decide.call_args.kwargs, {"remote": True})

    @patch("broker_ytd_dispatch.legacy.authorized", return_value=True)
    @patch("ai_decision_engine.telegram.load_snapshot",
           side_effect=ai.DataNotReady("Stale EOD"))
    def test_missing_cache_fail_closed(self, load, auth):
        out = dispatch.command_result("AI CHECK BBRI", "owner")
        self.assertIn("WAIT / DATA NOT READY", out)
        self.assertIn("Stale EOD", out)

    @patch("broker_ytd_dispatch.legacy.authorized", return_value=True)
    def test_help_command_and_old_fallback(self, auth):
        self.assertIn("AI RUN BBRI", dispatch.command_result("AI", "owner"))
        with patch("broker_ytd_dispatch._original_command_result", return_value="LEGACY") as base:
            self.assertEqual(dispatch.command_result("SETUP RANK", "owner"), "LEGACY")
            base.assert_called_once()

    def test_dependency_install_gate(self):
        with patch.dict(os.environ, {"BOT1_AI_ENABLED":"1", "BOT1_AI_REMOTE":"1",
                                     "OPENAI_API_KEY":"dummy"}):
            self.assertTrue(should_install(text="AI RUN BBRI", chat_id="123", private_chat_id="123"))
            self.assertFalse(should_install(text="AI CHECK BBRI", chat_id="123", private_chat_id="123"))
            self.assertFalse(should_install(text="AI RUN BBRI", chat_id="999", private_chat_id="123"))
            self.assertFalse(should_install(text="AI RUN ../BAD", chat_id="123", private_chat_id="123"))
        with patch.dict(os.environ, {"BOT1_AI_ENABLED":"0", "BOT1_AI_REMOTE":"1",
                                     "OPENAI_API_KEY":"dummy"}):
            self.assertFalse(should_install(text="AI RUN BBRI", chat_id="123", private_chat_id="123"))


if __name__ == "__main__":
    unittest.main()
