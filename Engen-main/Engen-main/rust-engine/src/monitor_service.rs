//! Opt-in background monitoring lifecycle for public wallets only.

use std::collections::BTreeMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc;
use std::sync::{Arc, Condvar, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use chrono::{DateTime, Utc};
use serde_json::{json, Value};
use thiserror::Error;

use crate::paper_simulator::PaperCommitLease;
use crate::signature_store::{SignatureStore, SignatureStoreError};
use crate::solana_validation::is_valid_solana_address;
use uuid::Uuid;

const MONITOR_LEASE_SECONDS: u64 = 90;
const MONITOR_LEASE_RENEW_SECONDS: u64 = 30;

#[derive(Debug, Error)]
pub enum MonitorServiceError {
    #[error("Monitoring interval must be at least 30 seconds.")]
    InvalidInterval,
    #[error("Wallet addresses must be supplied as a list of valid Solana public addresses.")]
    InvalidWallet,
    #[error("A non-empty profile lease scope is required.")]
    InvalidLeaseScope,
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
            if stale && state != "error" && state != "busy" {
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

type ScanFunction =
    dyn Fn(&str, Option<&PaperCommitLease>) -> Result<Vec<Value>, String> + Send + Sync + 'static;

/// A durable per-profile/wallet lease held for the full duration of one read-only scan.
struct MonitorLeaseGuard {
    store: Arc<SignatureStore>,
    scope: String,
    owner: String,
    stop_heartbeat: mpsc::Sender<()>,
    heartbeat: Option<JoinHandle<()>>,
    lost: Arc<AtomicBool>,
}

impl MonitorLeaseGuard {
    fn acquire(
        store: Arc<SignatureStore>,
        scope: String,
    ) -> Result<Option<Self>, SignatureStoreError> {
        let owner = format!("{}:{}", std::process::id(), Uuid::new_v4());
        let now = unix_epoch_seconds()?;
        if !store.claim_monitor_lease(&scope, &owner, now, MONITOR_LEASE_SECONDS)? {
            return Ok(None);
        }

        let lost = Arc::new(AtomicBool::new(false));
        let heartbeat_lost = Arc::clone(&lost);
        let heartbeat_store = Arc::clone(&store);
        let heartbeat_scope = scope.clone();
        let heartbeat_owner = owner.clone();
        let (stop_heartbeat, stop_rx) = mpsc::channel::<()>();
        let heartbeat = match thread::Builder::new()
            .name("engen-monitor-lease-renewal".to_string())
            .spawn(move || loop {
                match stop_rx.recv_timeout(Duration::from_secs(MONITOR_LEASE_RENEW_SECONDS)) {
                    Ok(()) | Err(mpsc::RecvTimeoutError::Disconnected) => break,
                    Err(mpsc::RecvTimeoutError::Timeout) => {
                        let renewed = unix_epoch_seconds().and_then(|now| {
                            heartbeat_store.renew_monitor_lease(
                                &heartbeat_scope,
                                &heartbeat_owner,
                                now,
                                MONITOR_LEASE_SECONDS,
                            )
                        });
                        if !matches!(renewed, Ok(true)) {
                            heartbeat_lost.store(true, Ordering::Release);
                            break;
                        }
                    }
                }
            }) {
            Ok(handle) => handle,
            Err(_) => {
                let _ = store.release_monitor_lease(&scope, &owner);
                return Err(SignatureStoreError::InvalidInput(
                    "Unable to start monitor lease renewal.".to_string(),
                ));
            }
        };

        Ok(Some(Self {
            store,
            scope,
            owner,
            stop_heartbeat,
            heartbeat: Some(heartbeat),
            lost,
        }))
    }

    /// Renew at scan completion so an expired/reclaimed lease cannot publish stale results
    /// merely because the periodic heartbeat has not run again yet.
    fn renew_and_validate(&self) -> bool {
        if self.lost.load(Ordering::Acquire) {
            return false;
        }
        let renewed = unix_epoch_seconds().and_then(|now| {
            self.store
                .renew_monitor_lease(&self.scope, &self.owner, now, MONITOR_LEASE_SECONDS)
        });
        if matches!(renewed, Ok(true)) {
            true
        } else {
            self.lost.store(true, Ordering::Release);
            false
        }
    }
}

impl Drop for MonitorLeaseGuard {
    fn drop(&mut self) {
        let _ = self.stop_heartbeat.send(());
        if let Some(heartbeat) = self.heartbeat.take() {
            let _ = heartbeat.join();
        }
        // Owner matching prevents an expired worker from releasing a successor's lease.
        let _ = self.store.release_monitor_lease(&self.scope, &self.owner);
    }
}

fn unix_epoch_seconds() -> Result<i64, SignatureStoreError> {
    let elapsed = SystemTime::now().duration_since(UNIX_EPOCH).map_err(|_| {
        SignatureStoreError::InvalidInput("System clock is before Unix epoch.".to_string())
    })?;
    i64::try_from(elapsed.as_secs()).map_err(|_| {
        SignatureStoreError::InvalidInput(
            "System clock is outside the supported range.".to_string(),
        )
    })
}

pub struct MonitorService {
    interval: Duration,
    scan: Arc<ScanFunction>,
    lease_store: Option<Arc<SignatureStore>>,
    lease_scope_prefix: Option<String>,
    state: Arc<Mutex<SharedState>>,
    stop_signal: Arc<(Mutex<bool>, Condvar)>,
    worker: Mutex<Option<JoinHandle<()>>>,
}

impl MonitorService {
    /// Construct a single-process monitor service without cross-process coordination.
    ///
    /// Use new_with_lease_store when multiple processes can monitor the same profile.
    pub fn new<F>(interval_seconds: u64, scan: F) -> Result<Self, MonitorServiceError>
    where
        F: Fn(&str) -> Result<Vec<Value>, String> + Send + Sync + 'static,
    {
        let scan = move |wallet: &str, _lease: Option<&PaperCommitLease>| scan(wallet);
        Self::build(interval_seconds, None, None, scan)
    }

    /// Construct a monitor service coordinated through the shared SignatureStore database.
    ///
    /// Every process monitoring the same profile must use the same store and scope prefix.
    /// The callback is for read-only wallet scanning; paper-order queue/ledger commits require
    /// their own ownership and freshness checks at the commit boundary.
    pub fn new_with_lease_store<F>(
        interval_seconds: u64,
        scope_prefix: impl Into<String>,
        store: Arc<SignatureStore>,
        scan: F,
    ) -> Result<Self, MonitorServiceError>
    where
        F: Fn(&str) -> Result<Vec<Value>, String> + Send + Sync + 'static,
    {
        let scope_prefix = scope_prefix.into();
        if scope_prefix.trim().is_empty()
            || scope_prefix.len() > 256
            || scope_prefix.trim() != scope_prefix.as_str()
        {
            return Err(MonitorServiceError::InvalidLeaseScope);
        }
        let scan = move |wallet: &str, _lease: Option<&PaperCommitLease>| scan(wallet);
        Self::build(interval_seconds, Some(store), Some(scope_prefix), scan)
    }

    /// Construct a monitor service whose callback receives the active profile/wallet paper lease.
    ///
    /// Use this constructor when the callback queues or commits persistent automatic-copy
    /// paper orders. The same SQLite file must back SignatureStore and PaperSimulator.
    pub fn new_with_lease_store_and_commit_context<F>(
        interval_seconds: u64,
        scope_prefix: impl Into<String>,
        store: Arc<SignatureStore>,
        scan: F,
    ) -> Result<Self, MonitorServiceError>
    where
        F: Fn(&str, &PaperCommitLease) -> Result<Vec<Value>, String> + Send + Sync + 'static,
    {
        let scope_prefix = scope_prefix.into();
        if scope_prefix.trim().is_empty()
            || scope_prefix.len() > 256
            || scope_prefix.trim() != scope_prefix.as_str()
        {
            return Err(MonitorServiceError::InvalidLeaseScope);
        }
        let scan = move |wallet: &str, lease: Option<&PaperCommitLease>| match lease {
            Some(lease) => scan(wallet, lease),
            None => Err("Monitor lease context is unavailable.".to_string()),
        };
        Self::build(interval_seconds, Some(store), Some(scope_prefix), scan)
    }
    fn build<F>(
        interval_seconds: u64,
        lease_store: Option<Arc<SignatureStore>>,
        lease_scope_prefix: Option<String>,
        scan: F,
    ) -> Result<Self, MonitorServiceError>
    where
        F: Fn(&str, Option<&PaperCommitLease>) -> Result<Vec<Value>, String> + Send + Sync + 'static,
    {
        if interval_seconds < 30 {
            return Err(MonitorServiceError::InvalidInterval);
        }
        Ok(Self {
            interval: Duration::from_secs(interval_seconds),
            scan: Arc::new(scan),
            lease_store,
            lease_scope_prefix,
            state: Arc::new(Mutex::new(SharedState::default())),
            stop_signal: Arc::new((Mutex::new(false), Condvar::new())),
            worker: Mutex::new(None),
        })
    }

    fn status_value(&self) -> Result<Value, MonitorServiceError> {
        let state = self
            .state
            .lock()
            .map_err(|_| MonitorServiceError::StateUnavailable)?;
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

        let mut worker = self
            .worker
            .lock()
            .map_err(|_| MonitorServiceError::StateUnavailable)?;
        if worker.as_ref().is_some_and(|handle| !handle.is_finished()) {
            let mut state = self
                .state
                .lock()
                .map_err(|_| MonitorServiceError::StateUnavailable)?;
            let next_wallets = cleaned
                .into_iter()
                .map(|address| {
                    let existing = state
                        .wallets
                        .get(&address)
                        .cloned()
                        .unwrap_or_else(WalletState::waiting);
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
            let mut state = self
                .state
                .lock()
                .map_err(|_| MonitorServiceError::StateUnavailable)?;
            state.wallets = cleaned
                .into_iter()
                .map(|address| (address, WalletState::waiting()))
                .collect();
            state.enabled = true;
        }
        {
            let (stop, _) = &*self.stop_signal;
            *stop
                .lock()
                .map_err(|_| MonitorServiceError::StateUnavailable)? = false;
        }

        let interval = self.interval;
        let scan = Arc::clone(&self.scan);
        let lease_store = self.lease_store.clone();
        let lease_scope_prefix = self.lease_scope_prefix.clone();
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
                        let now = Utc::now().to_rfc3339();
                        let lease = match (&lease_store, &lease_scope_prefix) {
                            (Some(store), Some(prefix)) => {
                                let scope = format!("{prefix}:wallet:{address}");
                                match MonitorLeaseGuard::acquire(Arc::clone(store), scope) {
                                    Ok(Some(guard)) => Some(guard),
                                    Ok(None) => {
                                        if let Ok(mut snapshot) = state.lock() {
                                            if let Some(wallet) = snapshot.wallets.get_mut(&address) {
                                                wallet.state = "busy".to_string();
                                                wallet.last_scan = Some(now.clone());
                                                wallet.last_error = Some(
                                                    "Another process currently owns this wallet monitor lease."
                                                        .to_string(),
                                                );
                                            }
                                        } else {
                                            break;
                                        }
                                        continue;
                                    }
                                    Err(_) => {
                                        if let Ok(mut snapshot) = state.lock() {
                                            if let Some(wallet) = snapshot.wallets.get_mut(&address) {
                                                wallet.state = "error".to_string();
                                                wallet.last_scan = Some(now.clone());
                                                wallet.last_error = Some(
                                                    "Monitor lease unavailable; scan was not started."
                                                        .to_string(),
                                                );
                                            }
                                        } else {
                                            break;
                                        }
                                        continue;
                                    }
                                }
                            }
                            _ => None,
                        };

                        if stop_signal.0.lock().map(|value| *value).unwrap_or(true) {
                            drop(lease);
                            break;
                        }

                        if let Ok(mut snapshot) = state.lock() {
                            if let Some(wallet) = snapshot.wallets.get_mut(&address) {
                                wallet.state = "scanning".to_string();
                            }
                        } else {
                            drop(lease);
                            break;
                        }

                        let mut scan_result = if let Some(guard) = lease.as_ref() {
                            match PaperCommitLease::new(&guard.store, &guard.scope, &guard.owner) {
                                Ok(commit_lease) => scan(&address, Some(&commit_lease)),
                                Err(_) => Err("Monitor lease context could not be created.".to_string()),
                            }
                        } else {
                            scan(&address, None)
                        };
                        if lease
                            .as_ref()
                            .is_some_and(|guard| !guard.renew_and_validate())
                        {
                            scan_result = Err("Monitor lease ownership was lost.".to_string());
                        }
                        match scan_result {
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
                                            "Monitoring scan failed or monitor lease was lost; sensitive details are hidden."
                                                .to_string(),
                                        );
                                    }
                                }
                            }
                        }
                        drop(lease);
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
            *stop
                .lock()
                .map_err(|_| MonitorServiceError::StateUnavailable)? = true;
            wake.notify_all();
        }
        {
            let mut state = self
                .state
                .lock()
                .map_err(|_| MonitorServiceError::StateUnavailable)?;
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
        let mut worker = self
            .worker
            .lock()
            .map_err(|_| MonitorServiceError::StateUnavailable)?;
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
    fn concurrent_start_requests_share_one_worker_and_never_overlap_scans() {
        use std::sync::Barrier;

        let active = Arc::new(AtomicUsize::new(0));
        let maximum_active = Arc::new(AtomicUsize::new(0));
        let scan_active = Arc::clone(&active);
        let scan_maximum = Arc::clone(&maximum_active);
        let service = Arc::new(
            MonitorService::new(30, move |_wallet| {
                let current = scan_active.fetch_add(1, Ordering::SeqCst) + 1;
                scan_maximum.fetch_max(current, Ordering::SeqCst);
                thread::sleep(Duration::from_millis(25));
                scan_active.fetch_sub(1, Ordering::SeqCst);
                Ok(Vec::new())
            })
            .unwrap(),
        );

        let barrier = Arc::new(Barrier::new(5));
        let mut starters = Vec::new();
        for _ in 0..4 {
            let service = Arc::clone(&service);
            let barrier = Arc::clone(&barrier);
            starters.push(thread::spawn(move || {
                barrier.wait();
                service.start(&[WALLET.to_string()]).unwrap()
            }));
        }
        barrier.wait();

        for starter in starters {
            assert_eq!(starter.join().unwrap()["enabled"], true);
        }

        let deadline = Instant::now() + Duration::from_secs(2);
        while active.load(Ordering::SeqCst) == 0 && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(2));
        }
        assert!(maximum_active.load(Ordering::SeqCst) >= 1);
        let stopped = service.stop_and_wait(Duration::from_secs(2)).unwrap();
        assert_eq!(stopped["worker_stopped"], true);
        assert_eq!(maximum_active.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn durable_lease_prevents_overlapping_scans_across_service_instances() {
        let directory = tempfile::tempdir().unwrap();
        let store = Arc::new(SignatureStore::open(directory.path().join("monitor.db")).unwrap());
        let first_started = Arc::new(AtomicBool::new(false));
        let release_first = Arc::new((Mutex::new(false), Condvar::new()));
        let first_scans = Arc::new(AtomicUsize::new(0));
        let first_started_in_scan = Arc::clone(&first_started);
        let release_first_in_scan = Arc::clone(&release_first);
        let first_scans_in_scan = Arc::clone(&first_scans);

        let first = MonitorService::new_with_lease_store(
            30,
            "profile:test",
            Arc::clone(&store),
            move |_wallet| {
                first_scans_in_scan.fetch_add(1, Ordering::SeqCst);
                first_started_in_scan.store(true, Ordering::Release);
                // Keep the first service inside its scan while the second service attempts a claim.
                let (lock, wake) = &*release_first_in_scan;
                let mut released = lock.lock().unwrap();
                let deadline = Instant::now() + Duration::from_secs(5);
                while !*released && Instant::now() < deadline {
                    let (next, _) = wake
                        .wait_timeout(released, Duration::from_millis(20))
                        .unwrap();
                    released = next;
                }
                Ok(Vec::new())
            },
        )
        .unwrap();

        let second_scans = Arc::new(AtomicUsize::new(0));
        let second_scans_in_scan = Arc::clone(&second_scans);
        let second = MonitorService::new_with_lease_store(
            30,
            "profile:test",
            Arc::clone(&store),
            move |_wallet| {
                second_scans_in_scan.fetch_add(1, Ordering::SeqCst);
                Ok(Vec::new())
            },
        )
        .unwrap();

        first.start(&[WALLET.to_string()]).unwrap();
        let deadline = Instant::now() + Duration::from_secs(2);
        while !first_started.load(Ordering::Acquire) && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(5));
        }
        assert!(
            first_started.load(Ordering::Acquire),
            "first service did not begin its scan"
        );

        second.start(&[WALLET.to_string()]).unwrap();
        let deadline = Instant::now() + Duration::from_secs(2);
        loop {
            let state = second.status().unwrap()["wallets"][0]["state"]
                .as_str()
                .unwrap()
                .to_string();
            if state == "busy" {
                break;
            }
            assert!(
                Instant::now() < deadline,
                "second service did not report the wallet lease as busy"
            );
            thread::sleep(Duration::from_millis(5));
        }
        assert_eq!(second_scans.load(Ordering::SeqCst), 0);

        let (lock, wake) = &*release_first;
        *lock.lock().unwrap() = true;
        wake.notify_all();
        first.stop_and_wait(Duration::from_secs(2)).unwrap();
        second.stop_and_wait(Duration::from_secs(2)).unwrap();
        assert_eq!(first_scans.load(Ordering::SeqCst), 1);

        let scope = format!("profile:test:wallet:{WALLET}");
        let now = unix_epoch_seconds().unwrap();
        assert!(store
            .claim_monitor_lease(&scope, "release-verification", now, MONITOR_LEASE_SECONDS)
            .unwrap());
        assert!(store
            .release_monitor_lease(&scope, "release-verification")
            .unwrap());
    }

    #[test]
    fn lease_reclaimed_during_scan_discards_results_and_preserves_successor() {
        let directory = tempfile::tempdir().unwrap();
        let store = Arc::new(SignatureStore::open(directory.path().join("monitor.db")).unwrap());
        let scan_started = Arc::new(AtomicBool::new(false));
        let release_scan = Arc::new((Mutex::new(false), Condvar::new()));
        let scan_started_in_callback = Arc::clone(&scan_started);
        let release_scan_in_callback = Arc::clone(&release_scan);

        let service = MonitorService::new_with_lease_store(
            30,
            "profile:lease-reclaim",
            Arc::clone(&store),
            move |_wallet| {
                scan_started_in_callback.store(true, Ordering::Release);
                let (lock, wake) = &*release_scan_in_callback;
                let mut released = lock.lock().unwrap();
                let deadline = Instant::now() + Duration::from_secs(5);
                while !*released && Instant::now() < deadline {
                    let (next, _) = wake
                        .wait_timeout(released, Duration::from_millis(10))
                        .unwrap();
                    released = next;
                }
                Ok(vec![json!({"marker":"must-not-publish"})])
            },
        )
        .unwrap();

        service.start(&[WALLET.to_string()]).unwrap();
        let deadline = Instant::now() + Duration::from_secs(2);
        while !scan_started.load(Ordering::Acquire) && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(5));
        }
        assert!(
            scan_started.load(Ordering::Acquire),
            "monitor scan did not start"
        );

        let scope = format!("profile:lease-reclaim:wallet:{WALLET}");
        let connection = rusqlite::Connection::open(store.path()).unwrap();
        assert_eq!(
            connection
                .execute(
                    "UPDATE monitor_leases SET expires_at=0 WHERE scope=?1",
                    [scope.as_str()],
                )
                .unwrap(),
            1
        );
        let now = unix_epoch_seconds().unwrap();
        assert!(store
            .claim_monitor_lease(&scope, "successor-owner", now, MONITOR_LEASE_SECONDS)
            .unwrap());

        let (lock, wake) = &*release_scan;
        *lock.lock().unwrap() = true;
        wake.notify_all();

        let deadline = Instant::now() + Duration::from_secs(2);
        let wallet_state = loop {
            let wallet = service.status().unwrap()["wallets"][0].clone();
            if wallet["state"] == "error" {
                break wallet;
            }
            assert!(
                Instant::now() < deadline,
                "scan result was not rejected after lease ownership changed"
            );
            thread::sleep(Duration::from_millis(5));
        };
        assert_eq!(wallet_state["latest_results"], json!([]));
        assert!(wallet_state["last_successful_check"].is_null());

        service.stop_and_wait(Duration::from_secs(2)).unwrap();
        let successor = connection
            .query_row(
                "SELECT owner FROM monitor_leases WHERE scope=?1",
                [scope.as_str()],
                |row| row.get::<_, String>(0),
            )
            .unwrap();
        assert_eq!(successor, "successor-owner");
    }

    #[test]
    fn durable_lease_store_failure_prevents_wallet_scan() {
        let directory = tempfile::tempdir().unwrap();
        let store = Arc::new(SignatureStore::open(directory.path().join("monitor.db")).unwrap());
        rusqlite::Connection::open(store.path())
            .unwrap()
            .execute_batch("DROP TABLE monitor_leases;")
            .unwrap();

        let scans = Arc::new(AtomicUsize::new(0));
        let observed = Arc::clone(&scans);
        let service =
            MonitorService::new_with_lease_store(30, "profile:test", store, move |_wallet| {
                observed.fetch_add(1, Ordering::SeqCst);
                Ok(Vec::new())
            })
            .unwrap();

        service.start(&[WALLET.to_string()]).unwrap();
        let deadline = Instant::now() + Duration::from_secs(2);
        loop {
            let state = service.status().unwrap()["wallets"][0]["state"]
                .as_str()
                .unwrap()
                .to_string();
            if state == "error" {
                break;
            }
            assert!(
                Instant::now() < deadline,
                "service did not surface the unavailable lease store"
            );
            thread::sleep(Duration::from_millis(5));
        }
        assert_eq!(scans.load(Ordering::SeqCst), 0);
        service.stop_and_wait(Duration::from_secs(2)).unwrap();
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
        assert_eq!(
            service.status().unwrap()["wallets"]
                .as_array()
                .unwrap()
                .len(),
            1
        );
        service.stop_and_wait(Duration::from_secs(2)).unwrap();
    }    #[test]
    fn commit_context_constructor_passes_exact_profile_wallet_lease() {
        let directory = tempfile::tempdir().unwrap();
        let store = Arc::new(SignatureStore::open(directory.path().join("monitor.db")).unwrap());
        let observed = Arc::new(AtomicBool::new(false));
        let observed_in_scan = Arc::clone(&observed);
        let service = MonitorService::new_with_lease_store_and_commit_context(
            30,
            "profile:commit-context",
            Arc::clone(&store),
            move |wallet, lease| {
                assert_eq!(
                    lease.scope(),
                    format!("profile:commit-context:wallet:{wallet}")
                );
                observed_in_scan.store(true, Ordering::Release);
                Ok(Vec::new())
            },
        )
        .unwrap();

        service.start(&[WALLET.to_string()]).unwrap();
        let deadline = Instant::now() + Duration::from_secs(2);
        while !observed.load(Ordering::Acquire) && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(5));
        }
        assert!(
            observed.load(Ordering::Acquire),
            "scan callback did not receive its active paper-commit lease"
        );
        service.stop_and_wait(Duration::from_secs(2)).unwrap();
    }

}
