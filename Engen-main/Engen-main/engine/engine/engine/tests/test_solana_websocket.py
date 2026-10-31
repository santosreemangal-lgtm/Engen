import json
import threading
import unittest

from engine.solana_websocket import SolanaLogsWebSocket


SIGNATURE = "1111111111111111111111111111111111111111111111111111111111111112"
WALLET = "11111111111111111111111111111111"


class FakeConnection:
    def __init__(self, messages, stop_event=None):
        self.messages = list(messages)
        self.stop_event = stop_event
        self.sent = []
        self.closed = False

    def settimeout(self, value):
        self.timeout = value

    def send(self, value):
        self.sent.append(json.loads(value))

    def recv(self):
        if self.messages:
            message = self.messages.pop(0)
            if not message and self.stop_event is not None:
                self.stop_event.set()
            return message
        if self.stop_event is not None:
            self.stop_event.set()
        return ""

    def close(self):
        self.closed = True


def notification(signature=SIGNATURE):
    return json.dumps({
        "jsonrpc": "2.0",
        "method": "logsNotification",
        "params": {
            "result": {
                "value": {"signature": signature, "err": None, "logs": []}
            }
        },
    })


class SolanaLogsWebSocketTests(unittest.TestCase):
    def test_duplicate_signature_is_suppressed_and_cache_is_bounded(self):
        listener = SolanaLogsWebSocket(
            "wss://example.invalid",
            max_seen_signatures=1,
            connect_function=lambda endpoint, timeout: None,
        )
        other = "1111111111111111111111111111111111111111111111111111111111111113"
        self.assertTrue(listener._remember_signature(SIGNATURE))
        self.assertFalse(listener._remember_signature(SIGNATURE))
        self.assertTrue(listener._remember_signature(other))
        self.assertTrue(listener._remember_signature(SIGNATURE))

    def test_subscribes_to_confirmed_wallet_logs_and_emits_signature_once(self):
        stop_event = threading.Event()
        ack = json.dumps({"jsonrpc": "2.0", "id": 1, "result": 7})
        connection = FakeConnection([ack, notification(), notification(), ""], stop_event)
        listener = SolanaLogsWebSocket(
            "wss://example.invalid",
            connect_function=lambda endpoint, timeout: connection,
        )
        events = []
        states = []
        listener.run(
            WALLET,
            stop_event,
            lambda signature, message: events.append(signature),
            lambda state, error=None: states.append(state),
        )
        self.assertEqual(len(connection.sent), 1)
        self.assertEqual(connection.sent[0]["method"], "logsSubscribe")
        self.assertEqual(connection.sent[0]["params"][0]["mentions"], [WALLET])
        self.assertEqual(connection.sent[0]["params"][1]["commitment"], "confirmed")
        self.assertEqual(events, [SIGNATURE])
        self.assertTrue(connection.closed)
        self.assertEqual(states[0], "connecting")
        self.assertEqual(states[-1], "stopped")

    def test_reconnects_after_transport_failure_and_stops_cleanly(self):
        stop_event = threading.Event()
        attempts = []
        states = []

        def failing_connect(endpoint, timeout):
            attempts.append(endpoint)
            raise OSError("simulated network outage")

        listener = SolanaLogsWebSocket(
            "wss://example.invalid",
            reconnect_initial=0.01,
            reconnect_max=0.02,
            connect_function=failing_connect,
        )

        def on_state(state, error=None):
            states.append(state)
            if state == "reconnecting":
                stop_event.set()

        listener.run(WALLET, stop_event, lambda signature, message: None, on_state)
        self.assertEqual(len(attempts), 1)
        self.assertIn("reconnecting", states)
        self.assertEqual(states[-1], "stopped")

    def test_rejects_non_websocket_endpoint(self):
        with self.assertRaises(ValueError):
            SolanaLogsWebSocket("https://example.invalid")


if __name__ == "__main__":
    unittest.main()
