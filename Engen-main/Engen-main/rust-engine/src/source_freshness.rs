//! Source-transaction freshness gate for paper-copy eligibility.

pub const MAX_SOURCE_AGE_SECONDS: i64 = 60;
pub const CLOCK_SKEW_SECONDS: i64 = 5;

/// A source transaction is eligible only with a positive, present block timestamp.
///
/// now_ts is injectable so boundary behavior is deterministic in tests.
pub fn source_block_time_ok(block_time: Option<i64>, now_ts: i64) -> bool {
    let Some(block_time) = block_time else {
        return false;
    };

    if block_time <= 0 {
        return false;
    }
    if block_time > now_ts.saturating_add(CLOCK_SKEW_SECONDS) {
        return false;
    }
    now_ts.saturating_sub(block_time) <= MAX_SOURCE_AGE_SECONDS
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_missing_or_non_positive_timestamp() {
        assert!(!source_block_time_ok(None, 1_700_000_000));
        assert!(!source_block_time_ok(Some(0), 1_700_000_000));
        assert!(!source_block_time_ok(Some(-1), 1_700_000_000));
    }

    #[test]
    fn accepts_exactly_sixty_seconds_old() {
        assert!(source_block_time_ok(Some(1_699_999_940), 1_700_000_000));
    }

    #[test]
    fn rejects_older_than_sixty_seconds() {
        assert!(!source_block_time_ok(Some(1_699_999_939), 1_700_000_000));
    }

    #[test]
    fn allows_five_seconds_of_future_skew_only() {
        assert!(source_block_time_ok(Some(1_700_000_005), 1_700_000_000));
        assert!(!source_block_time_ok(Some(1_700_000_006), 1_700_000_000));
    }
}
