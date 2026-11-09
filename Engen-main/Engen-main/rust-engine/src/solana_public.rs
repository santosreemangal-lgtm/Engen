//! Read-only Solana JSON-RPC client with a closed method allowlist.

use std::sync::{Mutex, OnceLock};
use std::thread;
use std::time::{Duration, Instant, SystemTime};

use reqwest::blocking::Client;
use reqwest::header::RETRY_AFTER;
use serde_json::{json, Value};
use thiserror::Error;

use crate::rpc_policy::{validate_rpc_timeout, validate_rpc_url, MAX_RPC_RETRIES};
use crate::solana_validation::require_transaction_signature;

pub const DEFAULT_RPC_URL: &str = "https://api.mainnet-beta.solana.com";
const MAX_BACKOFF_SECONDS: u64 = 600;

static PACER: OnceLock<Mutex<Option<Instant>>> = OnceLock::new();

/// Only public, read-only methods are representable by this type.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReadOnlyMethod {
    GetHealth,
    GetSlot,
    GetLatestBlockhash,
    GetSignatureStatuses,
    GetTransaction,
}

impl ReadOnlyMethod {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::GetHealth => "getHealth",
            Self::GetSlot => "getSlot",
            Self::GetLatestBlockhash => "getLatestBlockhash",
            Self::GetSignatureStatuses => "getSignatureStatuses",
            Self::GetTransaction => "getTransaction",
        }
    }
}

#[derive(Debug, Error)]
pub enum SolanaRpcError {
    #[error("Public Solana RPC transport failed.")]
    Transport,
    #[error("Public Solana RPC returned HTTP status {0}.")]
    Http(u16),
    #[error("Public Solana RPC returned invalid JSON or an invalid response envelope.")]
    InvalidResponse,
    #[error("Solana RPC provider returned an error (code {0:?}).")]
    Provider(Option<i64>),
    #[error("The supplied value is invalid: {0}")]
    InvalidInput(String),
}

#[derive(Debug, Clone)]
pub struct RpcConfig {
    pub endpoint: String,
    pub timeout: Duration,
    pub retries: u8,
    pub backoff: Duration,
    pub min_interval: Duration,
}

impl Default for RpcConfig {
    fn default() -> Self {
        Self {
            endpoint: DEFAULT_RPC_URL.to_string(),
            timeout: Duration::from_secs(10),
            retries: 2,
            backoff: Duration::from_millis(500),
            min_interval: Duration::from_millis(100),
        }
    }
}

impl RpcConfig {
    pub fn validate(&self) -> Result<(), SolanaRpcError> {
        validate_rpc_url(&self.endpoint).map_err(SolanaRpcError::InvalidInput)?;
        validate_rpc_timeout(self.timeout.as_secs_f64()).map_err(SolanaRpcError::InvalidInput)?;
        if self.retries > MAX_RPC_RETRIES {
            return Err(SolanaRpcError::InvalidInput(
                "RPC retry count exceeds the configured safety maximum.".to_string(),
            ));
        }
        if self.backoff > Duration::from_secs(MAX_BACKOFF_SECONDS)
            || self.min_interval > Duration::from_secs(60)
        {
            return Err(SolanaRpcError::InvalidInput(
                "RPC backoff and pacing values must be bounded.".to_string(),
            ));
        }
        Ok(())
    }
}

pub struct SolanaRpcClient {
    config: RpcConfig,
    client: Client,
}

impl SolanaRpcClient {
    pub fn new(config: RpcConfig) -> Result<Self, SolanaRpcError> {
        config.validate()?;
        let client = Client::builder()
            .timeout(config.timeout)
            .build()
            .map_err(|_| SolanaRpcError::Transport)?;
        Ok(Self { config, client })
    }

