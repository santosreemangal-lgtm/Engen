//! Opt-in background monitoring lifecycle for public wallets only.

use std::collections::BTreeMap;
use std::sync::{Arc, Condvar, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use chrono::{DateTime, Utc};
use serde_json::{json, Value};
use thiserror::Error;

use crate::solana_validation::is_valid_solana_address;

#[derive(Debug, Error)]
pub enum MonitorServiceError {
    #[error("Monitoring interval must be at least 30 seconds.")]
    InvalidInterval,
    #[error("Wallet addresses must be supplied as a list of valid Solana public addresses.")]
    InvalidWallet,
    #[error("Monitoring service state is unavailable.")]
    StateUnavailable,
    #[error("Shutdown timeout must be positive.")]
    InvalidShutdownTimeout,
}

#[derive(Debug, Clone)]
struct WalletState {
    state: String,
    last_scan: Option<String>,
    last_successful_check: Option<String>,
    last_error: Option<String>,
    latest_results: Vec<Value>,
}

impl WalletState {
    fn waiting() -> Self {
        Self {
            state: "waiting".to_string(),
            last_scan: None,
            last_successful_check: None,
            last_error: None,
            latest_results: Vec::new(),
        }
    }

    fn as_json(&self, interval_seconds: u64) -> Value {
        let mut state = self.state.clone();
        if let Some(last_success) = self.last_successful_check.as_deref() {
            let stale = DateTime::parse_from_rfc3339(last_success)
                .map(|timestamp| {
                    Utc::now()
                        .signed_duration_since(timestamp.with_timezone(&Utc))
                        .num_seconds()
                        > (interval_seconds.saturating_mul(2)) as i64
                })
                .unwrap_or(true);
            if stale && state != "error" {
                state = "stale".to_string();
            }
        }
        json!({
            "state":state,
            "last_scan":self.last_scan,
            "last_successful_check":self.last_successful_check,
            "last_error":self.last_error,
            "latest_results":self.latest_results
        })
    }
}

#[derive(Default)]
struct SharedState {
    enabled: bool,
    wallets: BTreeMap<String, WalletState>,
}

type ScanFunction = dyn Fn(&str) -> Result<Vec<Value>, String> + Send + Sync + 'static;

pub struct MonitorService {
    interval: Duration,
    scan: Arc<ScanFunction>,
    state: Arc<Mutex<SharedState>>,
    stop_signal: Arc<(Mutex<bool>, Condvar)>,
    worker: Mutex<Option<JoinHandle<()>>>,
}

impl MonitorService {
    pub fn new<F>(interval_seconds: u64, scan: F) -> Result<Self, MonitorServiceError>
    where
        F: Fn(&str) -> Result<Vec<Value>, String> + Send + Sync + 'static,
    {
        if interval_seconds < 30 {
            return Err(MonitorServiceError::InvalidInterval);
        }
        Ok(Self {
            interval: Duration::from_secs(interval_seconds),
            scan: Arc::new(scan),
            state: Arc::new(Mutex::new(SharedState::default())),
            stop_signal: Arc::new((Mutex::new(false), Condvar::new())),
            worker: Mutex::new(None),
        })
    }

    fn status_value(&self) -> Result<Value, MonitorServiceError> {
        let state = self.state.lock().map_err(|_| MonitorServiceError::StateUnavailable)?;
        let wallets: Vec<Value> = state
            .wallets
            .iter()
            .map(|(address, details)| {
                let mut value = details.as_json(self.interval.as_secs());
                value["address"] = Value::String(address.clone());
                value
            })
            .collect();
        Ok(json!({
            "enabled":state.enabled,
            "interval_seconds":self.interval.as_secs(),
            "wallets":wallets
        }))
    }

    pub fn status(&self) -> Result<Value, MonitorServiceError> {
        self.status_value()
    }

    /// Explicitly start or update monitoring. Calling this repeatedly never adds a second worker.
    pub fn start(&self, addresses: &[String]) -> Result<Value, MonitorServiceError> {
        let mut cleaned = Vec::new();
        for address in addresses {
            let address = address.trim();
            if !is_valid_solana_address(address) {
                return Err(MonitorServiceError::InvalidWallet);
            }
            if !cleaned.iter().any(|existing: &String| existing == address) {
                cleaned.push(address.to_string());
            }
        }

        let mut worker = self.worker.lock().map_err(|_| MonitorServiceError::StateUnavailable)?;
        if worker.as_ref().is_some_and(|handle| !handle.is_finished()) {
            let mut state = self.state.lock().map_err(|_| MonitorServiceError::StateUnavailable)?;
            let next_wallets = cleaned
                .into_iter()
                .map(|address| {
                    let existing = state.wallets.get(&address).cloned().unwrap_or_else(WalletState::waiting);
                    (address, existing)
                })
                .collect();
            state.wallets = next_wallets;
            state.enabled = true;
            drop(state);
            return self.status_value();
        }

        if let Some(previous) = worker.take() {
            let _ = previous.join();
        }

        {
            let mut state = self.state.lock().map_err(|_| MonitorServiceError::StateUnavailable)?;
            state.wallets = cleaned
                .into_iter()
                .map(|address| (address, WalletState::waiting()))
                .collect();
            state.enabled = true;
        }
        {
            let (stop, _) = &*self.stop_signal;
            *stop.lock().map_err(|_| MonitorServiceError::StateUnavailable)? = false;
        }

        let interval = self.interval;
        let scan = Arc::clone(&self.scan);
        let state = Arc::clone(&self.state);
        let stop_signal = Arc::clone(&self.stop_signal);
        *worker = Some(thread::Builder::new()
            .name("engen-read-only-monitor".to_string())
            .spawn(move || {
                loop {
                    let should_stop = stop_signal.0.lock().map(|value| *value).unwrap_or(true);
                    if should_stop {
                        break;
                    }
                    let addresses: Vec<String> = match state.lock() {
                        Ok(snapshot) => snapshot.wallets.keys().cloned().collect(),
                        Err(_) => break,
                    };

                    for address in addresses {
                        if stop_signal.0.lock().map(|value| *value).unwrap_or(true) {
                            break;
                        }
                        if let Ok(mut snapshot) = state.lock() {
                            if let Some(wallet) = snapshot.wallets.get_mut(&address) {
                                wallet.state = "scanning".to_string();
                            }
                        } else {
                            break;
                        }

                        let now = Utc::now().to_rfc3339();
                        match scan(&address) {
                            Ok(mut results) => {
                                results.truncate(20);
                                if let Ok(mut snapshot) = state.lock() {
                                    if let Some(wallet) = snapshot.wallets.get_mut(&address) {
                                        wallet.state = "connected".to_string();
                                        wallet.last_scan = Some(now.clone());
                                        wallet.last_successful_check = Some(now);
                                        wallet.last_error = None;
                                        wallet.latest_results = results;
                                    }
                                }
                            }
                            Err(_) => {
                                if let Ok(mut snapshot) = state.lock() {
                                    if let Some(wallet) = snapshot.wallets.get_mut(&address) {
                                        wallet.state = "error".to_string();
                                        wallet.last_scan = Some(now);
                                        wallet.last_error = Some(
                                            "Monitoring scan failed; sensitive error details are hidden."
                                                .to_string(),
                                        );
                                    }
                                }
                            }
                        }
                    }

                    let stop = match stop_signal.0.lock() {
                        Ok(value) => value,
                        Err(_) => break,
                    };
                    if *stop {
                        break;
                    }
                    let waited = stop_signal.1.wait_timeout(stop, interval);
                    if waited.map(|(guard, _)| *guard).unwrap_or(true) {
                        break;
                    }
                }

                if let Ok(mut snapshot) = state.lock() {
                    snapshot.enabled = false;
                    if let Ok(stop) = stop_signal.0.lock() {
                        if *stop {
                            for wallet in snapshot.wallets.values_mut() {
                                wallet.state = "stopped".to_string();
                            }
                        }
                    }
                }
            })
            .map_err(|_| MonitorServiceError::StateUnavailable)?);

        self.status_value()
    }

    pub fn stop(&self) -> Result<Value, MonitorServiceError> {
        {
            let (stop, wake) = &*self.stop_signal;
            *stop.lock().map_err(|_| MonitorServiceError::StateUnavailable)? = true;
            wake.notify_all();
        }
        {
            let mut state = self.state.lock().map_err(|_| MonitorServiceError::StateUnavailable)?;
            state.enabled = false;
            for wallet in state.wallets.values_mut() {
                if !matches!(wallet.state.as_str(), "error" | "stale") {
                    wallet.state = "stopping".to_string();
                }
            }
        }
        self.status_value()
    }

    pub fn stop_and_wait(&self, timeout: Duration) -> Result<Value, MonitorServiceError> {
        if timeout.is_zero() {
            return Err(MonitorServiceError::InvalidShutdownTimeout);
        }
        self.stop()?;
        let started = Instant::now();
        let mut worker = self.worker.lock().map_err(|_| MonitorServiceError::StateUnavailable)?;
        let stopped = if let Some(handle) = worker.as_ref() {
            while !handle.is_finished() && started.elapsed() < timeout {
                thread::sleep(Duration::from_millis(10));
            }
            handle.is_finished()
        } else {
            true
        };
        if stopped {
            if let Some(handle) = worker.take() {
                let _ = handle.join();
            }
        }
        let mut status = self.status_value()?;
        status["worker_stopped"] = Value::Bool(stopped);
        Ok(status)
    }
}

impl Drop for MonitorService {
    fn drop(&mut self) {
        let (stop, wake) = &*self.stop_signal;
        if let Ok(mut should_stop) = stop.lock() {
            *should_stop = true;
            wake.notify_all();
        }
        if let Ok(worker) = self.worker.get_mut() {
            if let Some(handle) = worker.take() {
                let _ = handle.join();
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    const WALLET: &str = "11111111111111111111111111111111";

    #[test]
    fn validates_interval_and_wallet_addresses() {
        assert!(matches!(
            MonitorService::new(29, |_| Ok(Vec::new())),
            Err(MonitorServiceError::InvalidInterval)
        ));
        let service = MonitorService::new(30, |_| Ok(Vec::new())).unwrap();
        assert!(matches!(
            service.start(&["not-a-wallet".to_string()]),
            Err(MonitorServiceError::InvalidWallet)
        ));
    }

    #[test]
    fn starts_opt_in_worker_and_stops_it_cleanly() {
        let scans = Arc::new(AtomicUsize::new(0));
        let observed = Arc::clone(&scans);
        let service = MonitorService::new(30, move |_wallet| {
            observed.fetch_add(1, Ordering::SeqCst);
            Ok(vec![json!({"status":"observed"})])
        })
        .unwrap();
        let started = service.start(&[WALLET.to_string()]).unwrap();
        assert_eq!(started["enabled"], true);

        let deadline = Instant::now() + Duration::from_secs(2);
        while scans.load(Ordering::SeqCst) == 0 && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(5));
        }
        assert!(scans.load(Ordering::SeqCst) > 0);
        let stopped = service.stop_and_wait(Duration::from_secs(2)).unwrap();
        assert_eq!(stopped["worker_stopped"], true);
        assert_eq!(stopped["enabled"], false);
    }

    #[test]
    fn updating_wallets_does_not_create_overlapping_workers() {
        let scans = Arc::new(AtomicUsize::new(0));
        let observed = Arc::clone(&scans);
        let service = MonitorService::new(30, move |_wallet| {
            observed.fetch_add(1, Ordering::SeqCst);
            Ok(Vec::new())
        })
        .unwrap();
        service.start(&[WALLET.to_string()]).unwrap();
        service.start(&[WALLET.to_string()]).unwrap();
        assert_eq!(service.status().unwrap()["wallets"].as_array().unwrap().len(), 1);
        service.stop_and_wait(Duration::from_secs(2)).unwrap();
    }
}
