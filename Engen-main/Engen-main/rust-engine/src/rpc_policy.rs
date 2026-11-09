//! Validation helpers for the public, read-only Solana RPC boundary.

use std::net::IpAddr;
use url::Url;

pub const MAX_RPC_RETRIES: u8 = 5;
pub const MAX_RPC_TIMEOUT_SECONDS: f64 = 60.0;

pub fn validate_rpc_url(raw: &str) -> Result<String, String> {
    let value = raw.trim();
    if value.is_empty() {
        return Err("ENGEN_RPC_URL must be a non-empty URL.".to_string());
    }

    let parsed = Url::parse(value).map_err(|_| "ENGEN_RPC_URL is malformed.".to_string())?;
    let host = parsed
        .host_str()
        .ok_or_else(|| "ENGEN_RPC_URL must include a host.".to_string())?;

    if !parsed.username().is_empty() || parsed.password().is_some() {
        return Err("ENGEN_RPC_URL must not embed credentials.".to_string());
    }
    if parsed.fragment().is_some() {
        return Err("ENGEN_RPC_URL must not contain a fragment.".to_string());
    }

    let loopback = host.eq_ignore_ascii_case("localhost")
        || host
            .parse::<IpAddr>()
            .map(|address| address.is_loopback())
            .unwrap_or(false);

    match parsed.scheme() {
        "https" => {}
        "http" if loopback => {}
        _ => {
            return Err(
                "ENGEN_RPC_URL must use HTTPS; HTTP is allowed only for localhost.".to_string(),
            )
        }
    }

    Ok(value.to_string())
}

pub fn validate_rpc_timeout(timeout: f64) -> Result<f64, String> {
    if !timeout.is_finite() || timeout <= 0.0 || timeout > MAX_RPC_TIMEOUT_SECONDS {
        return Err("RPC timeout must be finite and greater than 0 through 60 seconds.".to_string());
    }
    Ok(timeout)
}

pub fn bounded_rpc_retries(retries: u16) -> u8 {
    retries.min(u16::from(MAX_RPC_RETRIES)) as u8
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn requires_https_for_remote_rpc() {
        assert!(validate_rpc_url("https://api.mainnet-beta.solana.com").is_ok());
        assert!(validate_rpc_url("http://127.0.0.1:8899").is_ok());
        assert!(validate_rpc_url("http://localhost:8899").is_ok());
        assert!(validate_rpc_url("http://api.mainnet-beta.solana.com").is_err());
    }

    #[test]
    fn rejects_credentials_and_fragments() {
        assert!(validate_rpc_url("https://user:pass@example.com").is_err());
        assert!(validate_rpc_url("https://example.com/#secret").is_err());
    }

    #[test]
    fn rejects_invalid_timeouts() {
        for value in [0.0, -1.0, 60.1, f64::NAN, f64::INFINITY] {
            assert!(validate_rpc_timeout(value).is_err());
        }
        assert_eq!(validate_rpc_timeout(2.5).unwrap(), 2.5);
    }

    #[test]
    fn caps_retry_count() {
        assert_eq!(bounded_rpc_retries(2), 2);
        assert_eq!(bounded_rpc_retries(500), MAX_RPC_RETRIES);
    }
}
