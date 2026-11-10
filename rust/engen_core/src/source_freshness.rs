//! Port of engine/source_freshness.py — reject stale source swaps.

use std::time::{SystemTime, UNIX_EPOCH};

pub const MAX_SOURCE_AGE_SECONDS: i64 = 60;
pub const CLOCK_SKEW_SECONDS: i64 = 5;

/// `block_time` is Solana unix seconds. Missing/invalid/stale/future → false.
pub fn source_block_time_ok(block_time: Option<i64>, now_ts: Option<i64>) -> bool {
    let Some(bt) = block_time else {
        return false;
    };
    if bt <= 0 {
        return false;
    }
    let now = now_ts.unwrap_or_else(|| {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs() as i64)
            .unwrap_or(0)
    });
    if bt > now + CLOCK_SKEW_SECONDS {
        return false;
    }
    if now - bt > MAX_SOURCE_AGE_SECONDS {
        return false;
    }
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fresh_ok() {
        let now = 1_700_000_000;
        assert!(source_block_time_ok(Some(now - 10), Some(now)));
    }

    #[test]
    fn missing_rejected() {
        assert!(!source_block_time_ok(None, Some(1)));
        assert!(!source_block_time_ok(Some(0), Some(1)));
    }

    #[test]
    fn stale_rejected() {
        let now = 1_700_000_000;
        assert!(!source_block_time_ok(
            Some(now - MAX_SOURCE_AGE_SECONDS - 1),
            Some(now)
        ));
    }

    #[test]
    fn future_rejected() {
        let now = 1_700_000_000;
        assert!(!source_block_time_ok(
            Some(now + CLOCK_SKEW_SECONDS + 10),
            Some(now)
        ));
    }
}
