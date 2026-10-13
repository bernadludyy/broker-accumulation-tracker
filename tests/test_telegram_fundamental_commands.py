import unittest
from unittest.mock import patch

import telegram_query_bot


class FundamentalCommandTests(unittest.TestCase):
    @patch("telegram_query_bot.format_fundamental_report", return_value="FUND")
    def test_fundamental_command_aliases(self, formatter):
        self.assertEqual(telegram_query_bot.command_result("/fund ADRO", "1"), "FUND")
        self.assertEqual(telegram_query_bot.command_result("ADRO FUND", "1"), "FUND")
        self.assertEqual(telegram_query_bot.command_result("BOT1 FUND ADRO", "1"), "FUND")
        self.assertEqual(formatter.call_count, 3)

    @patch("telegram_query_bot.format_bot1_full", return_value="FULL")
    def test_full_command_aliases(self, formatter):
        self.assertEqual(telegram_query_bot.command_result("/full ADRO", "1"), "FULL")
        self.assertEqual(telegram_query_bot.command_result("ADRO FULL", "1"), "FULL")
        self.assertEqual(telegram_query_bot.command_result("BOT1 FULL ADRO", "1"), "FULL")
        self.assertEqual(formatter.call_count, 3)


if __name__ == "__main__":
    unittest.main()
