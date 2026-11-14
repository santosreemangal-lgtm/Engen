"""Reject stale source-wallet swaps before paper-copy queueing.

Paper-only safety helper. Does not touch the chain.
"""

import os
import time

# Source swaps older than this are not copied into the paper ledger.
MAX_SOURCE_AGE_SECONDS = 60
# Allow a few seconds of clock skew for "future" timestamps.
CLOCK_SKEW_SECONDS = 5


def _configured_max_source_age_seconds():
    raw = os.environ.get("ENGEN_MAX_SOURCE_AGE_SECONDS")
    if raw is None:
        return MAX_SOURCE_AGE_SECONDS
    try:
        value = int(raw.strip())
    except (AttributeError, TypeError, ValueError):
        return None
    return value if value > 0 else None


def source_block_time_ok(observation, *, now_ts=None):
    """True only when observation has a usable, fresh block_time."""
    if not isinstance(observation, dict):
        return False
    block_time = observation.get("block_time")
    if not isinstance(block_time, int) or isinstance(block_time, bool) or block_time <= 0:
        return False
    now = int(now_ts if now_ts is not None else time.time())
    if block_time > now + CLOCK_SKEW_SECONDS:
        return False
    max_age = _configured_max_source_age_seconds()
    if max_age is None or now - block_time > max_age:
        return False
    return True
