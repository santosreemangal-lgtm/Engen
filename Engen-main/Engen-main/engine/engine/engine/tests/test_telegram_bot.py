import unittest
from unittest.mock import patch

from engine.paper_trading import PaperTrader
from engine.telegram_bot import TelegramBotError, TelegramPaperBot


class TelegramPaperBotTests(unittest.TestCase):
    def setUp(self):
        self.trader = PaperTrader()
        self.bot = TelegramPaperBot(
            token="test-token",
            allowed_chat_ids=["12345"],
            trader=self.trader,
        )
        self.sent = []
        self.bot.send_message = lambda chat_id, text: self.sent.append((str(chat_id), text))

    def test_help_command_replies_with_virtual_only_notice(self):
        self.bot.handle_update({
            "update_id": 1,
            "message": {"chat": {"id": 12345}, "text": "/help"},
        })
        self.assertEqual(len(self.sent), 1)
        self.assertIn("/balance", self.sent[0][1])
        self.assertIn("No real transactions", self.sent[0][1])

    def test_unauthorized_chat_is_ignored(self):
        self.bot.handle_update({
            "update_id": 1,
            "message": {"chat": {"id": 99999}, "text": "/pause"},
        })
        self.assertEqual(self.sent, [])
        self.assertFalse(self.trader.status()["paused"])

    def test_pause_and_resume_commands_control_paper_trader(self):
        self.bot.handle_update({
            "message": {"chat": {"id": 12345}, "text": "/pause"},
        })
        self.assertTrue(self.trader.status()["paused"])
        self.bot.handle_update({
            "message": {"chat": {"id": 12345}, "text": "/resume"},
        })
        self.assertFalse(self.trader.status()["paused"])

    def test_status_balance_settings_history_commands(self):
        for command in ("/status", "/balance", "/settings", "/history"):
            self.bot.handle_update({
                "message": {"chat": {"id": 12345}, "text": command},
            })
        self.assertEqual(len(self.sent), 4)
        self.assertIn("Live trading: disabled", self.sent[0][1])

    def test_bot_refuses_to_start_without_allowlist(self):
        bot = TelegramPaperBot(token="test-token", allowed_chat_ids=[])
        with self.assertRaises(TelegramBotError):
            bot.run_forever()


if __name__ == "__main__":
    unittest.main()
