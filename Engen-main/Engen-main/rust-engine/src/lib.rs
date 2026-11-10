//! Rust migration of Engen's read-only monitoring and paper-simulation core.
//!
//! This crate deliberately exposes no transaction-signing or transaction-broadcast
//! implementation. Paper fills are virtual ledger events only.

pub mod config;
pub mod latency;
pub mod market_data;
pub mod monitor_scheduler;
pub mod monitor_service;
pub mod pnl;
pub mod paper_simulator;
pub mod paper_copy_runner;
pub mod profile_store;
pub mod rpc_policy;
pub mod safety;
pub mod signature_store;
pub mod solana_public;
pub mod solana_validation;
pub mod source_freshness;
pub mod transaction_analysis;
pub mod transaction_monitor;
pub mod transaction_processor;
pub mod wallet_monitor;

pub use config::{Config, ConfigError, LogLevel};
pub use safety::{block_live_execution, LiveExecutionDisabled};
