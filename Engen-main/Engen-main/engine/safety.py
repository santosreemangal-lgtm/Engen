"""Fail-closed execution safety guard for the read-only/paper-only project."""

from dataclasses import dataclass


class LiveExecutionDisabledError(RuntimeError):
    """Raised whenever a caller requests real transaction execution."""


@dataclass(frozen=True)
class EmergencyStopLatch:
    """An in-memory, default-engaged latch for the current non-live engine.

    The current release has no authorized live execution mode, so this latch
    intentionally exposes no disarm operation. A future execution proposal
    must replace/extend it only after a separate safety review.
    """

    engaged: bool = True
    reason: str = "Live execution is not available in this release."

    def require_execution_permitted(self):
        """Fail closed rather than allowing a signing/broadcast operation."""
        if self.engaged:
            raise LiveExecutionDisabledError(self.reason)
        # Even a manually constructed disengaged instance must not authorize
        # execution in this release.
        raise LiveExecutionDisabledError(
            "Live execution is disabled by project policy."
        )


EMERGENCY_STOP = EmergencyStopLatch()


def block_live_execution():
    """Shared guard for any future signing or transaction-broadcast adapter."""
    EMERGENCY_STOP.require_execution_permitted()
