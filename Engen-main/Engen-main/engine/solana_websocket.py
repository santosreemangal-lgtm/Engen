"""Optional read-only Solana WebSocket notifications for faster paper-monitor wakeups.

Notifications are hints, not transaction proof. The existing HTTP RPC scanner still
fetches and validates transaction metadata and remains the recovery path.
"""
import json
import threading
import time
from urllib.parse import urlsplit, urlunsplit


def websocket_url_from_rpc(rpc_url):
    """Convert an HTTP(S) JSON-RPC URL to its WS(S) equivalent."""
    if not isinstance(rpc_url, str) or not rpc_url.strip():
        raise ValueError("A Solana RPC URL is required.")
    parts = urlsplit(rpc_url.strip())
    scheme = {"https": "wss", "http": "ws"}.get(parts.scheme.lower())
    if scheme is None or not parts.netloc:
        raise ValueError("Solana RPC URL must use http or https.")
    return urlunsplit((scheme, parts.netloc, parts.path, parts.query, ""))


class SolanaWebSocketWakeup:
    """Subscribe to wallet log notifications and wake a read-only scanner.

    The dependency is imported lazily so offline tests and deployments without
    the optional package continue to use the bounded HTTP polling fallback.
    """

    def __init__(self, websocket_url, reconnect_max_seconds=30):
        if not isinstance(websocket_url, str) or not websocket_url.strip():
            raise ValueError("A WebSocket endpoint is required.")
        parts = urlsplit(websocket_url.strip())
        if parts.scheme not in ("ws", "wss") or not parts.netloc:
            raise ValueError("WebSocket endpoint must use ws or wss.")
        if (
            not isinstance(reconnect_max_seconds, (int, float))
            or isinstance(reconnect_max_seconds, bool)
            or reconnect_max_seconds < 1
        ):
            raise ValueError("Reconnect maximum must be at least one second.")
        self._url = websocket_url.strip()
        self._reconnect_max_seconds = float(reconnect_max_seconds)
        self._lock = threading.RLock()
        self._addresses = []
        self._callback = None
        self._stop_event = threading.Event()
        self._refresh_event = threading.Event()
        self._thread = None
        self._last_error = None
        self._notifications = 0
        self._reconnects = 0

    def status(self):
        with self._lock:
            return {
                "enabled": True,
                "connected": self._thread is not None and self._thread.is_alive()
                    and self._last_error is None,
                "subscribed_wallet_count": len(self._addresses),
                "notifications": self._notifications,
                "reconnects": self._reconnects,
                "last_error": self._last_error,
                "mode": "READ_ONLY_NOTIFICATIONS",
            }

    def update_addresses(self, addresses, callback):
        if not isinstance(addresses, list) or not callable(callback):
            raise ValueError("Wallet addresses and a callback are required.")
        cleaned = list(dict.fromkeys(
            item.strip() for item in addresses
            if isinstance(item, str) and item.strip()
        ))
        with self._lock:
            changed = cleaned != self._addresses or callback is not self._callback
            self._addresses = cleaned
            self._callback = callback
            if self._thread is None or not self._thread.is_alive():
                self._stop_event.clear()
                self._thread = threading.Thread(
                    target=self._run, name="solana-websocket-wakeup", daemon=True
                )
                self._thread.start()
            elif changed:
                self._refresh_event.set()

    def stop(self, timeout=3):
        self._stop_event.set()
        self._refresh_event.set()
        worker = self._thread
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=timeout)

    def _snapshot(self):
        with self._lock:
            return list(self._addresses), self._callback

    def _dispatch_notification(self, message, subscription_to_address):
        if not isinstance(message, dict) or message.get("method") != "logsNotification":
            return
        params = message.get("params")
        if not isinstance(params, dict):
            return
        address = subscription_to_address.get(params.get("subscription"))
        result = params.get("result")
        value = result.get("value") if isinstance(result, dict) else None
        signature = value.get("signature") if isinstance(value, dict) else None
        if not address or not isinstance(signature, str) or not signature:
            return
        _, callback = self._snapshot()
        if callback is None:
            return
        with self._lock:
            self._notifications += 1
        try:
            callback(address)
        except Exception:
            # A wakeup is advisory; the polling loop remains the recovery path.
            pass

    def _run(self):
        delay = 1.0
        while not self._stop_event.is_set():
            self._refresh_event.clear()
            addresses, _ = self._snapshot()
            if not addresses:
                self._stop_event.wait(1.0)
                continue
            try:
                # Lazy import keeps all offline/read-only tests independent of
                # the optional network transport dependency.
                from websockets.sync.client import connect

                with connect(
                    self._url, open_timeout=8, close_timeout=2,
                    ping_interval=20, ping_timeout=20, max_size=1_000_000,
                ) as websocket:
                    subscription_to_address = {}
                    request_to_address = {}
                    for request_id, address in enumerate(addresses, start=1):
                        request_to_address[request_id] = address
                        websocket.send(json.dumps({
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "method": "logsSubscribe",
                            "params": [
                                {"mentions": [address]},
                                {"commitment": "confirmed"},
                            ],
                        }))
                    pending = set(request_to_address)
                    deadline = time.monotonic() + 10
                    while pending and not self._stop_event.is_set():
                        if self._refresh_event.is_set() or time.monotonic() >= deadline:
                            break
                        try:
                            raw = websocket.recv(timeout=1)
                        except TimeoutError:
                            continue
                        message = json.loads(raw) if isinstance(raw, str) else {}
                        request_id = message.get("id") if isinstance(message, dict) else None
                        if request_id in pending:
                            pending.remove(request_id)
                            subscription_id = message.get("result")
                            if isinstance(subscription_id, int) and not isinstance(subscription_id, bool):
                                subscription_to_address[subscription_id] = request_to_address[request_id]
                        else:
                            self._dispatch_notification(message, subscription_to_address)
                    if pending and not self._stop_event.is_set() and not self._refresh_event.is_set():
                        raise TimeoutError("WebSocket subscription acknowledgement timed out.")
                    delay = 1.0
                    with self._lock:
                        self._last_error = None
                    while not self._stop_event.is_set() and not self._refresh_event.is_set():
                        try:
                            raw = websocket.recv(timeout=1)
                        except TimeoutError:
                            continue
                        try:
                            message = json.loads(raw) if isinstance(raw, str) else {}
                        except (TypeError, json.JSONDecodeError):
                            continue
                        self._dispatch_notification(message, subscription_to_address)
            except Exception as exc:
                with self._lock:
                    self._last_error = type(exc).__name__
                    self._reconnects += 1
                self._stop_event.wait(delay)
                delay = min(self._reconnect_max_seconds, delay * 2)
