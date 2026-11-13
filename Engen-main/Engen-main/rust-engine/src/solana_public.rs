//! Read-only Solana JSON-RPC client with a closed method allowlist.

use std::io::Read;
use std::sync::{Mutex, OnceLock};
use std::thread;
use std::time::{Duration, Instant, SystemTime};

use reqwest::blocking::Client;
use reqwest::header::RETRY_AFTER;
use serde_json::{json, Value};
use thiserror::Error;

use crate::monitor_scheduler::RateLimitBackoff;
use crate::rpc_policy::{validate_rpc_timeout, validate_rpc_url, MAX_RPC_RETRIES};
use crate::solana_validation::{
    is_valid_transaction_signature, require_solana_address, require_transaction_signature,
};

pub const DEFAULT_RPC_URL: &str = "https://api.mainnet-beta.solana.com";
const MAX_BACKOFF_SECONDS: u64 = 600;
pub const MAX_RPC_RESPONSE_BYTES: usize = 8 * 1024 * 1024;

static PACER: OnceLock<Mutex<Option<Instant>>> = OnceLock::new();

/// Only public, read-only methods are representable by this type.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReadOnlyMethod {
    GetHealth,
    GetSlot,
    GetLatestBlockhash,
    GetSignatureStatuses,
    GetBlockTime,
    GetSignaturesForAddress,
    GetTransaction,
}

impl ReadOnlyMethod {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::GetHealth => "getHealth",
            Self::GetSlot => "getSlot",
            Self::GetLatestBlockhash => "getLatestBlockhash",
            Self::GetSignatureStatuses => "getSignatureStatuses",
            Self::GetBlockTime => "getBlockTime",
            Self::GetSignaturesForAddress => "getSignaturesForAddress",
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
    #[error("Public Solana RPC response exceeded the configured size limit.")]
    ResponseTooLarge,
    #[error("Solana RPC provider does not support this transaction version.")]
    UnsupportedTransactionVersion,
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

#[derive(Debug, Default)]
struct RpcRateLimitState {
    backoff: RateLimitBackoff,
    blocked_until: Option<Instant>,
}

pub struct SolanaRpcClient {
    config: RpcConfig,
    client: Client,
    rate_limit_state: Mutex<RpcRateLimitState>,
}

impl SolanaRpcClient {
    pub fn new(config: RpcConfig) -> Result<Self, SolanaRpcError> {
        config.validate()?;
        let client = Client::builder()
            .timeout(config.timeout)
            .build()
            .map_err(|_| SolanaRpcError::Transport)?;
        Ok(Self {
            config,
            client,
            rate_limit_state: Mutex::new(RpcRateLimitState::default()),
        })
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
            self.wait_for_rate_limit_cooldown();
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
                        if status.as_u16() == 429 {
                            // Keep the 429 cooldown active across separate calls on this client.
                            self.register_rate_limit(retry_after);
                            if attempt >= self.config.retries {
                                return Err(SolanaRpcError::Http(429));
                            }
                            continue;
                        }
                        if !status.is_server_error() || attempt >= self.config.retries {
                            return Err(SolanaRpcError::Http(status.as_u16()));
                        }
                        self.wait_before_retry(attempt);
                        continue;
                    }

