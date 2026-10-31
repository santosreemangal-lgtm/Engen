
"""Read-only monitoring service for saved public Solana wallets."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from engine.solana_validation import is_valid_solana_address


class MonitorService:
    """Manage opt-in, read-only monitoring for saved wallet addresses."""

    def __init__(self, scan_function, interval=15, max_workers=4):
        if not callable(scan_function):
            raise ValueError("scan_function must be callable.")

        if (
            not isinstance(interval, int)
            or isinstance(interval, bool)
            or interval < 15
        ):
            raise ValueError("Monitoring interval must be at least 15 seconds.")

        if (
            not isinstance(max_workers, int)
            or isinstance(max_workers, bool)
            or not 1 <= max_workers <= 16
        ):
            raise ValueError("max_workers must be an integer from 1 to 16.")

        self._scan_function = scan_function
        self._interval = interval
        self._max_workers = max_workers
        self._last_cycle_duration_ms = None
        self._last_cycle_started_at = None
        self._last_cycle_completed_at = None
        self._last_cycle_wallet_count = 0
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
            "last_successful_check": None,
            "last_error": None,
            "source_quality": "unknown",
            "rpc_error_count": 0,
            "retryable_count": 0,
            "pagination_incomplete": False,
            "pagination_pages_scanned": 0,
            "scan_count": 0,
            "scan_duration_ms": None,
            "scan_started_at": None,
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
                active_states = {"connecting", "scanning", "waiting", "stopping", "stopped", "error"}
                if (
                    self._interval > 0
                    and age > self._interval * 2
                    and snapshot.get("state") not in active_states
                ):
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
                "scan_workers": self._max_workers,
                "last_cycle_duration_ms": self._last_cycle_duration_ms,
                "last_cycle_started_at": self._last_cycle_started_at,
                "last_cycle_completed_at": self._last_cycle_completed_at,
                "last_cycle_wallet_count": self._last_cycle_wallet_count,
                "latency": (
                    self._scan_function.latency_recorder.summary()
                    if getattr(self._scan_function, "latency_recorder", None) is not None
                    else {}
                ),
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

    def _scan_one_wallet(self, address):
        """Run one wallet scan and update only that wallet's state."""
        started = time.perf_counter()
        started_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            if address not in self._wallets:
                return
            self._wallets[address]["state"] = "scanning"
            self._wallets[address]["scan_started_at"] = started_at

        try:
            results = self._scan_function(address)
            if not isinstance(results, list):
                raise TypeError("Monitor scan must return a list.")

            pagination_markers = [
                item for item in results
                if isinstance(item, dict) and item.get("scan_incomplete") is True
            ]
            pagination_incomplete = bool(pagination_markers)
            pagination_pages_scanned = max(
                (
                    int(item.get("pagination_pages_scanned", 0))
                    for item in pagination_markers
                    if isinstance(item.get("pagination_pages_scanned", 0), int)
                    and not isinstance(item.get("pagination_pages_scanned", 0), bool)
                ),
                default=0,
            )
            rpc_errors = [
                item for item in results
                if isinstance(item, dict)
                and item.get("metadata_status") == "rpc_error"
            ]
            retryable = [
                item for item in results
                if isinstance(item, dict) and item.get("retryable") is True
            ]
            rpc_codes = sorted({
                str(item.get("rpc_code"))
                for item in rpc_errors
                if isinstance(item.get("rpc_code"), int)
                and not isinstance(item.get("rpc_code"), bool)
            })
            if pagination_incomplete:
                source_quality = "incomplete"
                scan_error = str(
                    pagination_markers[0].get("pagination_error")
                    or "Wallet history pagination is incomplete; scanning will resume from the saved cursor."
                )
            elif rpc_errors:
                source_quality = "degraded"
                code_note = (
                    " (RPC codes: " + ", ".join(rpc_codes) + ")"
                    if rpc_codes else ""
                )
                scan_error = (
                    "Transaction details failed for "
                    + str(len(rpc_errors))
                    + " of "
                    + str(len(results))
                    + " pending signature(s)"
                    + code_note
                    + "."
                )
            elif retryable:
                source_quality = "pending"
                scan_error = (
                    str(len(retryable))
                    + " transaction(s) remain pending confirmation or metadata."
                )
            else:
                source_quality = "healthy"
                scan_error = None

            finished_at = datetime.now(timezone.utc).isoformat()
            duration_ms = round((time.perf_counter() - started) * 1000, 3)
            with self._lock:
                if address in self._wallets:
                    wallet = self._wallets[address]
                    wallet.update({
                        "state": "connected",
                        "last_scan": finished_at,
                        "last_successful_check": finished_at,
                        "last_error": scan_error,
                        "source_quality": source_quality,
                        "rpc_error_count": len(rpc_errors),
                        "retryable_count": len(retryable),
                        "pagination_incomplete": pagination_incomplete,
                        "pagination_pages_scanned": pagination_pages_scanned,
                        "scan_count": wallet.get("scan_count", 0) + 1,
                        "scan_duration_ms": duration_ms,
                        "latest_results": results[:20],
                    })
        except Exception as exc:
            provider_code = getattr(exc, "code", None)
            error_detail = type(exc).__name__
            if isinstance(provider_code, int) and not isinstance(provider_code, bool):
                error_detail += "; RPC code " + str(provider_code)
            finished_at = datetime.now(timezone.utc).isoformat()
            duration_ms = round((time.perf_counter() - started) * 1000, 3)
            with self._lock:
                if address in self._wallets:
                    wallet = self._wallets[address]
                    wallet.update({
                        "state": "error",
                        "last_scan": finished_at,
                        "last_error": "Monitoring scan failed (" + error_detail + ").",
                        "source_quality": "unavailable",
                        "scan_count": wallet.get("scan_count", 0) + 1,
                        "scan_duration_ms": duration_ms,
                    })

    def _run(self):
        """Poll wallets concurrently with a bounded worker pool and fixed-rate cadence."""
        try:
            while not self._stop_event.is_set():
                cycle_started = time.perf_counter()
                cycle_started_at = datetime.now(timezone.utc).isoformat()
                next_cycle_due = cycle_started + self._interval
                with self._lock:
                    addresses = list(self._wallets.keys())

                workers = min(self._max_workers, len(addresses))
                if workers:
                    with ThreadPoolExecutor(
                        max_workers=workers,
                        thread_name_prefix="solana-wallet-scan",
                    ) as executor:
                        futures = {
                            executor.submit(self._scan_one_wallet, address): address
                            for address in addresses
                            if not self._stop_event.is_set()
                        }
                        for future in as_completed(futures):
                            # _scan_one_wallet contains scan exceptions. This
                            # guard protects the service from an unexpected worker bug.
                            try:
                                future.result()
                            except Exception:
                                continue

                finished_at = datetime.now(timezone.utc).isoformat()
                duration_ms = round((time.perf_counter() - cycle_started) * 1000, 3)
                with self._lock:
                    self._last_cycle_duration_ms = duration_ms
                    self._last_cycle_started_at = cycle_started_at
                    self._last_cycle_completed_at = finished_at
                    self._last_cycle_wallet_count = len(addresses)

                # Schedule relative to cycle start, not cycle completion.
                # Slow scans therefore do not add another full interval.
                remaining = max(0.0, next_cycle_due - time.perf_counter())
                self._stop_event.wait(remaining)

        finally:
            with self._lock:
                self._enabled = False
                if self._stop_event.is_set():
                    for details in self._wallets.values():
                        details["state"] = "stopped"



if __name__ == "__main__":
    print("Read-only monitoring service loaded successfully.")
