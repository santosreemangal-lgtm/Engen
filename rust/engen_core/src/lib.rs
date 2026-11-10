//! Engen paper copy core (Rust).
//!
//! Maps to the Python modules over time:
//! - `paper_simulator` ← engine/paper_simulator.py
//! - `paper_copy` ← engine/paper_copy_runner.py
//! - `transaction_monitor` ← engine/transaction_monitor.py
//! - `monitor_bridge` / `monitor_service` ← dashboard bridge/service
//! - `profile_store` ← engine/profile_store.py
//!
//! Safety: no private keys, no signing, no broadcast.

pub mod error;
pub mod paper_simulator;
pub mod paper_copy;
pub mod paper_http;
pub mod source_freshness;
pub mod transaction_monitor;
pub mod monitor_service;
pub mod profile_store;

pub use error::{EngenError, Result};
pub use paper_simulator::PaperSimulator;
