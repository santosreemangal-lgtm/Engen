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
        let token = env::var("DASHBOARD_AUTH_TOKEN").ok();
        let trust_proxy = env::var("DASHBOARD_TRUST_PROXY").ok().as_deref() == Some("true");
        validate_bind(&bind, trust_proxy, token.as_deref())?;
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

/// Localhost may run without a token. Any other bind needs an explicit proxy
/// trust flag and a bearer token of at least 32 characters.
pub fn validate_bind(bind: &str, trust_proxy: bool, token: Option<&str>) -> Result<()> {
    let host = bind.split(':').next().unwrap_or("");
    if host == "127.0.0.1" || host == "localhost" || host == "::1" {
        return Ok(());
    }
    if !trust_proxy {
        return Err(EngenError::InvalidInput(
            "Non-local bind requires DASHBOARD_TRUST_PROXY=true".into(),
        ));
    }
    if token.map(|t| t.len()).unwrap_or(0) < 32 {
        return Err(EngenError::InvalidInput(
            "Non-local bind requires DASHBOARD_AUTH_TOKEN of at least 32 characters".into(),
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn localhost_needs_no_token() {
        assert!(validate_bind("127.0.0.1:8787", false, None).is_ok());
    }

    #[test]
    fn public_bind_is_refused_without_proxy_and_token() {
        assert!(validate_bind("0.0.0.0:8787", false, None).is_err());
        assert!(validate_bind("0.0.0.0:8787", true, Some("short")).is_err());
        assert!(validate_bind("0.0.0.0:8787", true, Some(&"x".repeat(32))).is_ok());
    }
}