    pub fn request(
        &self,
        method: ReadOnlyMethod,
        params: Value,
        request_id: u64,
    ) -> Result<Value, SolanaRpcError> {
        let request = json!({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method.as_str(),
            "params": params,
        });

        for attempt in 0..=self.config.retries {
            self.pace();
            let response = self
                .client
                .post(&self.config.endpoint)
                .json(&request)
                .send();

            match response {
                Ok(response) => {
                    let status = response.status();
                    if !status.is_success() {
                        let retry_after = response
                            .headers()
                            .get(RETRY_AFTER)
                            .and_then(|value| value.to_str().ok())
                            .and_then(parse_retry_after);
                        let retryable = status.as_u16() == 429 || status.is_server_error();
                        if !retryable || attempt >= self.config.retries {
                            return Err(SolanaRpcError::Http(status.as_u16()));
                        }
                        self.wait_before_retry(attempt, retry_after);
                        continue;
                    }

                    let body: Value = response
                        .json()
                        .map_err(|_| SolanaRpcError::InvalidResponse)?;
                    return validate_response(body, request_id);
                }
                Err(error) => {
                    let retryable = error.is_timeout() || error.is_connect() || error.is_request();
                    if !retryable || attempt >= self.config.retries {
                        return Err(SolanaRpcError::Transport);
                    }
                    self.wait_before_retry(attempt, None);
                }
            }
        }

        Err(SolanaRpcError::Transport)
    }

    pub fn get_health(&self) -> Result<String, SolanaRpcError> {
        let value = self.request(ReadOnlyMethod::GetHealth, json!([]), 1)?;
        value
            .as_str()
            .filter(|value| !value.is_empty())
            .map(str::to_string)
            .ok_or(SolanaRpcError::InvalidResponse)
    }

    pub fn get_slot(&self) -> Result<u64, SolanaRpcError> {
        let value = self.request(ReadOnlyMethod::GetSlot, json!([]), 2)?;
        value.as_u64().ok_or(SolanaRpcError::InvalidResponse)
    }

    pub fn get_latest_blockhash(&self) -> Result<String, SolanaRpcError> {
        let value = self.request(
            ReadOnlyMethod::GetLatestBlockhash,
            json!([{ "commitment": "confirmed" }]),
            3,
        )?;
        value
            .get("value")
            .and_then(|item| item.get("blockhash"))
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .map(str::to_string)
            .ok_or(SolanaRpcError::InvalidResponse)
    }

    pub fn get_signature_status(&self, signature: &str) -> Result<Option<Value>, SolanaRpcError> {
        require_transaction_signature(signature)
            .map_err(|error| SolanaRpcError::InvalidInput(error.to_string()))?;

        let value = self.request(
            ReadOnlyMethod::GetSignatureStatuses,
            json!([[signature], { "searchTransactionHistory": true }]),
            5,
        )?;
        let statuses = value
            .get("value")
            .and_then(Value::as_array)
            .ok_or(SolanaRpcError::InvalidResponse)?;
        if statuses.len() != 1 {
            return Err(SolanaRpcError::InvalidResponse);
        }

        let Some(status) = statuses.first() else {
            return Err(SolanaRpcError::InvalidResponse);
        };
        if status.is_null() {
            return Ok(None);
        }
        if !status.is_object() {
            return Err(SolanaRpcError::InvalidResponse);
        }

        let confirmation = status.get("confirmationStatus").and_then(Value::as_str);
        if !matches!(confirmation, Some("processed" | "confirmed" | "finalized")) {
            return Ok(None);
        }
        Ok(Some(status.clone()))
    }

    pub fn get_transaction(&self, signature: &str) -> Result<Option<Value>, SolanaRpcError> {
        require_transaction_signature(signature)
            .map_err(|error| SolanaRpcError::InvalidInput(error.to_string()))?;

        let result = self.request(
            ReadOnlyMethod::GetTransaction,
            json!([
                signature,
                {
                    "encoding": "jsonParsed",
                    "commitment": "confirmed",
                    "maxSupportedTransactionVersion": 0
                }
            ]),
            4,
        )?;
        if result.is_null() {
            Ok(None)
        } else if result.is_object() {
            Ok(Some(result))
        } else {
            Err(SolanaRpcError::InvalidResponse)
        }
    }

