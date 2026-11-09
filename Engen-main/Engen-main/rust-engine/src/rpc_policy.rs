//! Bounded, read-only JSON-RPC transport with pacing and retry policy.
//! Endpoint credentials and URLs are never included in returned errors.

use std::{env, sync::Arc, time::Duration};

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use thiserror::Error;
use tokio::{sync::Mutex, time::sleep};
use url::Url;

const MAX_RPC_RETRIES: u32 = 5;
const MAX_SECONDS: f64 = 60.0;
const MAX_RETRY_AFTER: Duration = Duration::from_secs(600);

#[derive(Debug, Error, PartialEq, Eq)]
pub enum RpcError {
    #[error("RPC URL is invalid or violates transport policy")]
    InvalidUrl,
    #[error("RPC policy configuration is invalid")]
    InvalidPolicy,
    #[error("RPC request failed after bounded retries")]
    Transport,
    #[error("RPC provider returned HTTP status {0}")]
    Http(u16),
    #[error("RPC provider returned invalid JSON")]
    InvalidResponse,
    #[error("RPC provider returned a JSON-RPC error")]
    Provider,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RpcPolicy {
    pub timeout: Duration,
    /// Number of retries after the initial attempt; capped at five.
    pub retries: u32,
    pub backoff: Duration,
    pub min_interval: Duration,
}

impl Default for RpcPolicy {
    fn default() -> Self {
        Self {
            timeout: Duration::from_secs(10),
            retries: 2,
            backoff: Duration::from_millis(500),
            min_interval: Duration::from_millis(100),
        }
    }
}

impl RpcPolicy {
    /// Load the same environment variables and safety bounds as engine/rpc_policy.py.
    pub fn from_env() -> Result<Self, RpcError> {
        let mut policy = Self::default();
        if let Ok(raw) = env::var("ENGEN_RPC_TIMEOUT_SECONDS") {
            policy.timeout = parse_seconds(&raw, false)?;
        }
        if policy.timeout.is_zero() || policy.timeout > Duration::from_secs(60) {
            return Err(RpcError::InvalidPolicy);
        }
        if let Ok(raw) = env::var("ENGEN_RPC_RETRIES") {
            policy.retries = raw.trim().parse::<u32>().map_err(|_| RpcError::InvalidPolicy)?.min(MAX_RPC_RETRIES);
        }
        if let Ok(raw) = env::var("ENGEN_RPC_BACKOFF_SECONDS") {
            policy.backoff = parse_seconds(&raw, true)?;
        }
        if let Ok(raw) = env::var("ENGEN_RPC_MIN_INTERVAL_SECONDS") {
            policy.min_interval = parse_seconds(&raw, true)?;
        }
        Ok(policy)
    }

    fn validate(&mut self) -> Result<(), RpcError> {
        if self.timeout.is_zero() || self.timeout > Duration::from_secs(60)
            || self.backoff > Duration::from_secs(60)
            || self.min_interval > Duration::from_secs(60) {
            return Err(RpcError::InvalidPolicy);
        }
        self.retries = self.retries.min(MAX_RPC_RETRIES);
        Ok(())
    }
}

fn parse_seconds(raw: &str, allow_zero: bool) -> Result<Duration, RpcError> {
    let value = raw.trim().parse::<f64>().map_err(|_| RpcError::InvalidPolicy)?;
    if !value.is_finite() || value < 0.0 || value > MAX_SECONDS || (!allow_zero && value == 0.0) {
        return Err(RpcError::InvalidPolicy);
    }
    Ok(Duration::from_secs_f64(value))
}

#[derive(Clone)]
pub struct RpcClient {
    client: reqwest::Client,
    endpoint: Url,
    policy: RpcPolicy,
    last_request: Arc<Mutex<Option<tokio::time::Instant>>>,
}

#[derive(Debug, Serialize)]
struct RpcRequest<'a> {
    jsonrpc: &'static str,
    id: u64,
    method: &'a str,
    params: Value,
}

#[derive(Debug, Deserialize)]
struct RpcEnvelope {
    result: Option<Value>,
    error: Option<Value>,
}

fn retry_after_delay(value: &str, now: DateTime<Utc>) -> Option<Duration> {
    let value = value.trim();
    if let Ok(seconds) = value.parse::<u64>() {
        return Some(Duration::from_secs(seconds).min(MAX_RETRY_AFTER));
    }
    DateTime::parse_from_rfc2822(value).ok().map(|date| {
        date.signed_duration_since(now)
            .to_std()
            .unwrap_or_default()
            .min(MAX_RETRY_AFTER)
    })
}

