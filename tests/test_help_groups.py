"""Regression tests for Bot 1 help grouped by command function."""
from __future__ import annotations

import unittest

import broker_ytd_dispatch as bot


class HelpGroupsTest(unittest.TestCase):
    def test_full_help_is_grouped_and_keeps_extension_commands(self):
        text = bot.command_result("/help", "123")

        headings = [
            "ANALISIS & KEPUTUSAN",
            "SCREENER & RANKING",
            "BROKER & SMART MONEY",
            "FOREIGN FLOW",
            "ENTRY & TEKNIKAL",
            "DATA, CACHE & WATCHLIST",
        ]
        positions = [text.index(heading) for heading in headings]

        self.assertEqual(positions, sorted(positions))
        self.assertIn("AI CHECK BBRI", text)
        self.assertIn("BANDAR MOMENTUM", text)
        self.assertIn("BROKER TOP BBRI YTD", text)
        self.assertIn("INSIDER BBRI", text)

    def test_help_topic_returns_only_requested_function_group(self):
        broker = bot.command_result("HELP BROKER", "123")
        screener = bot.command_result("/help screener", "123")
        data = bot.command_result("HELP DATA", "123")

        self.assertIn("BROKER STALKER", broker)
        self.assertIn("BROKER TOP BBRI YTD", broker)
        self.assertNotIn("BANDAR MOMENTUM", broker)

        self.assertIn("BANDAR MOMENTUM", screener)
        self.assertIn("BULL DIV", screener)
        self.assertNotIn("BROKER TOP BBRI YTD", screener)

        self.assertIn("/eod refresh", data)
        self.assertIn("/eod refresh last", data)
        self.assertIn("INSIDER REFRESH BBRI", data)
        self.assertNotIn("AI CHECK BBRI", data)

    def test_help_topic_alias_and_unknown_category(self):
        alias = bot.command_result("HELP BANDAR", "123")
        unknown = bot.command_result("HELP LAINNYA", "123")

        self.assertIn("SCREENER & RANKING", alias)
        self.assertIn("BANDAR MOMENTUM", alias)
        self.assertIn("Kategori tidak dikenal: LAINNYA", unknown)


if __name__ == "__main__":
    unittest.main()