    fn pace(&self) {
        if self.config.min_interval.is_zero() {
            return;
        }
        let lock = PACER.get_or_init(|| Mutex::new(None));
        let mut last = lock.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
        if let Some(previous) = *last {
            let elapsed = previous.elapsed();
            if elapsed < self.config.min_interval {
                thread::sleep(self.config.min_interval - elapsed);
            }
        }
        *last = Some(Instant::now());
    }

    fn wait_before_retry(&self, attempt: u8, retry_after: Option<Duration>) {
        let exponent = u32::from(attempt.min(16));
        let factor = 1_u32.checked_shl(exponent).unwrap_or(u32::MAX);
        let backoff = self
            .config
            .backoff
            .saturating_mul(factor)
            .min(Duration::from_secs(MAX_BACKOFF_SECONDS));
        let delay = retry_after
            .unwrap_or(backoff)
            .min(Duration::from_secs(MAX_BACKOFF_SECONDS));
        if !delay.is_zero() {
            thread::sleep(delay);
        }
    }
}

fn validate_response(value: Value, expected_id: u64) -> Result<Value, SolanaRpcError> {
    if value.get("jsonrpc").and_then(Value::as_str) != Some("2.0")
        || value.get("id").and_then(Value::as_u64) != Some(expected_id)
    {
        return Err(SolanaRpcError::InvalidResponse);
    }

    if let Some(error) = value.get("error") {
        let Some(error_object) = error.as_object() else {
            return Err(SolanaRpcError::InvalidResponse);
        };
        let code = error_object.get("code").and_then(Value::as_i64);
        return Err(SolanaRpcError::Provider(code));
    }

    value
        .get("result")
        .cloned()
        .ok_or(SolanaRpcError::InvalidResponse)
}

fn parse_retry_after(raw: &str) -> Option<Duration> {
    if let Ok(seconds) = raw.trim().parse::<u64>() {
        return Some(Duration::from_secs(seconds.min(MAX_BACKOFF_SECONDS)));
    }

    let target = httpdate::parse_http_date(raw).ok()?;
    let now = SystemTime::now();
    let delay = target.duration_since(now).unwrap_or(Duration::ZERO);
    Some(delay.min(Duration::from_secs(MAX_BACKOFF_SECONDS)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn read_only_method_set_has_no_broadcast_method() {
        let methods = [
            ReadOnlyMethod::GetHealth,
            ReadOnlyMethod::GetSlot,
            ReadOnlyMethod::GetLatestBlockhash,
            ReadOnlyMethod::GetSignatureStatuses,
            ReadOnlyMethod::GetTransaction,
        ];
        assert!(methods
            .iter()
            .all(|method| !method.as_str().contains("send")));
    }

    #[test]
    fn validates_rpc_envelopes_and_provider_errors() {
        let good = json!({"jsonrpc":"2.0","id":7,"result":123});
        assert_eq!(validate_response(good, 7).unwrap(), json!(123));

        let wrong_id = json!({"jsonrpc":"2.0","id":8,"result":123});
        assert!(matches!(
            validate_response(wrong_id, 7),
            Err(SolanaRpcError::InvalidResponse)
        ));

        let provider = json!({"jsonrpc":"2.0","id":7,"error":{"code":-32005}});
        assert!(matches!(
            validate_response(provider, 7),
            Err(SolanaRpcError::Provider(Some(-32005)))
        ));
    }

    #[test]
    fn parses_retry_after_seconds_and_dates() {
        assert_eq!(parse_retry_after("12"), Some(Duration::from_secs(12)));
        assert_eq!(parse_retry_after("9999"), Some(Duration::from_secs(600)));
        assert!(parse_retry_after("not-a-date").is_none());
    }

    #[test]
    fn retry_count_is_validated() {
        let config = RpcConfig {
            retries: 99,
            ..RpcConfig::default()
        };
        assert!(config.validate().is_err());
    }
}
