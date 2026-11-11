//! Background paper loop. Read-only RPC signatures, no broadcast.

use crate::logutil;
use crate::monitor_service::MonitorService;
use crate::paper_simulator::PaperSimulator;
use crate::signature_store::SignatureStore;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

pub struct WorkerHandle {
    pub stop: Arc<Mutex<bool>>,
}

pub fn spawn_poller(
    monitor: Arc<Mutex<MonitorService>>,
    sim: Arc<Mutex<PaperSimulator>>,
    signatures: Arc<Mutex<SignatureStore>>,
    poll_seconds: u64,
) -> WorkerHandle {
    let stop = Arc::new(Mutex::new(false));
    let stop_flag = Arc::clone(&stop);
    thread::spawn(move || {
        logutil::info("paper worker started");
        loop {
            if *stop_flag.lock().unwrap_or_else(|e| e.into_inner()) {
                logutil::info("paper worker stopped");
                break;
            }
            poll_once(&monitor, &sim, &signatures);
            thread::sleep(Duration::from_secs(poll_seconds));
        }
    });
    WorkerHandle { stop }
}

fn poll_once(
    monitor: &Arc<Mutex<MonitorService>>,
    sim: &Arc<Mutex<PaperSimulator>>,
    signatures: &Arc<Mutex<SignatureStore>>,
) {
    let fetched = {
        let guard = match monitor.lock() {
            Ok(g) => g,
            Err(e) => e.into_inner(),
        };
        let mut out = Vec::new();
        for m in &guard.monitors {
            if m.rpc_url().is_none() {
                continue;
            }
            match m.fetch_signatures(10) {
                Ok(result) => out.push((m.wallet.clone(), result)),
                Err(err) => logutil::warn(&format!("rpc poll {}: {err}", m.wallet)),
            }
        }
        out
    };
    if let Ok(mut guard) = monitor.lock() {
        for (wallet, result) in &fetched {
            if let Some(m) = guard.monitors.iter_mut().find(|m| &m.wallet == wallet) {
                if let Err(err) = m.record_signature_list(result) {
                    logutil::warn(&format!("signature parse: {err}"));
                    continue;
                }
                if let Ok(store) = signatures.lock() {
                    for obs in m.observations() {
                        let _ = store.remember(&m.wallet, &obs.signature);
                    }
                }
            }
        }
        if let Ok(mut paper) = sim.lock() {
            if let Err(err) = guard.tick(&mut paper) {
                logutil::warn(&format!("paper tick: {err}"));
            }
        }
    }
}
