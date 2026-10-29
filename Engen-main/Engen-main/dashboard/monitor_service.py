
"""Read-only monitoring service for saved public Solana wallets."""

import threading
import time

from engine.solana_validation import is_valid_solana_address
from datetime import datetime, timezone


class MonitorService:
    """Manage opt-in, read-only monitoring for saved wallet addresses."""

    def __init__(self, scan_function, interval=15):
        if not callable(scan_function):
            raise ValueError("scan_function must be callable.")

        if (
            not isinstance(interval, int)
            or isinstance(interval, bool)
            or interval < 15
        ):
            raise ValueError("Monitoring interval must be at least 15 seconds.")

        self._scan_function = scan_function
        self._interval = interval
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None
        self._wallets = {}
        self._enabled = False

    @staticmethod
    def _new_wallet_state():
        return {
            "state": "waiting",
            "last_scan": None,
            "last_scan_duration_ms": None,
            "last_successful_check": None,
            "last_error": None,
            "latest_results": [],
        }

    def _status_details(self, details):
        """Report monitors as stale when their last successful check is old."""
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
        """Return a safe snapshot of monitoring state."""
        with self._lock:
            return {
                "enabled": self._enabled,
                "interval_seconds": self._interval,
                "wallets": [
                    {
                        "address": address,
                        **self._status_details(details),
                    }
                    for address, details in self._wallets.items()
                ],
            }

    def start(self, addresses):
        """Start monitoring explicitly for a list of public addresses."""
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

        # Serialize start/stop operations so two callers cannot create
        # overlapping monitor threads.
        with self._lifecycle_lock:
            with self._lock:
                previous_thread = self._thread

                if (
                    previous_thread is not None
                    and previous_thread.is_alive()
                    and not self._stop_event.is_set()
                ):
                    # Monitoring is already active. Update the wallet list
                    # without starting a second worker.
                    self._wallets = {
                        address: self._wallets.get(
                            address,
                            self._new_wallet_state(),
                        )
                        for address in cleaned
                    }
                    self._enabled = True
                    return self.status()

            # If a stop was requested, let the old worker finish before
            # clearing the shared stop event or creating another worker.
            if (
                previous_thread is not None
                and previous_thread.is_alive()
            ):
                previous_thread.join()

            with self._lock:
                self._wallets = {
                    address: self._new_wallet_state()
                    for address in cleaned
                }

                self._stop_event.clear()
                self._enabled = True

                self._thread = threading.Thread(
                    target=self._run,
                    name="solana-read-only-monitor",
                    daemon=True,
                )
                self._thread.start()

                return self.status()

    def stop(self):
        """Request a graceful stop without blocking the caller."""
        with self._lifecycle_lock:
            self._stop_event.set()

            with self._lock:
                self._enabled = False
                for details in self._wallets.values():
                    if details.get("state") not in ("error", "stale"):
                        details["state"] = "stopping"
                return self.status()

    def stop_and_wait(self, timeout=15):
        """Request stop and wait a bounded time for the worker to exit."""
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or timeout <= 0
        ):
            raise ValueError("Shutdown timeout must be positive.")

        self.stop()
        with self._lock:
            worker = self._thread

        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=timeout)

        return {
            **self.status(),
            "worker_stopped": worker is None or not worker.is_alive(),
        }

    def _run(self):
        """Poll saved addresses without signing or submitting transactions."""
        try:
            while not self._stop_event.is_set():
                with self._lock:
                    addresses = list(self._wallets.keys())

                for address in addresses:
                    if self._stop_event.is_set():
                        break

                    with self._lock:
                        if address not in self._wallets:
                            continue

                        self._wallets[address]["state"] = "scanning"

                    scan_started_ns = time.perf_counter_ns()
                    try:
                        results = self._scan_function(address)

                        if not isinstance(results, list):
                            results = []

                        with self._lock:
                            if address in self._wallets:
                                self._wallets[address].update(
                                    {
                                        "state": "connected",
                                        "last_scan": datetime.now(
                                            timezone.utc
                                        ).isoformat(),
                                        "last_scan_duration_ms": round((time.perf_counter_ns() - scan_started_ns) / 1_000_000, 3),
                                        "last_successful_check": datetime.now(
                                            timezone.utc
                                        ).isoformat(),
                                        "last_error": None,
                                        "latest_results": results[:20],
                                    }
                                )

                    except Exception:
                        with self._lock:
                            if address in self._wallets:
                                self._wallets[address].update(
                                    {
                                        "state": "error",
                                        "last_scan": datetime.now(
                                            timezone.utc
                                        ).isoformat(),
                                        "last_scan_duration_ms": round((time.perf_counter_ns() - scan_started_ns) / 1_000_000, 3),
                                        "last_error": "Monitoring scan failed; sensitive error details are hidden.",
                                    }
                                )

                self._stop_event.wait(self._interval)

        finally:
            with self._lock:
                # The lifecycle lock ensures a replacement worker cannot
                # start until this worker has completely exited.
                self._enabled = False
                if self._stop_event.is_set():
                    for details in self._wallets.values():
                        details["state"] = "stopped"


if __name__ == "__main__":
    print("Read-only monitoring service loaded successfully.")
