import threading
import time
from datetime import datetime, timedelta, timezone
import unittest

from dashboard.monitor_service import MonitorService


class MonitorServiceTests(unittest.TestCase):

    def test_recovers_after_scan_error(self):
        scan_attempts = 0
        recovered = threading.Event()

        def fake_scan(address):
            nonlocal scan_attempts
            scan_attempts += 1

            if scan_attempts == 1:
                raise RuntimeError("temporary RPC failure")

            recovered.set()
            return [
                {
                    "signature": "test_signature",
                    "available": True,
                }
            ]

        service = MonitorService(fake_scan, interval=30)

        # Test-only: skip the normal wait between polling cycles.
        service._interval = 0

        try:
            service.start(["11111111111111111111111111111111"])

            self.assertTrue(
                recovered.wait(timeout=3),
                "The service did not recover after the first scan error.",
            )

            deadline = time.monotonic() + 3
            status = service.status()

            while time.monotonic() < deadline:
                if (
                    status["wallets"]
                    and status["wallets"][0]["state"] == "connected"
                ):
                    break

                time.sleep(0.01)
                status = service.status()

            self.assertTrue(status["enabled"])
            self.assertEqual(len(status["wallets"]), 1)

            wallet = status["wallets"][0]

            self.assertEqual(wallet["address"], "11111111111111111111111111111111")
            self.assertEqual(wallet["state"], "connected")
            self.assertIsNone(wallet["last_error"])
            self.assertIsNotNone(wallet["last_successful_check"])
            self.assertEqual(
                wallet["latest_results"][0]["signature"],
                "test_signature",
            )
            self.assertGreaterEqual(scan_attempts, 2)

        finally:
            service.stop()
            worker = service._thread
            if worker is not None:
                worker.join(timeout=3)

    def test_wallet_failures_are_isolated_between_sources(self):
        second_wallet_succeeded = threading.Event()
        first = "11111111111111111111111111111111"
        second = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"

        def scan(address):
            if address == first:
                raise RuntimeError("temporary failure for first wallet")
            second_wallet_succeeded.set()
            return [{"signature": "public-result", "available": True}]

        service = MonitorService(scan, interval=30)
        try:
            service.start([first, second])
            self.assertTrue(second_wallet_succeeded.wait(timeout=3))
            status = service.status()
            states = {item["address"]: item["state"] for item in status["wallets"]}
            self.assertEqual(states[first], "error")
            self.assertEqual(states[second], "connected")
        finally:
            service.stop_and_wait(timeout=3)

    def test_stale_monitor_is_reported_from_last_successful_check(self):
        service = MonitorService(lambda address: [], interval=30)
        old = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()
        state = service._status_details({
            "state": "connected",
            "last_successful_check": old,
            "last_error": None,
            "latest_results": [],
        })
        self.assertEqual(state["state"], "stale")
        service.stop()

    def test_active_scan_is_not_reported_stale(self):
        service = MonitorService(lambda address: [], interval=30)
        old = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()
        state = service._status_details({
            "state": "scanning",
            "last_successful_check": old,
            "last_error": None,
            "latest_results": [],
        })
        self.assertEqual(state["state"], "scanning")
        service.stop()

    def test_transaction_rpc_errors_report_degraded_source_quality(self):
        scan_started = threading.Event()

        def degraded_scan(address):
            scan_started.set()
            return [{
                "signature": "test_signature",
                "metadata_status": "rpc_error",
                "rpc_code": -32015,
                "retryable": True,
            }]

        service = MonitorService(degraded_scan, interval=30)
        try:
            service.start(["11111111111111111111111111111111"])
            self.assertTrue(scan_started.wait(timeout=3))
            deadline = time.monotonic() + 3
            wallet = service.status()["wallets"][0]
            while time.monotonic() < deadline and wallet["state"] != "connected":
                time.sleep(0.01)
                wallet = service.status()["wallets"][0]
            self.assertEqual(wallet["state"], "connected")
            self.assertEqual(wallet["source_quality"], "degraded")
            self.assertEqual(wallet["rpc_error_count"], 1)
            self.assertIn("-32015", wallet["last_error"])
        finally:
            service.stop_and_wait(timeout=3)

    def test_scan_error_is_recorded(self):
        scan_started = threading.Event()

        def failing_scan(address):
            scan_started.set()
            raise RuntimeError("temporary RPC failure")

        service = MonitorService(failing_scan, interval=30)

        try:
            service.start(["11111111111111111111111111111111"])

            self.assertTrue(
                scan_started.wait(timeout=3),
                "The scan did not start.",
            )

            deadline = time.monotonic() + 3
            wallet = service.status()["wallets"][0]

            while time.monotonic() < deadline:
                if wallet["state"] == "error":
                    break

                time.sleep(0.01)
                wallet = service.status()["wallets"][0]

            self.assertEqual(wallet["state"], "error")
            self.assertEqual(
                wallet["last_error"],
                "Monitoring scan failed (RuntimeError).",
            )
            self.assertIsNotNone(wallet["last_scan"])

        finally:
            service.stop()
            worker = service._thread
            if worker is not None:
                worker.join(timeout=3)

    def test_stop_and_wait_confirms_worker_exit(self):
        scan_started = threading.Event()
        release_scan = threading.Event()

        def slow_scan(address):
            scan_started.set()
            release_scan.wait(timeout=2)
            return []

        service = MonitorService(slow_scan, interval=30)
        service.start(["11111111111111111111111111111111"])
        self.assertTrue(scan_started.wait(timeout=2))
        release_scan.set()

        result = service.stop_and_wait(timeout=2)
        self.assertTrue(result["worker_stopped"])
        self.assertFalse(result["enabled"])
        self.assertEqual(result["wallets"][0]["state"], "stopped")

    def test_stop_and_wait_rejects_invalid_timeout(self):
        service = MonitorService(lambda address: [], interval=30)
        for timeout in (0, -1, True, "1"):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    service.stop_and_wait(timeout=timeout)
        service.stop()
        worker = service._thread
        if worker is not None:
            worker.join(timeout=2)

    def test_rejects_interval_below_15_seconds(self):
        with self.assertRaises(ValueError):
            MonitorService(lambda address: [], interval=14)

    def test_rejects_non_callable_scan_function(self):
        with self.assertRaises(ValueError):
            MonitorService(None)

    def test_rejects_invalid_solana_wallet_address(self):
        service = MonitorService(lambda address: [], interval=30)
        try:
            with self.assertRaises(ValueError):
                service.start(["not-a-solana-address"])
        finally:
            service.stop()

    def test_rejects_non_list_wallet_addresses(self):
        service = MonitorService(lambda address: [], interval=30)

        try:
            with self.assertRaises(ValueError):
                service.start("not-a-list")
        finally:
            service.stop()
            worker = service._thread
            if worker is not None:
                worker.join(timeout=3)

    def test_restart_waits_for_previous_thread_to_stop(self):
        first_scan_started = threading.Event()
        release_first_scan = threading.Event()
        second_scan_completed = threading.Event()

        scan_count = 0
        scan_lock = threading.Lock()

        def fake_scan(address):
            nonlocal scan_count

            with scan_lock:
                scan_count += 1
                current_scan = scan_count

            if current_scan == 1:
                first_scan_started.set()

                if not release_first_scan.wait(timeout=5):
                    raise RuntimeError(
                        "Timed out waiting to release the first scan."
                    )

                return []

            second_scan_completed.set()
            return []

        service = MonitorService(fake_scan, interval=30)

        # Test-only: skip the normal wait between polling cycles.
        service._interval = 0

        release_timer = None
        previous_worker = None

        try:
            service.start(["11111111111111111111111111111111"])

            self.assertTrue(
                first_scan_started.wait(timeout=3),
                "The first scan did not start.",
            )

            previous_worker = service._thread
            service.stop()

            # Release the blocked scan shortly afterward. A correct restart
            # waits for the previous worker to finish before starting again.
            release_timer = threading.Timer(
                0.5,
                release_first_scan.set,
            )
            release_timer.daemon = True
            release_timer.start()

            service.start(["11111111111111111111111111111111"])

            self.assertIsNotNone(previous_worker)
            self.assertFalse(
                previous_worker.is_alive(),
                "Restart returned before the previous worker stopped.",
            )

            self.assertTrue(
                second_scan_completed.wait(timeout=3),
                "The restarted service did not perform another scan.",
            )

            self.assertTrue(service.status()["enabled"])
            self.assertGreaterEqual(scan_count, 2)

        finally:
            release_first_scan.set()

            if release_timer is not None:
                release_timer.cancel()

            service.stop()

            worker = service._thread
            if worker is not None:
                worker.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
