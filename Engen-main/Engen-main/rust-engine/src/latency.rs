//! Bounded, local-stage latency recorder. No end-to-end latency is inferred.

use std::collections::BTreeMap;
use std::sync::Mutex;
use std::time::Instant;

#[derive(Debug, Clone, PartialEq)]
pub struct LatencySummary {
    pub count: usize,
    pub mean_ms: f64,
    pub max_ms: f64,
    pub p50_ms: f64,
    pub p95_ms: f64,
    pub p99_ms: f64,
}

#[derive(Debug)]
pub struct LatencyRecorder {
    max_samples: usize,
    samples: Mutex<BTreeMap<String, Vec<f64>>>,
}

impl Default for LatencyRecorder {
    fn default() -> Self {
        Self::new(10_000).expect("the default sample bound is valid")
    }
}

impl LatencyRecorder {
    pub fn new(max_samples: usize) -> Result<Self, &'static str> {
        if max_samples == 0 {
            return Err("max_samples must be a positive integer.");
        }
        Ok(Self {
            max_samples,
            samples: Mutex::new(BTreeMap::new()),
        })
    }

    pub fn record(&self, stage: &str, elapsed_ms: f64) -> Result<(), &'static str> {
        if stage.trim().is_empty() {
            return Err("A non-empty latency stage name is required.");
        }
        if !elapsed_ms.is_finite() || elapsed_ms < 0.0 {
            return Err("elapsed_ms must be a finite, non-negative number.");
        }

        let mut guard = self
            .samples
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let stage_samples = guard
            .entry(stage.to_string())
            .or_insert_with(|| Vec::with_capacity(self.max_samples));

        // Match the legacy recorder: retain the first bounded sample window.
        if stage_samples.len() < self.max_samples {
            stage_samples.push(elapsed_ms);
        }
        Ok(())
    }

    /// Measure a synchronous local stage and record its elapsed milliseconds.
    pub fn measure<T, F>(&self, stage: &str, operation: F) -> Result<T, &'static str>
    where
        F: FnOnce() -> T,
    {
        if stage.trim().is_empty() {
            return Err("A non-empty latency stage name is required.");
        }
        let started = Instant::now();
        let result = operation();
        self.record(stage, started.elapsed().as_secs_f64() * 1_000.0)?;
        Ok(result)
    }

    pub fn summary(&self) -> BTreeMap<String, LatencySummary> {
        let snapshot = self
            .samples
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .clone();

        snapshot
            .into_iter()
            .filter_map(|(stage, samples)| {
                if samples.is_empty() {
                    return None;
                }

                let mut values = samples.into_iter().collect::<Vec<_>>();
                values.sort_by(f64::total_cmp);
                let count = values.len();
                let mean_ms = values.iter().sum::<f64>() / count as f64;

                Some((
                    stage,
                    LatencySummary {
                        count,
                        mean_ms: round3(mean_ms),
                        max_ms: round3(*values.last().unwrap_or(&0.0)),
                        p50_ms: round3(percentile(&values, 0.50)),
                        p95_ms: round3(percentile(&values, 0.95)),
                        p99_ms: round3(percentile(&values, 0.99)),
                    },
                ))
            })
            .collect()
    }
}

fn percentile(sorted: &[f64], p: f64) -> f64 {
    let index = ((sorted.len() as f64 * p).ceil() as usize).saturating_sub(1);
    sorted[index.min(sorted.len() - 1)]
}

fn round3(value: f64) -> f64 {
    (value * 1_000.0).round() / 1_000.0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn records_nearest_rank_percentiles() {
        let recorder = LatencyRecorder::new(10).unwrap();
        for sample in [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0] {
            recorder.record("paper_fill", sample).unwrap();
        }
        let summary = recorder.summary().remove("paper_fill").unwrap();
        assert_eq!(summary.count, 10);
        assert_eq!(summary.mean_ms, 5.5);
        assert_eq!(summary.max_ms, 10.0);
        assert_eq!(summary.p50_ms, 5.0);
        assert_eq!(summary.p95_ms, 10.0);
        assert_eq!(summary.p99_ms, 10.0);
    }

    #[test]
    fn retains_first_bounded_samples_like_python_reference() {
        let recorder = LatencyRecorder::new(2).unwrap();
        recorder.record("scan", 1.0).unwrap();
        recorder.record("scan", 2.0).unwrap();
        recorder.record("scan", 3.0).unwrap();
        let summary = recorder.summary().remove("scan").unwrap();
        assert_eq!(summary.count, 2);
        assert_eq!(summary.mean_ms, 1.5);
        assert_eq!(summary.max_ms, 2.0);
    }

    #[test]
    fn measures_local_operation_and_records_its_stage() {
        let recorder = LatencyRecorder::new(4).unwrap();
        let output = recorder.measure("decode", || 42).unwrap();
        assert_eq!(output, 42);
        let summary = recorder.summary().remove("decode").unwrap();
        assert_eq!(summary.count, 1);
        assert!(summary.mean_ms >= 0.0);
    }

    #[test]
    fn rejects_invalid_measurements() {
        let recorder = LatencyRecorder::new(5).unwrap();
        assert!(recorder.record("", 1.0).is_err());
        assert!(recorder.record("scan", -1.0).is_err());
        assert!(recorder.record("scan", f64::NAN).is_err());
    }
}
