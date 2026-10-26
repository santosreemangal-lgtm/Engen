import unittest
from dataclasses import replace

from engine.safety import (
    EMERGENCY_STOP,
    EmergencyStopLatch,
    LiveExecutionDisabledError,
    block_live_execution,
)


class EmergencyStopTests(unittest.TestCase):
    def test_default_latch_is_engaged_after_startup(self):
        self.assertTrue(EMERGENCY_STOP.engaged)
        self.assertTrue(EMERGENCY_STOP.reason)

    def test_live_execution_is_blocked_by_default(self):
        with self.assertRaises(LiveExecutionDisabledError):
            block_live_execution()

    def test_latch_has_no_public_disarm_method(self):
        self.assertFalse(hasattr(EMERGENCY_STOP, "disarm"))
        self.assertFalse(hasattr(EMERGENCY_STOP, "release"))

    def test_even_disengaged_instance_cannot_authorize_live_execution(self):
        attempted_override = replace(
            EmergencyStopLatch(),
            engaged=False,
            reason="test override",
        )
        with self.assertRaisesRegex(
            LiveExecutionDisabledError,
            "disabled by project policy",
        ):
            attempted_override.require_execution_permitted()


if __name__ == "__main__":
    unittest.main()
