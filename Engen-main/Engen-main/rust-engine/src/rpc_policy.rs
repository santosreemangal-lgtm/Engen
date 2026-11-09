//! Bounded, read-only JSON-RPC transport. Errors deliberately omit endpoint URLs.

use std::{sync::Arc, time::Duration};

use serde::{Deserialize, Serialize};
use serde_json::Value;
use thiserror::Error;
use tokio::{sync::Mutex, time::sleep};
use url::Url;

#[derive(Debug, Error)]
pub enum RpcError {
    #[error("RPC URL is invalid or violates transport policy")]
    InvalidUrl,
    #[error("RPC request failed after bounded retries")]
    Transport,
    #[error("RPC provider returned HTTP status {0}")]
    Http(u16),
    #[error("RPC provider returned invalid JSON")]
    InvalidResponse,
    #[error("RPC provider returned a JSON-RPC error")]
    Provider,
}

#[derive(Debug, Clone)]
pub struct RpcPolicy {
    pub timeout: Duration,
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

pub fn validate_rpc_url(raw: &str) -> Result<Url, RpcError> {
    let url = Url::parse(raw.trim()).map_err(|_| RpcError::InvalidUrl)?;
    let host = url.host_str().ok_or(RpcError::InvalidUrl)?.to_ascii_lowercase();
    let loopback = host == "localhost" || host == "127.0.0.1" || host == "[::1]" || host == "::1";
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
        if policy.timeout.is_zero() || policy.timeout > Duration::from_secs(60) {
            return Err(RpcError::InvalidUrl);
        }
        policy.retries = policy.retries.min(5);
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
        if method.trim().is_empty() {
            return Err(RpcError::InvalidResponse);
        }
        let body = RpcRequest { jsonrpc: "2.0", id: 1, method, params };
        for attempt in 0..=self.policy.retries {
            self.pace().await;
            let response = self.client.post(self.endpoint.clone()).json(&body).send().await;
            match response {
                Ok(response) if response.status().is_success() => {
                    let envelope: RpcEnvelope = response.json().await.map_err(|_| RpcError::InvalidResponse)?;
                    if envelope.error.is_some() {
                        return Err(RpcError::Provider);
                    }
                    return envelope.result.ok_or(RpcError::InvalidResponse);
                }
                Ok(response) => {
                    let status = response.status().as_u16();
                    if attempt == self.policy.retries || (status != 429 && status < 500) {
                        return Err(RpcError::Http(status));
                    }
                }
                Err(_) if attempt == self.policy.retries => return Err(RpcError::Transport),
                Err(_) => {}
            }
            let multiplier = 1u32.checked_shl(attempt.min(8)).unwrap_or(256);
            sleep(self.policy.backoff.saturating_mul(multiplier)).await;
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
    }

    #[test]
    fn rejects_embedded_credentials() {
        assert!(validate_rpc_url("https://user:pass@example.com").is_err());
    }
}
