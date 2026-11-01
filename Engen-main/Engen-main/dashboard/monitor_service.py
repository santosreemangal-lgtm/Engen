
"""Read-only monitoring service for saved public Solana wallets."""

import threading
import time

from engine.solana_validation import is_valid_solana_address
from datetime import datetime, timezone


class MonitorService:
    """Manage opt-in, read-only monitoring for saved wallet addresses."""

    def __init__(self, scan_function, interval=15, metrics_function=None):
        if not callable(scan_function):
            raise ValueError("scan_function must be callable.")

        if (
            not isinstance(interval, int)
            or isinstance(interval, bool)
            or interval < 15
        ):
            raise ValueError("Monitoring interval must be at least 15 seconds.")

        self._scan_function = scan_function
        self._metrics_function = metrics_function or getattr(scan_function, "metrics_function", None)
        self._backlog_samples = {}
        self._interval = interval
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None
        self._wallets = {}
        self._enabled = False
        self._last_cycle_duration_seconds = None

    @staticmethod
    def _new_wallet_state():
        return {
            "state": "waiting",
            "last_scan": None,
            "last_successful_check": None,
            "last_error": None,
            "latest_results": [],
            "scan_count": 0,
            "last_scan_duration_seconds": None,
            "last_result_count": 0,
            "last_signatures_attempted_count": 0,
            "last_retry_count": 0,
            "last_needs_review_count": 0,
            "last_discovery_warning": False,
            "backlog_metrics": None,
            "backlog_delta_since_last_scan": None,
            "backlog_net_drain_per_minute": None,
        }

    def _read_backlog_metrics(self, address):
        """Read durable queue metrics without allowing diagnostics to break scanning."""
        if not callable(self._metrics_function):
            return None
        try:
            metrics = self._metrics_function(address)
        except Exception:
            return None
        return dict(metrics) if isinstance(metrics, dict) else None

    def _sample_backlog(self, address, metrics):
        """Calculate observed net backlog change per minute between scan samples."""
        if not isinstance(metrics, dict):
            return None, None
        pending = metrics.get("pending_count")
        if not isinstance(pending, int) or isinstance(pending, bool) or pending < 0:
            return None, None

        sampled_at = time.monotonic()
        previous = self._backlog_samples.get(address)
        self._backlog_samples[address] = (sampled_at, pending)
        if previous is None:
            return None, None

        elapsed = sampled_at - previous[0]
        if elapsed <= 0:
            return previous[1] - pending, None
        delta = previous[1] - pending
        return delta, round((delta * 60.0) / elapsed, 3)

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
                "last_cycle_duration_seconds": self._last_cycle_duration_seconds,
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
                self._backlog_samples = {}

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

                cycle_started = time.monotonic()
                for address in addresses:
                    if self._stop_event.is_set():
                        break

                    with self._lock:
                        if address not in self._wallets:
                            continue

                        self._wallets[address]["state"] = "scanning"

                    scan_started = time.monotonic()
                    try:
                        results = self._scan_function(address)

                        if not isinstance(results, list):
                            results = []

                        backlog_metrics = self._read_backlog_metrics(address)
                        backlog_delta, backlog_rate = self._sample_backlog(
                            address, backlog_metrics
                        )
                        retry_count = sum(
                            1 for item in results
                            if isinstance(item, dict) and item.get("retryable") is True
                        )
                        needs_review_count = sum(
                            1 for item in results
                            if isinstance(item, dict)
                            and item.get("processing_status") == "needs_review"
                        )
                        discovery_warning = any(
                            isinstance(item, dict) and bool(item.get("discovery_warning"))
                            for item in results
                        )
                        scan_duration = max(0.0, time.monotonic() - scan_started)
                        with self._lock:
                            if address in self._wallets:
                                self._wallets[address].update(
                                    {
                                        "state": "connected",
                                        "scan_count": self._wallets[address].get("scan_count", 0) + 1,
                                        "last_scan_duration_seconds": round(scan_duration, 3),
                                        "last_result_count": len(results),
                                        "last_signatures_attempted_count": len(results),
                                        "last_retry_count": retry_count,
                                        "last_needs_review_count": needs_review_count,
                                        "last_discovery_warning": discovery_warning,
                                        "backlog_metrics": backlog_metrics,
                                        "backlog_delta_since_last_scan": backlog_delta,
                                        "backlog_net_drain_per_minute": backlog_rate,
                                        "last_scan": datetime.now(
                                            timezone.utc
                                        ).isoformat(),
                                        "last_successful_check": datetime.now(
                                            timezone.utc
                                        ).isoformat(),
                                        "last_error": None,
                                        "latest_results": results[:20],
                                    }
                                )

                    except Exception:
                        backlog_metrics = self._read_backlog_metrics(address)
                        backlog_delta, backlog_rate = self._sample_backlog(
                            address, backlog_metrics
                        )
                        scan_duration = max(0.0, time.monotonic() - scan_started)
                        with self._lock:
                            if address in self._wallets:
                                self._wallets[address].update(
                                    {
                                        "state": "error",
                                        "scan_count": self._wallets[address].get("scan_count", 0) + 1,
                                        "backlog_metrics": backlog_metrics,
                                        "backlog_delta_since_last_scan": backlog_delta,
                                        "backlog_net_drain_per_minute": backlog_rate,
                                        "last_scan_duration_seconds": round(scan_duration, 3),
                                        "last_scan": datetime.now(
                                            timezone.utc
                                        ).isoformat(),
                                        "last_error": "Monitoring scan failed; sensitive error details are hidden.",
                                    }
                                )

                # Cycle duration excludes the configured idle interval and reports
                # actual wallet sweep time, including provider/network latency.
                with self._lock:
                    self._last_cycle_duration_seconds = round(
                        max(0.0, time.monotonic() - cycle_started), 3
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
