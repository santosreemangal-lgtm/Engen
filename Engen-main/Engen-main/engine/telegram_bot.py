"""Minimal Telegram control adapter for the virtual paper portfolio.

Requires TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_CHAT_IDS. Uses only the
Python standard library. No wallet keys or live-trading endpoints are used.
"""
import json
import os
import time
import urllib.parse
import urllib.request

from engine.paper_trading import PaperTrader

API_ROOT = "https://api.telegram.org"


class TelegramBotError(RuntimeError):
    pass


class TelegramPaperBot:
    def __init__(self, token=None, allowed_chat_ids=None, trader=None, timeout=15):
        self.token = (token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN", "")).strip()
        raw_allowed = (
            allowed_chat_ids
            if allowed_chat_ids is not None
            else os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "")
        )
        if isinstance(raw_allowed, str):
            self.allowed_chat_ids = {
                item.strip() for item in raw_allowed.split(",") if item.strip()
            }
        else:
            self.allowed_chat_ids = {str(item).strip() for item in raw_allowed}
        self.trader = trader or PaperTrader()
        self.timeout = timeout
        self._offset = None

    def _request(self, method, payload=None):
        if not self.token:
            raise TelegramBotError("TELEGRAM_BOT_TOKEN is not configured.")
        url = "%s/bot%s/%s" % (API_ROOT, self.token, method)
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"} if data else {},
            method="GET" if data is None else "POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise TelegramBotError("Telegram API request failed: %s" % type(exc).__name__) from exc
        if not isinstance(body, dict) or body.get("ok") is not True:
            raise TelegramBotError("Telegram API returned an unsuccessful response.")
        return body.get("result")

    def send_message(self, chat_id, text):
        return self._request("sendMessage", {"chat_id": chat_id, "text": text})

    def handle_update(self, update):
        if not isinstance(update, dict):
            return
        message = update.get("message") or update.get("edited_message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat")
        if not isinstance(chat, dict) or chat.get("id") is None:
            return
        chat_id = str(chat["id"])
        if chat_id not in self.allowed_chat_ids:
            # Do not respond to unauthorized users.
            return
        text = message.get("text")
        if not isinstance(text, str):
            return
        command = text.strip().split()[0].split("@", 1)[0].lower()
        if command in ("/start", "/help"):
            reply = (
                "Engen paper-trading controls (virtual only):\n"
                "/status - portfolio status\n"
                "/balance - virtual cash and positions\n"
                "/settings - current demo risk limit\n"
                "/history - recent simulated trades\n"
                "/pause - block simulated trades\n"
                "/resume - allow simulated trades\n"
                "No real transactions are sent."
            )
        elif command == "/status":
            status = self.trader.status()
            reply = "Mode: PAPER\nPaused: %s\nVirtual cash: %s\nPositions: %s\nLive trading: disabled" % (
                status["paused"], status["cash"], len(status["positions"])
            )
        elif command == "/balance":
            status = self.trader.status()
            positions = status["positions"]
            reply = "Virtual cash: %s\nPositions: %s\n%s" % (
                status["cash"], len(positions),
                "\n".join("%s: %s units @ avg %s" % (token, item["quantity"], item["average_entry_price"]) for token, item in positions.items()) or "No open positions."
            )
        elif command == "/settings":
            reply = "Paper-only settings:\nStarting virtual balance: %s\nMaximum position cost: %s%% of starting balance\nLive trading: disabled" % (
                self.trader.status()["starting_balance"],
                int(self.trader._max_position_fraction * 100),
            )
        elif command == "/history":
            entries = self.trader.history(10)
            reply = "Recent simulated trades:\n" + (
                "\n".join("%s %s qty=%s price=%s realized P/L=%s" % (
                    item["side"].upper(), item["token"], item["quantity"],
                    item["price"], item["realized_pnl"]
                ) for item in reversed(entries)) or "No simulated trades yet."
            )
        elif command == "/pause":
            self.trader.pause()
            reply = "Paper trading paused. No real trades are possible."
        elif command == "/resume":
            self.trader.resume()
            reply = "Paper trading resumed. Virtual trades only."
        else:
            reply = "Unknown command. Use /help."
        self.send_message(chat_id, reply)

    def run_forever(self, poll_seconds=1):
        if not self.token:
            raise TelegramBotError("Set TELEGRAM_BOT_TOKEN before starting the bot.")
        if not self.allowed_chat_ids:
            raise TelegramBotError("Set TELEGRAM_ALLOWED_CHAT_IDS to your authorized numeric chat ID(s).")
        while True:
            params = {"timeout": 20}
            if self._offset is not None:
                params["offset"] = self._offset
            query = urllib.parse.urlencode(params)
            updates = self._request("getUpdates?" + query)
            if not isinstance(updates, list):
                raise TelegramBotError("Telegram returned an invalid updates list.")
            for update in updates:
                if isinstance(update, dict) and isinstance(update.get("update_id"), int):
                    self._offset = update["update_id"] + 1
                self.handle_update(update)
            time.sleep(poll_seconds)


def main():
    bot = TelegramPaperBot()
    print("Starting Telegram paper-trading bot; live trading is disabled.")
    bot.run_forever()


if __name__ == "__main__":
    main()
