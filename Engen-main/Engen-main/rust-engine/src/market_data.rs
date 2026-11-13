//! Bounded read-only DEX Screener market snapshots for paper simulation.

use std::io::Read;
use std::str::FromStr;
use std::time::Duration;

use bigdecimal::BigDecimal;
use chrono::{SecondsFormat, Utc};
use reqwest::blocking::Client;
use reqwest::header::{ACCEPT, USER_AGENT};
use serde_json::Value;
use thiserror::Error;

use crate::pnl::MarketPrice;
use crate::solana_validation::is_valid_solana_address;

pub const DEXSCREENER_TOKEN_PAIRS_URL: &str = "https://api.dexscreener.com/token-pairs/v1/solana/";
const USER_AGENT_VALUE: &str = "SolanaCopyTradingEngine-PaperSimulation/1.0";
const MAX_RESPONSE_BYTES: u64 = 1_000_000;
const MAX_TIMEOUT: Duration = Duration::from_secs(60);

#[derive(Debug, Error)]
pub enum MarketDataError {
    #[error("Market-data timeout must be greater than zero and no more than 60 seconds.")]
    InvalidTimeout,
    #[error("Unable to initialize the read-only market-data client.")]
    Client,
}

fn parse_decimal(value: &Value) -> Option<BigDecimal> {
    let raw = value
        .as_str()
        .map(str::to_string)
        .or_else(|| value.as_number().map(ToString::to_string))?;
    BigDecimal::from_str(raw.trim()).ok()
}

fn snapshot_from_payload(payload: &Value, mint: &str, observed_at: &str) -> Option<MarketPrice> {
    let pairs = payload.as_array()?;
    let mut best: Option<(BigDecimal, BigDecimal)> = None;

    for pair in pairs {
        if pair.get("chainId").and_then(Value::as_str) != Some("solana")
            || pair
                .get("baseToken")
                .and_then(|value| value.get("address"))
                .and_then(Value::as_str)
                != Some(mint)
        {
            continue;
        }
        let Some(liquidity) = pair.get("liquidity") else {
            continue;
        };
        let Some(price) = pair.get("priceUsd").and_then(parse_decimal) else {
            continue;
        };
        let Some(liquidity_usd) = liquidity.get("usd").and_then(parse_decimal) else {
            continue;
        };
        if price <= 0 || liquidity_usd <= 0 {
            continue;
        }
        let replace = best
            .as_ref()
            .is_none_or(|(best_liquidity, _)| liquidity_usd > *best_liquidity);
        if replace {
            best = Some((liquidity_usd, price));
        }
    }

    let (liquidity_usd, price) = best?;
    MarketPrice::new(
        mint,
        &price.normalized().to_string(),
        "USD",
        "dexscreener_public_api",
        observed_at,
        Some(&liquidity_usd.normalized().to_string()),
        Some("dexscreener_public_api".to_string()),
        Some(observed_at.to_string()),
    )
    .ok()
}

/// Return the highest-liquidity valid Solana pool snapshot, or no value on provider/data failure.
///
/// The endpoint is queried over HTTPS with a strict response-size bound. This is indicative
/// market data only; paper execution still applies independent price/liquidity/freshness guards.
pub fn fetch_market_price(
    mint: &str,
    timeout: Duration,
) -> Result<Option<MarketPrice>, MarketDataError> {
    if !is_valid_solana_address(mint) {
        return Ok(None);
    }
    if timeout.is_zero() || timeout > MAX_TIMEOUT {
        return Err(MarketDataError::InvalidTimeout);
    }
    let client = Client::builder()
        .timeout(timeout)
        .build()
        .map_err(|_| MarketDataError::Client)?;
    let url = format!("{DEXSCREENER_TOKEN_PAIRS_URL}{mint}");
    let response = match client
        .get(url)
        .header(ACCEPT, "application/json")
        .header(USER_AGENT, USER_AGENT_VALUE)
        .send()
    {
        Ok(response) => response,
        Err(_) => return Ok(None),
    };
    if !response.status().is_success() {
        return Ok(None);
    }
    if response
        .content_length()
        .is_some_and(|length| length > MAX_RESPONSE_BYTES)
    {
        return Ok(None);
    }

    let mut bounded = response.take(MAX_RESPONSE_BYTES + 1);
    let mut body = Vec::new();
    if bounded.read_to_end(&mut body).is_err() || body.len() as u64 > MAX_RESPONSE_BYTES {
        return Ok(None);
    }
    let payload: Value = match serde_json::from_slice(&body) {
        Ok(payload) => payload,
        Err(_) => return Ok(None),
    };
    let observed_at = Utc::now().to_rfc3339_opts(SecondsFormat::Millis, true);
    Ok(snapshot_from_payload(&payload, mint, &observed_at))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    const MINT: &str = "11111111111111111111111111111111";
    const OBSERVED: &str = "2026-11-10T04:00:00.000Z";

    #[test]
    fn selects_highest_liquidity_valid_solana_pool() {
        let payload = json!([
            {
                "chainId":"solana",
                "baseToken":{"address":MINT},
                "priceUsd":"0.25",
                "liquidity":{"usd":"1000"}
            },
            {
                "chainId":"solana",
                "baseToken":{"address":MINT},
                "priceUsd":"0.30",
                "liquidity":{"usd":"25000"}
            },
            {
                "chainId":"ethereum",
                "baseToken":{"address":MINT},
                "priceUsd":"100",
                "liquidity":{"usd":"100000"}
            }
        ]);
        let price = snapshot_from_payload(&payload, MINT, OBSERVED).unwrap();
        assert_eq!(price.price.to_string(), "0.3");
        assert_eq!(price.liquidity_usd.unwrap().to_string(), "25000");
        assert_eq!(price.source, "dexscreener_public_api");
    }

    #[test]
    fn rejects_invalid_prices_negative_liquidity_and_wrong_base_mints() {
        let payload = json!([
            {
                "chainId":"solana",
                "baseToken":{"address":MINT},
                "priceUsd":"NaN",
                "liquidity":{"usd":"90000"}
            },
            {
                "chainId":"solana",
                "baseToken":{"address":MINT},
                "priceUsd":"1",
                "liquidity":{"usd":"-1"}
            },
            {
                "chainId":"solana",
                "baseToken":{"address":MINT},
                "priceUsd":"1",
                "liquidity":{"usd":"0"}
            },
            {
                "chainId":"solana",
                "baseToken":{"address":"OtherMint"},
                "priceUsd":"1",
                "liquidity":{"usd":"100"}
            }
        ]);
        assert!(snapshot_from_payload(&payload, MINT, OBSERVED).is_none());
    }

    #[test]
    fn rejects_invalid_timeout_and_invalid_mint_without_network_access() {
        assert!(matches!(
            fetch_market_price(MINT, Duration::ZERO),
            Err(MarketDataError::InvalidTimeout)
        ));
        assert_eq!(
            fetch_market_price("not-a-mint", Duration::from_secs(2)).unwrap(),
            None
        );
    }

    #[test]
    fn returns_none_for_non_array_provider_payload() {
        assert!(snapshot_from_payload(&json!({"pairs":[]}), MINT, OBSERVED).is_none());
    }
}
