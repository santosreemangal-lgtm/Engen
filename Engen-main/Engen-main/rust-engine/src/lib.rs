//! Rust implementation of Engen's read-only monitoring and paper-only engine.
//!
//! This crate intentionally exposes no transaction signing, wallet-secret,
//! transaction-broadcast, or live-trading interfaces.

pub mod config;
pub mod pnl;
pub mod market_data;
pub mod solana_public;
pub mod rpc_policy;

pub const ENGINE_MODE: &str = "read_only";
pub const LIVE_TRADING_AVAILABLE: bool = false;