pub fn validate_rpc_url(raw: &str) -> Result<Url, RpcError> {
    if raw.trim().is_empty() { return Err(RpcError::InvalidUrl); }
    let url = Url::parse(raw.trim()).map_err(|_| RpcError::InvalidUrl)?;
    let host = url.host_str().ok_or(RpcError::InvalidUrl)?.to_ascii_lowercase();
    let loopback = host == "localhost" || host == "127.0.0.1" || host == "::1";
    if url.username() != "" || url.password().is_some() || url.fragment().is_some() {
        return Err(RpcError::InvalidUrl);
    }
    if url.scheme() != "https" && !(url.scheme() == "http" && loopback) {
        return Err(RpcError::InvalidUrl);
    }
    Ok(url)
}

impl RpcClient {
    pub fn new(raw_url: &str, mut policy: RpcPolicy) -> Result<Self, RpcError> {
        policy.validate()?;
        let endpoint = validate_rpc_url(raw_url)?;
        let client = reqwest::Client::builder()
            .timeout(policy.timeout)
            .build()
            .map_err(|_| RpcError::Transport)?;
        Ok(Self {
            client,
            endpoint,
            policy,
            last_request: Arc::new(Mutex::new(None)),
        })
    }

    /// Construct a client using ENGEN_RPC_* environment configuration.
    pub fn from_env(raw_url: &str) -> Result<Self, RpcError> {
        Self::new(raw_url, RpcPolicy::from_env()?)
    }

    async fn pace(&self) {
        let mut last = self.last_request.lock().await;
        if let Some(previous) = *last {
            let elapsed = previous.elapsed();
            if elapsed < self.policy.min_interval {
                sleep(self.policy.min_interval - elapsed).await;
            }
        }
        *last = Some(tokio::time::Instant::now());
    }

    pub async fn call(&self, method: &str, params: Value) -> Result<Value, RpcError> {
        if method.trim().is_empty() { return Err(RpcError::InvalidResponse); }
        let body = RpcRequest { jsonrpc: "2.0", id: 1, method, params };
        for attempt in 0..=self.policy.retries {
            self.pace().await;
            let response = self.client.post(self.endpoint.clone()).json(&body).send().await;
            let mut provider_delay = None;
            match response {
                Ok(response) if response.status().is_success() => {
                    let envelope: RpcEnvelope = response.json().await.map_err(|_| RpcError::InvalidResponse)?;
                    if envelope.error.is_some() { return Err(RpcError::Provider); }
                    return envelope.result.ok_or(RpcError::InvalidResponse);
                }
                Ok(response) => {
                    let status = response.status().as_u16();
                    provider_delay = response.headers().get(reqwest::header::RETRY_AFTER)
                        .and_then(|value| value.to_str().ok())
                        .and_then(|value| retry_after_delay(value, Utc::now()));
                    if attempt == self.policy.retries || (status != 429 && !(500..=599).contains(&status)) {
                        return Err(RpcError::Http(status));
                    }
                }
                Err(_) if attempt == self.policy.retries => return Err(RpcError::Transport),
                Err(_) => {}
            }
            let multiplier = 1u32.checked_shl(attempt.min(8)).unwrap_or(256);
            let exponential = self.policy.backoff.saturating_mul(multiplier);
            sleep(provider_delay.unwrap_or(exponential).min(MAX_RETRY_AFTER)).await;
        }
        Err(RpcError::Transport)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn requires_https_for_remote_rpc() {
        assert!(validate_rpc_url("http://rpc.example.com").is_err());
        assert!(validate_rpc_url("https://rpc.example.com").is_ok());
        assert!(validate_rpc_url("http://127.0.0.1:8899").is_ok());
        assert!(validate_rpc_url("http://[::1]:8899").is_ok());
    }

    #[test]
    fn rejects_embedded_credentials_and_fragments() {
        assert!(validate_rpc_url("https://user:pass@example.com").is_err());
        assert!(validate_rpc_url("https://rpc.example.com/#secret").is_err());
    }

    #[test]
    fn parses_retry_after_seconds_and_http_date() {
        assert_eq!(retry_after_delay("30", Utc::now()), Some(Duration::from_secs(30)));
        assert_eq!(retry_after_delay("not-a-date", Utc::now()), None);
        assert_eq!(retry_after_delay("9999", Utc::now()), Some(MAX_RETRY_AFTER));
    }

    #[test]
    fn policy_caps_retries_and_rejects_invalid_bounds() {
        let mut policy = RpcPolicy { retries: 99, ..RpcPolicy::default() };
        policy.validate().unwrap();
        assert_eq!(policy.retries, MAX_RPC_RETRIES);
        let mut invalid = RpcPolicy { timeout: Duration::ZERO, ..RpcPolicy::default() };
        assert_eq!(invalid.validate(), Err(RpcError::InvalidPolicy));
    }

    #[test]
    fn parses_bounded_seconds() {
        assert_eq!(parse_seconds("0", true).unwrap(), Duration::ZERO);
        assert!(parse_seconds("61", true).is_err());
        assert!(parse_seconds("NaN", true).is_err());
    }
}
