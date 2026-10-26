"""Shared read-only JSON-RPC transport policy: pacing and bounded retries."""

import json
import os
import threading
import time
import urllib.error
import urllib.request


_lock = threading.Lock()
_last_request_at = 0.0


def _non_negative_int_env(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def _non_negative_float_env(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if value < 0 or value != value or value == float("inf"):
        return default
    return value


def _pace_requests(min_interval):
    """Apply a process-wide minimum gap between outbound RPC requests."""
    global _last_request_at
    with _lock:
        now = time.monotonic()
        wait_for = min_interval - (now - _last_request_at)
        if wait_for > 0:
            time.sleep(wait_for)
        _last_request_at = time.monotonic()


def post_json_rpc(payload, rpc_url, timeout):
    """Post a JSON-RPC payload with bounded transport retries and pacing.

    Environment controls:
      ENGEN_RPC_RETRIES: retry attempts after the first request (default 2)
      ENGEN_RPC_BACKOFF_SECONDS: exponential backoff base (default 0.5)
      ENGEN_RPC_MIN_INTERVAL_SECONDS: minimum gap between requests (default 0.1)

    Error messages deliberately omit the configured URL and chained exceptions.
    """
    retries = _non_negative_int_env("ENGEN_RPC_RETRIES", 2)
    backoff = _non_negative_float_env("ENGEN_RPC_BACKOFF_SECONDS", 0.5)
    min_interval = _non_negative_float_env(
        "ENGEN_RPC_MIN_INTERVAL_SECONDS", 0.1
    )
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        rpc_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    for attempt in range(retries + 1):
        _pace_requests(min_interval)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("invalid JSON-RPC response")
            return data
        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code <= 599
            if not retryable or attempt >= retries:
                raise RuntimeError(
                    "Public Solana RPC request failed."
                ) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt >= retries:
                raise RuntimeError(
                    "Public Solana RPC request failed."
                ) from None
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise RuntimeError(
                "Public Solana RPC returned an invalid response."
            ) from None

        if backoff:
            time.sleep(backoff * (2 ** attempt))

    raise RuntimeError("Public Solana RPC request failed.")
