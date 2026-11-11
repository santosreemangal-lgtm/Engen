//! Engen paper runtime (Rust). No private keys, no signing, no broadcast.
//!
//! Python dashboard modules are not deleted in this repo yet. This crate is the
//! production paper executable: config, log, sqlite ledger, profiles, signature
//! store, classifier, monitor, worker, and HTTP API.

pub mod classifier;
pub mod config;
pub mod error;
pub mod logutil;
pub mod monitor_service;
pub mod paper_copy;
pub mod paper_http;
pub mod paper_simulator;
pub mod profile_store;
pub mod signature_store;
pub mod source_freshness;
pub mod transaction_monitor;
pub mod worker;

pub use error::{EngenError, Result};
pub use paper_simulator::PaperSimulator;
