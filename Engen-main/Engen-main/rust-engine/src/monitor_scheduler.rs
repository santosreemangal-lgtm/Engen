//! Deterministic Fresh-first monitoring scheduler and bounded 429 cooldown policy.

use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

pub const MAX_FRESH_BATCH: usize = 50;
pub const DEFAULT_HISTORY_BATCH: usize = 25;
pub const MIN_HISTORY_BATCH: usize = 10;
pub const MAX_HISTORY_BATCH: usize = 25;
pub const MAX_HISTORY_PER_WALLET: usize = 1_000;
pub const INITIAL_RATE_LIMIT_COOLDOWN: Duration = Duration::from_secs(30);
pub const MAX_RATE_LIMIT_COOLDOWN: Duration = Duration::from_secs(600);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ScanPlan {
    pub fresh_limit: usize,
    pub history_limit: usize,
    pub history_offset: usize,
}

impl ScanPlan {
    pub fn new(fresh_limit: usize, history_limit: usize, history_offset: usize) -> Self {
        Self {
            fresh_limit: fresh_limit.min(MAX_FRESH_BATCH),
            history_limit: history_limit.clamp(MIN_HISTORY_BATCH, MAX_HISTORY_BATCH),
            history_offset: history_offset.min(MAX_HISTORY_PER_WALLET),
        }
    }

    /// Fresh work always runs first; History is eligible only after Fresh is drained.
    pub fn next_stage(self, fresh_pending: usize, history_pending: usize) -> Option<ScanStage> {
        if fresh_pending > 0 {
            return Some(ScanStage::Fresh {
                limit: fresh_pending.min(self.fresh_limit),
            });
        }
        if history_pending > 0 && self.history_offset < MAX_HISTORY_PER_WALLET {
            let remaining = MAX_HISTORY_PER_WALLET - self.history_offset;
            return Some(ScanStage::History {
                limit: history_pending.min(self.history_limit).min(remaining),
                offset: self.history_offset,
            });
        }
        None
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ScanStage {
    Fresh { limit: usize },
    History { limit: usize, offset: usize },
}

/// Atomic process-wide gate: only one monitoring cycle may run at a time.
#[derive(Debug, Default)]
pub struct ScanCycleGate {
    active: AtomicBool,
}

impl ScanCycleGate {
    pub fn try_enter(&self) -> Option<ScanCycleGuard<'_>> {
        self.active
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .ok()
            .map(|_| ScanCycleGuard { gate: self })
    }

    pub fn is_active(&self) -> bool {
        self.active.load(Ordering::Acquire)
    }
}

pub struct ScanCycleGuard<'a> {
    gate: &'a ScanCycleGate,
}

impl Drop for ScanCycleGuard<'_> {
    fn drop(&mut self) {
        self.gate.active.store(false, Ordering::Release);
    }
}

/// Exponential 429 cooldown, honoring Retry-After while retaining a 10-minute cap.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RateLimitBackoff {
    consecutive_429s: u32,
    current: Duration,
}

impl Default for RateLimitBackoff {
    fn default() -> Self {
        Self {
            consecutive_429s: 0,
            current: Duration::ZERO,
        }
    }
}

impl RateLimitBackoff {
    pub fn on_rate_limited(&mut self, retry_after: Option<Duration>) -> Duration {
        self.consecutive_429s = self.consecutive_429s.saturating_add(1);
        let exponential = if self.consecutive_429s == 1 {
            INITIAL_RATE_LIMIT_COOLDOWN
        } else {
            self.current
                .checked_mul(2)
                .unwrap_or(MAX_RATE_LIMIT_COOLDOWN)
        };
        self.current = exponential
            .max(retry_after.unwrap_or(Duration::ZERO))
            .min(MAX_RATE_LIMIT_COOLDOWN);
        self.current
    }

    pub fn on_success(&mut self) {
        self.consecutive_429s = 0;
        self.current = Duration::ZERO;
    }

    pub fn current(&self) -> Duration {
        self.current
    }

    pub fn consecutive_429s(&self) -> u32 {
        self.consecutive_429s
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fresh_queue_is_always_drained_before_history() {
        let plan = ScanPlan::new(100, 100, 0);
        assert_eq!(
            plan.next_stage(55, 100),
            Some(ScanStage::Fresh { limit: 50 })
        );
        assert_eq!(plan.next_stage(5, 100), Some(ScanStage::Fresh { limit: 5 }));
        assert_eq!(
            plan.next_stage(0, 100),
            Some(ScanStage::History {
                limit: 25,
                offset: 0
            })
        );
    }

    #[test]
    fn history_batch_and_wallet_window_are_capped() {
        let plan = ScanPlan::new(50, 1, 990);
        assert_eq!(
            plan.next_stage(0, 100),
            Some(ScanStage::History {
                limit: 10,
                offset: 990
            })
        );
        let exhausted = ScanPlan::new(50, 25, 1_000);
        assert_eq!(exhausted.next_stage(0, 100), None);
    }

    #[test]
    fn rate_limit_backoff_starts_at_thirty_seconds_and_caps_at_ten_minutes() {
        let mut backoff = RateLimitBackoff::default();
        assert_eq!(backoff.on_rate_limited(None), Duration::from_secs(30));
        assert_eq!(backoff.on_rate_limited(None), Duration::from_secs(60));
        assert_eq!(backoff.on_rate_limited(None), Duration::from_secs(120));
        assert_eq!(
            backoff.on_rate_limited(Some(Duration::from_secs(900))),
            Duration::from_secs(600)
        );
        assert_eq!(backoff.on_rate_limited(None), Duration::from_secs(600));
        backoff.on_success();
        assert_eq!(backoff.current(), Duration::ZERO);
        assert_eq!(backoff.consecutive_429s(), 0);
    }

    #[test]
    fn retry_after_is_honored_but_never_exceeds_cap() {
        let mut backoff = RateLimitBackoff::default();
        assert_eq!(
            backoff.on_rate_limited(Some(Duration::from_secs(90))),
            Duration::from_secs(90)
        );
    }

    #[test]
    fn only_one_scan_cycle_can_be_active() {
        let gate = ScanCycleGate::default();
        let first = gate.try_enter().unwrap();
        assert!(gate.is_active());
        assert!(gate.try_enter().is_none());
        drop(first);
        assert!(!gate.is_active());
        assert!(gate.try_enter().is_some());
    }
}
