//! Source-transaction freshness gate for paper-copy eligibility.

use std::env;

pub const MAX_SOURCE_AGE_SECONDS: i64 = 60;
pub const CLOCK_SKEW_SECONDS: i64 = 5;

/// A source transaction is eligible only with a positive, present block timestamp.
///
/// now_ts is injectable so boundary behavior is deterministic in tests.
fn configured_max_source_age_seconds(value: Option<&str>) -> Option<i64> {
    match value {
        None => Some(MAX_SOURCE_AGE_SECONDS),
        Some(raw) => raw.trim().parse::<i64>().ok().filter(|seconds| *seconds > 0),
    }
}

/// Read the accepted runtime override; malformed/zero/negative values fail closed.
pub fn max_source_age_seconds_from_env() -> Option<i64> {
    let configured = env::var("ENGEN_MAX_SOURCE_AGE_SECONDS").ok();
    configured_max_source_age_seconds(configured.as_deref())
}

pub fn source_block_time_ok(block_time: Option<i64>, now_ts: i64) -> bool {
    let Some(block_time) = block_time else {
        return false;
    };
    let Some(max_age) = max_source_age_seconds_from_env() else {
        return false;
    };

    if block_time <= 0 {
        return false;
    }
    if block_time > now_ts.saturating_add(CLOCK_SKEW_SECONDS) {
        return false;
    }
    now_ts.saturating_sub(block_time) <= max_age
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn configured_max_source_age_defaults_and_validates() {
        assert_eq!(configured_max_source_age_seconds(None), Some(60));
        assert_eq!(configured_max_source_age_seconds(Some("15")), Some(15));
        assert_eq!(configured_max_source_age_seconds(Some(" 25 ")), Some(25));
        for value in ["", "0", "-1", "not-an-integer"] {
            assert_eq!(configured_max_source_age_seconds(Some(value)), None);
        }
    }

    #[test]
    fn configured_source_age_override_changes_freshness_window() {
        let now = 1_700_000_000;
        let max_age = configured_max_source_age_seconds(Some("15")).unwrap();
        assert!(now - (now - 15) <= max_age);
        assert!(!(now - (now - 16) <= max_age));
    }

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
