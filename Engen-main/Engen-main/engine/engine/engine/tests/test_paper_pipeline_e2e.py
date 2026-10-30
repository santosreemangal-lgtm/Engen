"""Offline end-to-end regression for WebSocket-triggered paper-copy flow.

The test exercises the real monitor service, monitor bridge, order-queue logic,
SQLite-backed paper simulator and retry-safe market-data gate. Only the public
RPC observation boundary and external market-price provider are replaced.
No wallet secrets, signing, Solana transaction submission or live trading.
"""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from dashboard.monitor_bridge import create_scan_function
from dashboard.monitor_service import MonitorService
from engine.paper_simulator import PaperSimulator


WALLET = "11111111111111111111111111111111"
SIGNATURE = "1111111111111111111111111111111111111111111111111111111111111112"
ASSET_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
QUOTE_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"


def confirmed_buy_observation():
    return {
        "signature": SIGNATURE,
        "status": "success",
        "metadata_complete": True,
        "retryable": False,
        "signers": [WALLET],
        "signature_observation": {
            "confirmation_status": "confirmed",
            "slot": 123,
            "block_time": int(time.time()),
        },
        "trade_signal": {
            "classification": "BUY_CANDIDATE",
            "asset_mints": [ASSET_MINT],
            "quote_mints": [QUOTE_MINT],
        },
        "token_balance_changes": [
            {
                "owner": WALLET, "mint": ASSET_MINT, "decimals": 6,
                "delta_amount": "1000000", "pre_amount": "0",
                "post_amount": "1000000", "source": "token",
            },
            {
                "owner": WALLET, "mint": QUOTE_MINT, "decimals": 6,
                "delta_amount": "-5000000", "pre_amount": "10000000",
                "post_amount": "5000000", "source": "token",
            },
        ],
    }


class OneNotificationListener:
    def __init__(self, endpoint, first_scan_done, notification_sent):
        self.first_scan_done = first_scan_done
        self.notification_sent = notification_sent

    def run(self, address, stop_event, on_event, on_state):
        on_state("connected")
        if self.first_scan_done.wait(timeout=3) and not stop_event.is_set():
            on_event(SIGNATURE, {"method": "logsNotification"})
            self.notification_sent.set()
        stop_event.wait(timeout=3)
        on_state("stopped")


class PaperPipelineE2ETests(unittest.TestCase):
    def test_websocket_notification_creates_one_persistent_paper_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "paper-simulator.sqlite3"
            simulator = PaperSimulator(db_path=db_path)
            simulator.set_initial_virtual_balance("1000", "USD")

            first_scan_done = threading.Event()
            notification_sent = threading.Event()
            second_scan_completed = threading.Event()
            monitor_calls = []

            def monitor_wallet_stub(**kwargs):
                monitor_calls.append(kwargs["wallet_address"])
                if len(monitor_calls) == 1:
                    first_scan_done.set()
                    return []
                second_scan_completed.set()
                return [confirmed_buy_observation()]

            def scan(address):
                return scanner(address)

            listener_factory = lambda endpoint: OneNotificationListener(
                endpoint, first_scan_done, notification_sent
            )

            with patch("dashboard.monitor_bridge.SignatureStore"), \
                 patch("dashboard.monitor_bridge.monitor_wallet", side_effect=monitor_wallet_stub), \
                 patch("engine.paper_copy_runner.fetch_market_price", return_value=None):
                scanner = create_scan_function(
                    simulator_getter=lambda: simulator,
                    settings_getter=lambda: {
                        "sizing_mode": "FIXED_USD",
                        "sizing_value": "10",
                        "max_buy_usd": "10",
                    },
                )
                service = MonitorService(
                    scan,
                    interval=30,
                    websocket_endpoint="wss://offline-test.invalid",
                    websocket_listener_factory=listener_factory,
                )
                try:
                    service.start([WALLET])
                    self.assertTrue(notification_sent.wait(timeout=3))
                    self.assertTrue(second_scan_completed.wait(timeout=3))

                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        status = service.status()["wallets"][0]
                        if (
                            status["last_scan_source"] == "websocket"
                            and status["state"] == "connected"
                        ):
                            break
                        time.sleep(0.01)
                    else:
                        self.fail("WebSocket-triggered end-to-end scan did not complete")

                    self.assertEqual(monitor_calls, [WALLET, WALLET])
                    self.assertEqual(status["websocket_state"], "connected")
                    self.assertEqual(status["last_scan_source"], "websocket")
                    self.assertEqual(len(simulator.orders), 1)

                    order = next(iter(simulator.orders.values()))
                    self.assertEqual(order["side"], "BUY")
                    self.assertEqual(order["asset_mint"], ASSET_MINT)
                    self.assertEqual(order["status"], "CREATED")
                    self.assertEqual(order["details"]["origin"], "AUTOMATIC_WALLET_COPY")
                    self.assertEqual(order["details"]["target_notional_usd"], "10")
                    self.assertIn("market", (order.get("reason") or "").lower())
                    self.assertEqual(simulator.fills, [])
                    self.assertFalse(order["details"]["transaction_broadcast"])

                    recovered = PaperSimulator(db_path=db_path)
                    self.assertEqual(len(recovered.orders), 1)
                    recovered_order = next(iter(recovered.orders.values()))
                    self.assertEqual(recovered_order["signal_id"], order["signal_id"])
                    self.assertEqual(recovered_order["status"], "CREATED")
                    self.assertEqual(recovered.balances["USD"], simulator.balances["USD"])
                finally:
                    service.stop_and_wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
