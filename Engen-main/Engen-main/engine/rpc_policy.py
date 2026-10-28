"""Shared read-only JSON-RPC transport policy: pacing and bounded retries."""

import json
import math
import os
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit


_lock = threading.Lock()
_last_request_at = 0.0
MAX_RPC_RETRIES = 5


class RPCTransportError(RuntimeError):
    """A transport/network failure after bounded retries."""


class RPCHTTPError(RuntimeError):
    """An HTTP failure after bounded retries, with a safe status code."""

    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__("Public Solana RPC HTTP request failed.")


class RPCInvalidResponseError(RuntimeError):
    """The provider returned invalid JSON or a non-object response."""


def validate_rpc_url(rpc_url):
    """Require HTTPS for remote RPC; permit plain HTTP only for loopback."""
    if not isinstance(rpc_url, str) or not rpc_url.strip():
        raise ValueError("ENGEN_RPC_URL must be a non-empty URL.")
    try:
        parsed = urlsplit(rpc_url.strip())
        hostname = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise ValueError("ENGEN_RPC_URL is malformed.") from exc
    if not hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("ENGEN_RPC_URL must include a host and must not embed credentials.")
    if parsed.fragment:
        raise ValueError("ENGEN_RPC_URL must not contain a fragment.")
    scheme = parsed.scheme.lower()
    loopback = hostname.lower() in {"localhost", "127.0.0.1", "::1"}
    if scheme != "https" and not (scheme == "http" and loopback):
        raise ValueError("ENGEN_RPC_URL must use HTTPS; HTTP is allowed only for localhost.")
    return rpc_url.strip()


def validate_rpc_timeout(timeout):
    """Bound network timeouts to a finite, positive interval."""
    if isinstance(timeout, bool):
        raise ValueError("RPC timeout must be between 0 and 60 seconds.")
    try:
        value = float(timeout)
    except (TypeError, ValueError) as exc:
        raise ValueError("RPC timeout must be between 0 and 60 seconds.") from exc
    if not math.isfinite(value) or not 0 < value <= 60:
        raise ValueError("RPC timeout must be between 0 and 60 seconds.")
    return value


def _non_negative_int_env(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("%s must be a non-negative integer." % name) from exc
    if value < 0:
        raise ValueError("%s must be a non-negative integer." % name)
    # A large value is capped rather than allowed to create an unbounded retry loop.
    return min(value, MAX_RPC_RETRIES)


def _non_negative_float_env(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("%s must be a finite number between 0 and 60." % name) from exc
    if not math.isfinite(value) or value < 0 or value > 60:
        raise ValueError("%s must be a finite number between 0 and 60." % name)
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
    """Post JSON-RPC with bounded retries, pacing, and typed safe errors.

    ENGEN_RPC_RETRIES is the number of retries after the first attempt.
    Backoff is exponential and the retry loop is always finite.
    """
    rpc_url = validate_rpc_url(rpc_url)
    timeout = validate_rpc_timeout(timeout)
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
                raw = response.read().decode("utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise RPCInvalidResponseError(
                    "Public Solana RPC returned an invalid response."
                )
            return data
        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code <= 599
            if not retryable or attempt >= retries:
                raise RPCHTTPError(exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt >= retries:
                raise RPCTransportError(
                    "Public Solana RPC request failed."
                ) from None
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RPCInvalidResponseError(
                "Public Solana RPC returned an invalid response."
            ) from None

        if backoff:
            time.sleep(backoff * (2 ** attempt))

    raise RPCTransportError("Public Solana RPC request failed.")