                    let content_length = response.content_length();
                    let body = read_limited_body(response, content_length)?;
                    let retryable_provider_error = provider_error_is_retryable(&body);
                    match validate_response(body, request_id) {
                        Ok(value) => {
                            self.reset_rate_limit();
                            return Ok(value);
                        }
                        Err(SolanaRpcError::Provider(code)) if retryable_provider_error => {
                            self.register_rate_limit(None);
                            if attempt < self.config.retries {
                                continue;
                            }
                            return Err(SolanaRpcError::Provider(code));
                        }
                        Err(error) => return Err(error),
                    }
                }
                Err(error) => {
                    let retryable = error.is_timeout() || error.is_connect() || error.is_request();
                    if !retryable || attempt >= self.config.retries {
                        return Err(SolanaRpcError::Transport);
                    }
                    self.wait_before_retry(attempt);
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

    /// Fetch a block timestamp from a public slot; null means the provider has no timestamp yet.
    pub fn get_block_time(&self, slot: u64) -> Result<Option<i64>, SolanaRpcError> {
        if slot > i64::MAX as u64 {
            return Err(SolanaRpcError::InvalidInput(
                "Slot is out of range.".to_string(),
            ));
        }
        let value = self.request(ReadOnlyMethod::GetBlockTime, json!([slot]), 7)?;
        parse_block_time(value)
    }

    /// Fetch a bounded, newest-first page of public wallet signatures.
    pub fn get_signatures_for_address(
        &self,
        wallet_address: &str,
        limit: usize,
        before: Option<&str>,
    ) -> Result<Vec<Value>, SolanaRpcError> {
        require_solana_address(wallet_address)
            .map_err(|error| SolanaRpcError::InvalidInput(error.to_string()))?;
        if !(1..=1000).contains(&limit) {
            return Err(SolanaRpcError::InvalidInput(
                "Signature-page limit must be between 1 and 1000.".to_string(),
            ));
        }
        if let Some(cursor) = before {
            require_transaction_signature(cursor)
                .map_err(|error| SolanaRpcError::InvalidInput(error.to_string()))?;
        }
        let mut options = serde_json::Map::new();
        options.insert("limit".to_string(), json!(limit));
        if let Some(cursor) = before {
            options.insert("before".to_string(), json!(cursor));
        }
        let value = self.request(
            ReadOnlyMethod::GetSignaturesForAddress,
            json!([wallet_address, Value::Object(options)]),
            6,
        )?;
        let rows = value.as_array().ok_or(SolanaRpcError::InvalidResponse)?;
        Ok(normalize_signature_rows(rows))
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

    fn wait_before_retry(&self, attempt: u8) {
        let exponent = u32::from(attempt.min(16));
        let factor = 1_u32.checked_shl(exponent).unwrap_or(u32::MAX);
        let delay = self
            .config
            .backoff
            .saturating_mul(factor)
            .min(Duration::from_secs(MAX_BACKOFF_SECONDS));
        if !delay.is_zero() {
            thread::sleep(delay);
        }
    }

    fn register_rate_limit(&self, retry_after: Option<Duration>) -> Duration {
        let mut state = self
            .rate_limit_state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let delay = state.backoff.on_rate_limited(retry_after);
        state.blocked_until = Some(Instant::now() + delay);
        delay
    }

    fn wait_for_rate_limit_cooldown(&self) {
        loop {
            let delay = {
                let state = self
                    .rate_limit_state
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner());
                state
                    .blocked_until
                    .map(|deadline| deadline.saturating_duration_since(Instant::now()))
                    .unwrap_or(Duration::ZERO)
            };
            if delay.is_zero() {
                return;
            }
            thread::sleep(delay);
        }
    }

    fn reset_rate_limit(&self) {
        let mut state = self
            .rate_limit_state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        state.backoff.on_success();
        state.blocked_until = None;
    }
}

fn read_limited_body<R: Read>(
    reader: R,
    content_length: Option<u64>,
) -> Result<Value, SolanaRpcError> {
    if content_length.is_some_and(|length| length > MAX_RPC_RESPONSE_BYTES as u64) {
        return Err(SolanaRpcError::ResponseTooLarge);
    }

    let mut bytes = Vec::new();
    reader
        .take((MAX_RPC_RESPONSE_BYTES as u64) + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| SolanaRpcError::Transport)?;
    if bytes.len() > MAX_RPC_RESPONSE_BYTES {
        return Err(SolanaRpcError::ResponseTooLarge);
    }
    serde_json::from_slice(&bytes).map_err(|_| SolanaRpcError::InvalidResponse)
}

fn provider_error_is_retryable(value: &Value) -> bool {
    let Some(error) = value.get("error").and_then(Value::as_object) else {
        return false;
    };
    let code = error.get("code").and_then(Value::as_i64);
    if matches!(code, Some(429 | -32005)) {
        return true;
    }
    let message = error
        .get("message")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_ascii_lowercase();
    ["rate limit", "rate-limit", "too many request", "429"]
        .iter()
        .any(|marker| message.contains(marker))
}

fn parse_block_time(value: Value) -> Result<Option<i64>, SolanaRpcError> {
    if value.is_null() {
        return Ok(None);
    }
    let timestamp = value.as_i64().ok_or(SolanaRpcError::InvalidResponse)?;
    if timestamp <= 0 {
        return Err(SolanaRpcError::InvalidResponse);
    }
    Ok(Some(timestamp))
}

fn normalize_signature_rows(rows: &[Value]) -> Vec<Value> {
    rows.iter()
        .filter(|item| {
            if !item.is_object() {
                return false;
            }
            let Some(signature) = item.get("signature").and_then(Value::as_str) else {
                return false;
            };
            if !is_valid_transaction_signature(signature) {
                return false;
            }
            match item.get("slot") {
                None | Some(Value::Null) => true,
                Some(slot) => slot.as_u64().is_some(),
            }
        })
        .cloned()
        .collect()
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
        let message = error_object
            .get("message")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_ascii_lowercase();
        if code == Some(-32015)
            || (message.contains("transaction version")
                && (message.contains("not supported") || message.contains("unsupported")))
        {
            return Err(SolanaRpcError::UnsupportedTransactionVersion);
        }
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
            ReadOnlyMethod::GetBlockTime,
            ReadOnlyMethod::GetTransaction,
        ];
        assert!(methods
            .iter()
            .all(|method| !method.as_str().contains("send")));
    }

    #[test]
    fn block_time_response_is_parsed_without_fabricating_missing_values() {
        assert_eq!(parse_block_time(Value::Null).unwrap(), None);
        assert_eq!(
            parse_block_time(json!(1_760_000_000)).unwrap(),
            Some(1_760_000_000)
        );
        assert!(matches!(
            parse_block_time(json!(0)),
            Err(SolanaRpcError::InvalidResponse)
        ));
        assert!(matches!(
            parse_block_time(json!(-1)),
            Err(SolanaRpcError::InvalidResponse)
        ));
        assert!(matches!(
            parse_block_time(json!("1760000000")),
            Err(SolanaRpcError::InvalidResponse)
        ));
    }


    #[test]
    fn client_keeps_429_cooldown_across_requests_and_honors_retry_after() {
        let client = SolanaRpcClient::new(RpcConfig {
            endpoint: DEFAULT_RPC_URL.to_string(),
            timeout: Duration::from_secs(1),
            retries: 0,
            backoff: Duration::from_millis(1),
            min_interval: Duration::ZERO,
        })
        .unwrap();

        assert_eq!(client.register_rate_limit(None), Duration::from_secs(30));
        assert_eq!(
            client.register_rate_limit(Some(Duration::from_secs(90))),
            Duration::from_secs(90)
        );
        {
            let state = client.rate_limit_state.lock().unwrap();
            assert_eq!(state.backoff.current(), Duration::from_secs(90));
            assert!(state.blocked_until.is_some());
        }
        client.reset_rate_limit();
        let state = client.rate_limit_state.lock().unwrap();
        assert_eq!(state.backoff.current(), Duration::ZERO);
        assert!(state.blocked_until.is_none());
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

        let target = SystemTime::now() + Duration::from_secs(60);
        let date = httpdate::fmt_http_date(target);
        let parsed = parse_retry_after(&date).expect("valid HTTP date should parse");
        assert!(parsed >= Duration::from_secs(55));
        assert!(parsed <= Duration::from_secs(60));
        assert!(parse_retry_after("not-a-date").is_none());
    }

    #[test]
    fn signature_page_filter_preserves_provider_metadata_and_drops_malformed_rows() {
        let signature = "1".repeat(64);
        let rows = vec![
            json!({"signature":signature,"slot":123,"err":null,"blockTime":456}),
            json!({"signature":"bad","slot":123}),
            json!({"signature": "1".repeat(64), "slot":-1}),
            json!("not-an-object"),
            json!({"slot":123}),
        ];
        let result = normalize_signature_rows(&rows);
        assert_eq!(result.len(), 1);
        assert_eq!(result[0]["slot"], 123);
        assert_eq!(result[0]["blockTime"], 456);
    }

    #[test]
    fn retry_count_is_validated() {
        let config = RpcConfig {
            retries: 99,
            ..RpcConfig::default()
        };
        assert!(config.validate().is_err());
    }

    #[test]
    fn rpc_response_body_is_bounded_even_without_content_length() {
        use std::io::Cursor;

        let too_large = vec![b' '; MAX_RPC_RESPONSE_BYTES + 1];
        assert!(matches!(
            read_limited_body(Cursor::new(too_large), None),
            Err(SolanaRpcError::ResponseTooLarge)
        ));
        assert!(matches!(
            read_limited_body(
                Cursor::new(b"{}".to_vec()),
                Some((MAX_RPC_RESPONSE_BYTES + 1) as u64)
            ),
            Err(SolanaRpcError::ResponseTooLarge)
        ));
        assert_eq!(
            read_limited_body(Cursor::new(br#"{"ok":true}"#.to_vec()), Some(11)).unwrap(),
            json!({"ok":true})
        );
    }

    #[test]
    fn provider_rate_limit_errors_are_retryable_but_version_errors_are_not() {
        assert!(provider_error_is_retryable(&json!({
            "error":{"code":429,"message":"Too many requests"}
        })));
        assert!(provider_error_is_retryable(&json!({
            "error":{"code":-32005,"message":"Node is unhealthy"}
        })));
        assert!(provider_error_is_retryable(&json!({
            "error":{"code":-32000,"message":"Provider rate limit exceeded"}
        })));
        assert!(!provider_error_is_retryable(&json!({
            "error":{"code":-32015,"message":"Transaction version not supported"}
        })));
        assert!(matches!(
            validate_response(
                json!({"jsonrpc":"2.0","id":5,"error":{
                    "code":-32015,
                    "message":"Transaction version (1) is not supported"
                }}),
                5
            ),
            Err(SolanaRpcError::UnsupportedTransactionVersion)
        ));
        assert!(!provider_error_is_retryable(&json!({"result":null})));
    }

    #[test]
    fn retries_provider_rate_limit_response_using_offline_mock_server() {
        use std::io::Write;
        use std::net::TcpListener;

        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let server = thread::spawn(move || {
            let responses = [
                (
                    429,
                    "Retry-After: 0\r\n",
                    json!({"error":{"code":429,"message":"HTTP rate limit"}}),
                ),
                (
                    200,
                    "",
                    json!({"jsonrpc":"2.0","id":7,"error":{"code":429,"message":"provider rate limit"}}),
                ),
                (200, "", json!({"jsonrpc":"2.0","id":7,"result":42})),
            ];
            for (status, extra_headers, body) in responses {
                let (mut stream, _) = listener.accept().unwrap();
                stream
                    .set_read_timeout(Some(Duration::from_secs(2)))
                    .unwrap();
                // Drain the request before closing the socket. Otherwise TCP may
                // reset a connection with unread request bytes and look like a
                // client transport failure rather than a retry assertion.
                let mut request = [0_u8; 4096];
                let _ = stream.read(&mut request);
                let payload = serde_json::to_vec(&body).unwrap();
                let reason = if status == 429 {
                    "Too Many Requests"
                } else {
                    "OK"
                };
                let header = format!(
                    "HTTP/1.1 {status} {reason}\r\n{extra_headers}Content-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                    payload.len()
                );
                stream.write_all(header.as_bytes()).unwrap();
                stream.write_all(&payload).unwrap();
                stream.flush().unwrap();
            }
        });

        let client = SolanaRpcClient::new(RpcConfig {
            endpoint: format!("http://{address}"),
            timeout: Duration::from_secs(2),
            retries: 2,
            backoff: Duration::ZERO,
            min_interval: Duration::ZERO,
        })
        .unwrap();
        assert_eq!(
            client
                .request(ReadOnlyMethod::GetSlot, json!([]), 7)
                .unwrap(),
            json!(42)
        );
        server.join().unwrap();
    }

    #[test]
    fn malformed_json_and_wrong_jsonrpc_version_are_rejected() {
        use std::io::Cursor;

        assert!(matches!(
            read_limited_body(Cursor::new(b"{not-json".to_vec()), None),
            Err(SolanaRpcError::InvalidResponse)
        ));
        assert!(matches!(
            validate_response(json!({"jsonrpc":"1.0","id":7,"result":42}), 7),
            Err(SolanaRpcError::InvalidResponse)
        ));
        assert!(matches!(
            validate_response(json!({"jsonrpc":"2.0","id":7}), 7),
            Err(SolanaRpcError::InvalidResponse)
        ));
    }

    #[test]
    fn retries_server_error_using_offline_mock_server() {
        use std::io::Write;
        use std::net::TcpListener;

        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let server = thread::spawn(move || {
            for (status, body) in [
                (503, json!({"message":"temporary provider outage"})),
                (200, json!({"jsonrpc":"2.0","id":7,"result":43})),
            ] {
                let (mut stream, _) = listener.accept().unwrap();
                stream
                    .set_read_timeout(Some(Duration::from_secs(2)))
                    .unwrap();
                let mut request = [0_u8; 4096];
                let _ = stream.read(&mut request);
                let payload = serde_json::to_vec(&body).unwrap();
                let reason = if status == 503 {
                    "Service Unavailable"
                } else {
                    "OK"
                };
                let header = format!(
                    "HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                    payload.len()
                );
                stream.write_all(header.as_bytes()).unwrap();
                stream.write_all(&payload).unwrap();
                stream.flush().unwrap();
            }
        });

        let client = SolanaRpcClient::new(RpcConfig {
            endpoint: format!("http://{address}"),
            timeout: Duration::from_secs(2),
            retries: 1,
            backoff: Duration::ZERO,
            min_interval: Duration::ZERO,
        })
        .unwrap();
        assert_eq!(
            client
                .request(ReadOnlyMethod::GetSlot, json!([]), 7)
                .unwrap(),
            json!(43)
        );
        server.join().unwrap();
    }

    #[test]
    fn request_timeout_returns_typed_transport_error() {
        use std::io::Write;
        use std::net::TcpListener;

        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            stream
                .set_read_timeout(Some(Duration::from_secs(2)))
                .unwrap();
            let mut request = [0_u8; 4096];
            let _ = stream.read(&mut request);
            thread::sleep(Duration::from_millis(250));

            let payload = br#"{"jsonrpc":"2.0","id":7,"result":44}"#;
            let header = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                payload.len()
            );
            // The client should have timed out and closed its side already.
            let _ = stream.write_all(header.as_bytes());
            let _ = stream.write_all(payload);
        });

        let client = SolanaRpcClient::new(RpcConfig {
            endpoint: format!("http://{address}"),
            timeout: Duration::from_millis(75),
            retries: 0,
            backoff: Duration::ZERO,
            min_interval: Duration::ZERO,
        })
        .unwrap();
        assert!(matches!(
            client.request(ReadOnlyMethod::GetSlot, json!([]), 7),
            Err(SolanaRpcError::Transport)
        ));
        server.join().unwrap();
    }

    #[test]
    fn connection_failure_returns_typed_transport_error() {
        use std::net::TcpListener;

        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        drop(listener);

        let client = SolanaRpcClient::new(RpcConfig {
            endpoint: format!("http://{address}"),
            timeout: Duration::from_secs(1),
            retries: 0,
            backoff: Duration::ZERO,
            min_interval: Duration::ZERO,
        })
        .unwrap();
        assert!(matches!(
            client.request(ReadOnlyMethod::GetSlot, json!([]), 7),
            Err(SolanaRpcError::Transport)
        ));
    }
}
