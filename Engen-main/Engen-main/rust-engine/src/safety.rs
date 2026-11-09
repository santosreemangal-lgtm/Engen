//! Fail-closed policy. This crate has no live execution capability.

use std::fmt;

pub const LIVE_EXECUTION_IMPLEMENTED: bool = false;
pub const LIVE_TRADING_ENABLED: bool = false;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LiveExecutionDisabled {
    pub reason: &'static str,
}

impl fmt::Display for LiveExecutionDisabled {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.reason)
    }
}

impl std::error::Error for LiveExecutionDisabled {}

/// Shared guard for any future code that might be proposed as an execution adapter.
///
/// It always fails in this release. It intentionally has no disarm or override API.
pub fn block_live_execution() -> Result<(), LiveExecutionDisabled> {
    Err(LiveExecutionDisabled {
        reason: "Live signing and transaction broadcast are not implemented or authorized.",
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn live_execution_is_unconditionally_blocked() {
        assert!(!LIVE_EXECUTION_IMPLEMENTED);
        assert!(!LIVE_TRADING_ENABLED);
        assert!(block_live_execution().is_err());
    }
}
