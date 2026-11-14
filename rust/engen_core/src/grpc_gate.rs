//! Yellowstone gRPC is not part of this paper binary.
//!
//! A future live build may subscribe here. This gate refuses that path so the
//! paper executable cannot pretend it has a sub-100ms detection feed.

use crate::{EngenError, Result};

pub fn yellowstone_subscribe(_endpoint: &str) -> Result<()> {
    Err(EngenError::LiveDisabled)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn grpc_stays_disabled() {
        let err = yellowstone_subscribe("https://example.invalid").unwrap_err();
        assert!(err.to_string().contains("signing and broadcast are disabled") || err.to_string().contains("paper only"));
    }
}
