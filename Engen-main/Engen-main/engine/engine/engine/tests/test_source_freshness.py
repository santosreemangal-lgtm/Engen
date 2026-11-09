"""Tests for source_freshness window used before paper-copy queueing."""
import time
import unittest

from engine.source_freshness import (
    CLOCK_SKEW_SECONDS,
    MAX_SOURCE_AGE_SECONDS,
    source_block_time_ok,
)


class SourceFreshnessTests(unittest.TestCase):
    def test_fresh_block_time_ok(self):
        now = int(time.time())
        self.assertTrue(
            source_block_time_ok({"block_time": now - 10}, now_ts=now)
        )

    def test_missing_block_time_rejected(self):
        self.assertFalse(source_block_time_ok({}))
        self.assertFalse(source_block_time_ok({"block_time": None}))
        self.assertFalse(source_block_time_ok({"block_time": "bad"}))

    def test_stale_block_time_rejected(self):
        now = int(time.time())
        stale = now - MAX_SOURCE_AGE_SECONDS - 1
        self.assertFalse(
            source_block_time_ok({"block_time": stale}, now_ts=now)
        )

    def test_future_block_time_rejected(self):
        now = int(time.time())
        future = now + CLOCK_SKEW_SECONDS + 10
        self.assertFalse(
            source_block_time_ok({"block_time": future}, now_ts=now)
        )

    def test_small_clock_skew_allowed(self):
        now = int(time.time())
        slightly_future = now + min(2, CLOCK_SKEW_SECONDS)
        self.assertTrue(
            source_block_time_ok({"block_time": slightly_future}, now_ts=now)
        )


if __name__ == "__main__":
    unittest.main()
