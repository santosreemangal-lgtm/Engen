//! Process configuration. Paper mode only — live flags are rejected.

use crate::{EngenError, Result};
use std::env;
use std::path::PathBuf;

#[derive(Debug, Clone)]
pub struct Config {
    pub bind: String,
    pub paper_db: PathBuf,
    pub profile_db: PathBuf,
    pub rpc_url: Option<String>,
    pub wallets: Vec<String>,
    pub poll_seconds: u64,
}

impl Config {
    pub fn from_env() -> Result<Self> {
        if env::var("ENGEN_LIVE_ENABLED").ok().as_deref() == Some("true") {
            return Err(EngenError::LiveDisabled);
        }
        let bind = env::var("ENGEN_BIND").unwrap_or_else(|_| "127.0.0.1:8787".into());
        let paper_db = env::var("ENGEN_PAPER_DB")
            .map(PathBuf::from)
            .unwrap_or_else(|_| PathBuf::from("data/paper_simulator.db"));
        let profile_db = env::var("ENGEN_PROFILE_DB")
            .map(PathBuf::from)
            .unwrap_or_else(|_| PathBuf::from("data/profiles.db"));
        let rpc_url = env::var("SOLANA_RPC_URL").ok().filter(|s| !s.trim().is_empty());
        let wallets = env::var("ENGEN_WALLETS")
            .unwrap_or_default()
            .split(',')
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(|s| s.to_string())
            .collect();
        let poll_seconds = env::var("ENGEN_POLL_SECONDS")
            .ok()
            .and_then(|s| s.parse().ok())
            .unwrap_or(15)
            .clamp(5, 300);
        Ok(Self {
            bind,
            paper_db,
            profile_db,
            rpc_url,
            wallets,
            poll_seconds,
        })
    }
}
