"""Reject stale source-wallet swaps before paper-copy queueing.

Paper-only safety helper. Does not touch the chain.
"""

import time

# Source swaps older than this are not copied into the paper ledger.
MAX_SOURCE_AGE_SECONDS = 60
# Allow a few seconds of clock skew for "future" timestamps.
CLOCK_SKEW_SECONDS = 5


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
    if now - block_time > MAX_SOURCE_AGE_SECONDS:
        return False
    return True
