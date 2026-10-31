"""Read-only wallet monitor with WebSocket notifications and polling fallback."""

import os
import queue
import threading
import time
from datetime import datetime, timezone

from engine.solana_validation import is_valid_solana_address


class MonitorService:
    """Manage opt-in public-wallet monitoring; WebSocket is an accelerator, polling is fallback."""

    def __init__(self, scan_function, interval=15, websocket_endpoint=None,
                 websocket_listener_factory=None):
        if not callable(scan_function):
            raise ValueError("scan_function must be callable.")
        if not isinstance(interval, int) or isinstance(interval, bool) or interval < 15:
            raise ValueError("Monitoring interval must be at least 15 seconds.")

        self._scan_function = scan_function
        self._interval = interval
        self._websocket_endpoint = (
            websocket_endpoint
            if websocket_endpoint is not None
            else os.environ.get("ENGEN_WS_URL", "").strip()
        )
        self._websocket_listener_factory = websocket_listener_factory
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._websocket_stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread = None
        self._websocket_threads = {}
        self._pending_websocket_wallets = set()
        self._event_subscribers = set()
        self._wallets = {}
        self._enabled = False

    @staticmethod
    def _new_wallet_state(websocket_enabled=True):
        return {
            "state": "waiting",
            "last_scan": None,
            "last_scan_duration_ms": None,
            "last_successful_check": None,
            "last_error": None,
            "latest_results": [],
            "last_scan_source": None,
            "websocket_state": "connecting" if websocket_enabled else "disabled",
            "websocket_last_notification": None,
            "websocket_last_signature": None,
            "websocket_reconnects": 0,
            "websocket_error_type": None,
        }

    def _status_details(self, details):
        snapshot = dict(details)
        successful = snapshot.get("last_successful_check")
        if successful:
            try:
                last_success = datetime.fromisoformat(successful)
                age = (datetime.now(timezone.utc) - last_success).total_seconds()
                if self._interval > 0 and age > self._interval * 2 and snapshot.get("state") != "error":
                    snapshot["state"] = "stale"
            except (TypeError, ValueError):
                snapshot["state"] = "stale"
        return snapshot

    def status(self):
        with self._lock:
            return {
                "enabled": self._enabled,
                "interval_seconds": self._interval,
                "websocket_enabled": bool(self._websocket_endpoint),
                "polling_fallback_enabled": True,
                "wallets": [
                    {"address": address, **self._status_details(details)}
                    for address, details in self._wallets.items()
                ],
            }

    def _set_websocket_state(self, address, state, error_type=None):
        with self._lock:
            details = self._wallets.get(address)
            if details is None:
                return
            details["websocket_state"] = state
            details["websocket_error_type"] = error_type
            if state == "reconnecting":
                details["websocket_reconnects"] += 1

    def _queue_websocket_event(self, address, signature, _message):
        with self._lock:
            details = self._wallets.get(address)
            if details is None or self._stop_event.is_set():
                return
            details["websocket_last_notification"] = datetime.now(timezone.utc).isoformat()
            details["websocket_last_signature"] = signature
            self._pending_websocket_wallets.add(address)
        # The single scan worker consumes this wake-up. This coalesces bursts
        # and prevents concurrent WebSocket and polling scans for one wallet.
        self._wake_event.set()

    def _make_websocket_listener(self):
        if self._websocket_listener_factory is not None:
            return self._websocket_listener_factory(self._websocket_endpoint)
        from engine.solana_websocket import SolanaLogsWebSocket
        return SolanaLogsWebSocket(self._websocket_endpoint)

    def _start_websocket_worker(self, address):
        if not self._websocket_endpoint or address in self._websocket_threads:
            return
        try:
            listener = self._make_websocket_listener()
        except Exception as exc:
            self._set_websocket_state(address, "unavailable", type(exc).__name__)
            return

        def run_listener():
            try:
                listener.run(
                    address,
                    self._websocket_stop_event,
                    lambda signature, message: self._queue_websocket_event(address, signature, message),
                    lambda state, error_type=None: self._set_websocket_state(address, state, error_type),
                )
            except Exception as exc:
                self._set_websocket_state(address, "unavailable", type(exc).__name__)

        worker = threading.Thread(
            target=run_listener,
            name="solana-wallet-websocket",
            daemon=True,
        )
        self._websocket_threads[address] = worker
        worker.start()

    def start(self, addresses):
        """Start WebSocket subscriptions plus a periodic polling safety net."""
        if not isinstance(addresses, list):
            raise ValueError("Wallet addresses must be provided as a list.")
        cleaned = []
        for address in addresses:
            if not isinstance(address, str) or not address.strip():
                continue
            address = address.strip()
            if not is_valid_solana_address(address):
                raise ValueError("A monitored wallet must be a valid Solana public address.")
            if address not in cleaned:
                cleaned.append(address)

        with self._lifecycle_lock:
            with self._lock:
                previous_thread = self._thread
                active = (
                    previous_thread is not None
                    and previous_thread.is_alive()
                    and not self._stop_event.is_set()
                )
                if active:
                    changed = set(self._wallets) != set(cleaned)
                    if not changed:
                        return self.status()
                    # Rebuild subscriptions when the monitored wallet set changes.
                    # This guarantees removed wallets are no longer subscribed.
                    self._websocket_stop_event.set()
                    old_websocket_workers = list(self._websocket_threads.values())
                    self._wallets = {
                        address: self._wallets.get(
                            address, self._new_wallet_state(bool(self._websocket_endpoint))
                        )
                        for address in cleaned
                    }
                    self._pending_websocket_wallets.clear()
                else:
                    old_websocket_workers = []

            if active:
                for worker in old_websocket_workers:
                    if worker.is_alive():
                        worker.join()
                self._websocket_threads = {}
                self._websocket_stop_event.clear()
                for address in cleaned:
                    self._start_websocket_worker(address)
                return self.status()

            if previous_thread is not None and previous_thread.is_alive():
                previous_thread.join()
            for worker in list(self._websocket_threads.values()):
                if worker.is_alive():
                    worker.join()
            self._websocket_threads = {}
            self._stop_event.clear()
            self._websocket_stop_event.clear()
            self._wake_event.clear()
            self._pending_websocket_wallets.clear()

            with self._lock:
                self._wallets = {
                    address: self._new_wallet_state(bool(self._websocket_endpoint))
                    for address in cleaned
                }
                self._enabled = True
                self._thread = threading.Thread(
                    target=self._run,
                    name="solana-read-only-monitor",
                    daemon=True,
                )
                self._thread.start()
                for address in cleaned:
                    self._start_websocket_worker(address)
                return self.status()

    def stop(self):
        """Request a graceful stop for polling and all WebSocket workers."""
        with self._lifecycle_lock:
            self._stop_event.set()
            self._websocket_stop_event.set()
            self._wake_event.set()
            with self._lock:
                self._enabled = False
                for details in self._wallets.values():
                    if details.get("state") not in ("error", "stale"):
                        details["state"] = "stopping"
                return self.status()

    def stop_and_wait(self, timeout=15):
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            raise ValueError("Shutdown timeout must be positive.")
        self.stop()
        deadline = time.monotonic() + timeout
        with self._lock:
            workers = [self._thread] + list(self._websocket_threads.values())
        for worker in workers:
            if worker is not None and worker is not threading.current_thread():
                worker.join(timeout=max(0, deadline - time.monotonic()))
        stopped = all(worker is None or not worker.is_alive() for worker in workers)
        return {**self.status(), "worker_stopped": stopped}

    def subscribe_events(self, maxsize=32):
        """Subscribe to completed scans; each queue receives the newest event."""
        if not isinstance(maxsize, int) or isinstance(maxsize, bool) or maxsize < 1:
            raise ValueError("Event queue size must be a positive integer.")
        subscriber = queue.Queue(maxsize=maxsize)
        with self._lock:
            self._event_subscribers.add(subscriber)
        return subscriber

    def unsubscribe_events(self, subscriber):
        """Remove a dashboard event subscriber."""
        with self._lock:
            self._event_subscribers.discard(subscriber)

    def _publish_event(self, event):
        """Publish a bounded event after the scan function and paper pipeline finish."""
        with self._lock:
            subscribers = list(self._event_subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                try:
                    subscriber.get_nowait()
                except queue.Empty:
                    pass
                try:
                    subscriber.put_nowait(event)
                except queue.Full:
                    pass

    def _scan_one(self, address, source):
        with self._lock:
            if address not in self._wallets or self._stop_event.is_set():
                return
            self._wallets[address]["state"] = "scanning"
        started_ns = time.perf_counter_ns()
        scanned_at = datetime.now(timezone.utc).isoformat()
        try:
            results = self._scan_function(address)
            if not isinstance(results, list):
                results = []
            duration_ms = round((time.perf_counter_ns() - started_ns) / 1_000_000, 3)
            completed_at = datetime.now(timezone.utc).isoformat()
            with self._lock:
                if address in self._wallets:
                    self._wallets[address].update({
                        "state": "connected",
                        "last_scan": completed_at,
                        "last_scan_duration_ms": duration_ms,
                        "last_successful_check": completed_at,
                        "last_error": None,
                        "latest_results": results[:20],
                        "last_scan_source": source,
                    })
            self._publish_event({
                "type": "wallet_scan_complete",
                "wallet_address": address,
                "source": source,
                "scan_completed_at": completed_at,
                "scan_duration_ms": duration_ms,
                "result_count": len(results),
            })
        except Exception:
            duration_ms = round((time.perf_counter_ns() - started_ns) / 1_000_000, 3)
            completed_at = datetime.now(timezone.utc).isoformat()
            with self._lock:
                if address in self._wallets:
                    self._wallets[address].update({
                        "state": "error",
                        "last_scan": completed_at,
                        "last_scan_duration_ms": duration_ms,
                        "last_error": "Monitoring scan failed; sensitive error details are hidden.",
                        "last_scan_source": source,
                    })
            self._publish_event({
                "type": "wallet_scan_error",
                "wallet_address": address,
                "source": source,
                "scan_completed_at": completed_at,
                "scan_duration_ms": duration_ms,
            })

    def _run(self):
        """Poll on schedule and immediately rescan wallets signalled by WebSocket."""
        next_poll = 0.0
        try:
            while not self._stop_event.is_set():
                wait_seconds = max(0.0, next_poll - time.monotonic())
                self._wake_event.wait(wait_seconds)
                self._wake_event.clear()
                if self._stop_event.is_set():
                    break

                now = time.monotonic()
                with self._lock:
                    signalled = set(self._pending_websocket_wallets)
                    self._pending_websocket_wallets.clear()
                    if now >= next_poll:
                        addresses = list(self._wallets)
                        next_poll = now + self._interval
                        source = "polling"
                    else:
                        addresses = [a for a in self._wallets if a in signalled]
                        source = "websocket"

                for address in addresses:
                    if self._stop_event.is_set():
                        break
                    self._scan_one(address, source)
        finally:
            with self._lock:
                self._enabled = False
                if self._stop_event.is_set():
                    for details in self._wallets.values():
                        details["state"] = "stopped"


if __name__ == "__main__":
    print("Read-only WebSocket and polling monitor service loaded successfully.")
