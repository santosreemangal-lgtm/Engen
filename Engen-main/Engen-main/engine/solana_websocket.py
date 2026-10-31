"""Reconnectable, read-only Solana logsSubscribe client.

WebSocket notifications are hints to trigger a fresh RPC scan; they are never
used as trade instructions or as a replacement for confirmed transaction data.
"""
import json
import os
import threading
import time
from collections import OrderedDict

from engine.solana_public import is_valid_transaction_signature


DEFAULT_WS_URL = os.environ.get("ENGEN_WS_URL", "").strip() or "wss://api.mainnet-beta.solana.com"


class SolanaLogsWebSocket:
    """Subscribe to confirmed wallet logs with bounded reconnect and deduplication."""

    def __init__(
        self,
        endpoint=DEFAULT_WS_URL,
        *,
        connect_timeout=5,
        receive_timeout=1,
        reconnect_initial=1,
        reconnect_max=30,
        max_seen_signatures=2048,
        connect_function=None,
    ):
        if not isinstance(endpoint, str) or not endpoint.startswith(("ws://", "wss://")):
            raise ValueError("Solana WebSocket endpoint must use ws:// or wss://.")
        for name, value in (
            ("connect_timeout", connect_timeout),
            ("receive_timeout", receive_timeout),
            ("reconnect_initial", reconnect_initial),
            ("reconnect_max", reconnect_max),
        ):
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                raise ValueError(name + " must be positive.")
        if reconnect_max < reconnect_initial:
            raise ValueError("reconnect_max must be at least reconnect_initial.")
        if not isinstance(max_seen_signatures, int) or isinstance(max_seen_signatures, bool) or max_seen_signatures < 1:
            raise ValueError("max_seen_signatures must be a positive integer.")

        self.endpoint = endpoint
        self.connect_timeout = float(connect_timeout)
        self.receive_timeout = float(receive_timeout)
        self.reconnect_initial = float(reconnect_initial)
        self.reconnect_max = float(reconnect_max)
        self.max_seen_signatures = max_seen_signatures
        self._connect_function = connect_function
        self._seen = OrderedDict()
        self._seen_lock = threading.Lock()

    def _connect(self):
        if self._connect_function is not None:
            return self._connect_function(self.endpoint, self.connect_timeout)
        import websocket
        return websocket.create_connection(
            self.endpoint,
            timeout=self.connect_timeout,
            enable_multithread=True,
            suppress_origin=True,
        )

    def _remember_signature(self, signature):
        """Return False for a duplicate; retain only a bounded recent signature set."""
        if not is_valid_transaction_signature(signature):
            return False
        with self._seen_lock:
            if signature in self._seen:
                self._seen.move_to_end(signature)
                return False
            self._seen[signature] = None
            while len(self._seen) > self.max_seen_signatures:
                self._seen.popitem(last=False)
            return True

    @staticmethod
    def _notify_state(callback, state, error_type=None):
        if callback is not None:
            try:
                callback(state, error_type)
            except Exception:
                pass

    def run(self, wallet_address, stop_event, on_event, on_state=None):
        """Run until stopped; reconnect with capped exponential backoff."""
        backoff = self.reconnect_initial
        while not stop_event.is_set():
            connection = None
            try:
                self._notify_state(on_state, "connecting")
                connection = self._connect()
                connection.settimeout(self.receive_timeout)
                connection.send(json.dumps({
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "logsSubscribe",
                    "params": [
                        {"mentions": [wallet_address]},
                        {"commitment": "confirmed"},
                    ],
                }))
                subscribed = False
                connected_at = time.monotonic()
                while not stop_event.is_set():
                    try:
                        raw_message = connection.recv()
                    except Exception as exc:
                        # websocket-client raises its own timeout exception;
                        # use the exception class name to avoid a hard import here.
                        if type(exc).__name__ in ("WebSocketTimeoutException", "TimeoutError"):
                            if not subscribed and time.monotonic() - connected_at > self.connect_timeout:
                                raise RuntimeError("WebSocket subscription acknowledgement timed out.")
                            continue
                        raise
                    if not raw_message:
                        raise RuntimeError("WebSocket connection closed.")
                    message = json.loads(raw_message)
                    if not isinstance(message, dict):
                        continue
                    if message.get("id") == 1:
                        if message.get("error"):
                            raise RuntimeError("WebSocket subscription was rejected.")
                        if "result" in message:
                            subscribed = True
                            backoff = self.reconnect_initial
                            self._notify_state(on_state, "connected")
                        continue
                    if message.get("method") != "logsNotification":
                        continue
                    params = message.get("params")
                    result = params.get("result") if isinstance(params, dict) else None
                    value = result.get("value") if isinstance(result, dict) else None
                    if not isinstance(value, dict) or value.get("err") is not None:
                        continue
                    signature = value.get("signature")
                    if not self._remember_signature(signature):
                        continue
                    try:
                        on_event(signature, message)
                    except Exception:
                        # A consumer failure must not terminate the socket reader.
                        continue
            except Exception as exc:
                if not stop_event.is_set():
                    self._notify_state(on_state, "reconnecting", type(exc).__name__)
            finally:
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass
            if not stop_event.is_set():
                self._notify_state(on_state, "reconnecting")
                stop_event.wait(backoff)
                backoff = min(self.reconnect_max, backoff * 2)
        self._notify_state(on_state, "stopped")
