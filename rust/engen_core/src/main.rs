//! Unified paper runtime. Binds HTTP, optional read-only signature poller.
//! Live trading is refused.

use engen_core::config::Config;
use engen_core::logutil;
use engen_core::monitor_service::MonitorService;
use engen_core::paper_http::{serve, PaperApiState};
use engen_core::paper_simulator::PaperSimulator;
use engen_core::signature_store::SignatureStore;
use engen_core::worker;
use std::sync::{Arc, Mutex};

fn main() {
    let config = match Config::from_env() {
        Ok(config) => config,
        Err(err) => {
            eprintln!("config error: {err}");
            std::process::exit(1);
        }
    };
    logutil::info("engen_paper starting");
    logutil::info("real_trading=false transaction_broadcast=false");

    let sim = match PaperSimulator::with_sqlite(&config.paper_db) {
        Ok(sim) => sim,
        Err(err) => {
            eprintln!("paper db: {err}");
            std::process::exit(1);
        }
    };
    let sig_path = config
        .paper_db
        .parent()
        .unwrap_or(std::path::Path::new("data"))
        .join("signatures.db");
    let signatures = match SignatureStore::open(&sig_path) {
        Ok(store) => store,
        Err(err) => {
            eprintln!("signature store: {err}");
            std::process::exit(1);
        }
    };

    let mut monitor = MonitorService::new();
    for wallet in &config.wallets {
        if let Some(rpc) = &config.rpc_url {
            monitor.add_wallet_rpc(wallet, rpc);
        } else {
            monitor.add_wallet(wallet);
        }
    }

    let state = PaperApiState {
        sim: Arc::new(Mutex::new(sim)),
        monitor: Arc::new(Mutex::new(monitor)),
    };
    let _worker = worker::spawn_poller(
        Arc::clone(&state.monitor),
        Arc::clone(&state.sim),
        Arc::new(Mutex::new(signatures)),
        config.poll_seconds,
    );

    logutil::info(&format!("listening on http://{}", config.bind));
    if let Err(err) = serve(&config.bind, state) {
        eprintln!("http server: {err}");
        std::process::exit(1);
    }
}
