import threading
import time
import unittest

from dashboard.monitor_service import MonitorService


WALLET = "11111111111111111111111111111111"
SIGNATURE = "1111111111111111111111111111111111111111111111111111111111111112"


class TriggeringListener:
    def __init__(self, endpoint):
        self.endpoint = endpoint

    def run(self, address, stop_event, on_event, on_state):
        on_state("connected")
        if self.first_poll.wait(timeout=3):
            on_event(SIGNATURE, {"method": "logsNotification"})
            self.event_sent.set()
        stop_event.wait(timeout=3)
        on_state("stopped")

    first_poll = threading.Event()
    event_sent = threading.Event()


class UnavailableListener:
    def __init__(self, endpoint):
        self.endpoint = endpoint

    def run(self, address, stop_event, on_event, on_state):
        on_state("unavailable", "OSError")


class WebSocketMonitorIntegrationTests(unittest.TestCase):
    def test_websocket_event_triggers_scan_without_waiting_for_poll_interval(self):
        first_poll = threading.Event()
        second_scan = threading.Event()
        event_sent = threading.Event()
        scans = []

        class Listener:
            def __init__(self, endpoint):
                self.endpoint = endpoint

            def run(self, address, stop_event, on_event, on_state):
                on_state("connected")
                if first_poll.wait(timeout=3):
                    on_event(SIGNATURE, {"method": "logsNotification"})
                    event_sent.set()
                stop_event.wait(timeout=3)
                on_state("stopped")

        def scan(address):
            scans.append(address)
            if len(scans) == 1:
                first_poll.set()
            else:
                second_scan.set()
            return []

        service = MonitorService(
            scan,
            interval=30,
            websocket_endpoint="wss://example.invalid",
            websocket_listener_factory=Listener,
        )
        try:
            service.start([WALLET])
            self.assertTrue(second_scan.wait(timeout=3))
            self.assertTrue(event_sent.is_set())
            wallet = service.status()["wallets"][0]
            self.assertEqual(wallet["last_scan_source"], "websocket")
            self.assertEqual(wallet["websocket_state"], "connected")
            self.assertGreaterEqual(len(scans), 2)
        finally:
            service.stop_and_wait(timeout=3)

    def test_polling_continues_when_websocket_is_unavailable(self):
        scanned = threading.Event()

        def scan(address):
            scanned.set()
            return []

        service = MonitorService(
            scan,
            interval=30,
            websocket_endpoint="wss://example.invalid",
            websocket_listener_factory=UnavailableListener,
        )
        try:
            service.start([WALLET])
            self.assertTrue(scanned.wait(timeout=3))
            status = service.status()
            self.assertTrue(status["polling_fallback_enabled"])
            self.assertEqual(status["wallets"][0]["last_scan_source"], "polling")
            self.assertEqual(status["wallets"][0]["websocket_state"], "unavailable")
        finally:
            service.stop_and_wait(timeout=3)

    def test_websocket_can_be_disabled_without_disabling_polling(self):
        scanned = threading.Event()
        service = MonitorService(
            lambda address: (scanned.set() or []),
            interval=30,
            websocket_endpoint="",
        )
        try:
            service.start([WALLET])
            self.assertTrue(scanned.wait(timeout=3))
            status = service.status()
            self.assertFalse(status["websocket_enabled"])
            self.assertTrue(status["polling_fallback_enabled"])
            self.assertEqual(status["wallets"][0]["websocket_state"], "disabled")
        finally:
            service.stop_and_wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
