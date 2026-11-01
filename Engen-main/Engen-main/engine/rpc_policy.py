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

    def __init__(self, diagnostics=None):
        self.diagnostics = dict(diagnostics or {})
        super().__init__("Public Solana RPC request failed.")


class RPCHTTPError(RuntimeError):
    """An HTTP failure after bounded retries, with safe diagnostic metadata."""

    def __init__(self, status_code, diagnostics=None):
        self.status_code = status_code
        self.diagnostics = dict(diagnostics or {})
        super().__init__("Public Solana RPC HTTP request failed.")


class RPCInvalidResponseError(RuntimeError):
    """The provider returned invalid JSON or a non-object response."""

    def __init__(self, diagnostics=None):
        self.diagnostics = dict(diagnostics or {})
        super().__init__("Public Solana RPC returned an invalid response.")


class RPCResponse(dict):
    """A normal JSON-RPC mapping with out-of-band, secret-free request metadata."""

    def __init__(self, value, diagnostics):
        super().__init__(value)
        self.diagnostics = dict(diagnostics)


def _diagnostics(*, started_at, attempts, failure_kind=None,
                 exception_class=None, http_status=None, retry_after=None,
                 json_rpc_code=None):
    return {
        "failure_kind": failure_kind,
        "http_status": http_status,
        "json_rpc_code": json_rpc_code,
        "exception_class": exception_class,
        "retry_after": retry_after,
        "request_duration_ms": round(max(0.0, time.monotonic() - started_at) * 1000, 3),
        "attempts": attempts,
    }


def _retry_after_header(exc):
    headers = getattr(exc, "headers", None)
    value = headers.get("Retry-After") if headers is not None else None
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 128 or any(ord(char) < 32 for char in value):
        return None
    return value


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
    """Post JSON-RPC with bounded retries and secret-free failure diagnostics.

    ENGEN_RPC_RETRIES is the number of retries after the first attempt.
    Diagnostic metadata deliberately excludes the endpoint URL and exception text.
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
    started_at = time.monotonic()

    for attempt in range(retries + 1):
        _pace_requests(min_interval)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                details = _diagnostics(
                    started_at=started_at, attempts=attempt + 1,
                    failure_kind="invalid_response",
                    exception_class="InvalidResponse",
                )
                raise RPCInvalidResponseError(details)
            details = _diagnostics(
                started_at=started_at, attempts=attempt + 1,
            )
            return RPCResponse(data, details)
        except urllib.error.HTTPError as exc:
            retry_after = _retry_after_header(exc)
            retryable = exc.code == 429 or 500 <= exc.code <= 599
            if not retryable or attempt >= retries:
                details = _diagnostics(
                    started_at=started_at, attempts=attempt + 1,
                    failure_kind="http_429" if exc.code == 429 else "http_error",
                    exception_class=type(exc).__name__,
                    http_status=exc.code, retry_after=retry_after,
                )
                raise RPCHTTPError(exc.code, details) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", None)
            underlying = reason if isinstance(reason, BaseException) else exc
            is_timeout = isinstance(underlying, TimeoutError)
            if attempt >= retries:
                details = _diagnostics(
                    started_at=started_at, attempts=attempt + 1,
                    failure_kind="timeout" if is_timeout else "network_error",
                    exception_class=type(underlying).__name__,
                )
                raise RPCTransportError(details) from None
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            details = _diagnostics(
                started_at=started_at, attempts=attempt + 1,
                failure_kind="invalid_response",
                exception_class=type(exc).__name__,
            )
            raise RPCInvalidResponseError(details) from None

        if backoff:
            time.sleep(backoff * (2 ** attempt))

    details = _diagnostics(
        started_at=started_at, attempts=retries + 1,
        failure_kind="network_error", exception_class="UnknownTransportError",
    )
    raise RPCTransportError(details) from None
