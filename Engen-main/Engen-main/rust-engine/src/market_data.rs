//! Read-only DEX Screener market snapshots for paper simulation.
use chrono::Utc;
use rust_decimal::Decimal;
use serde_json::Value;
use crate::{pnl::{MarketPrice, PnlError}, solana_public::{is_valid_public_key, SolanaError}};
use thiserror::Error;

const URL: &str = "https://api.dexscreener.com/token-pairs/v1/solana/";
const USER_AGENT: &str = "SolanaCopyTradingEngine-PaperSimulation/1.0";
#[derive(Debug, Error)]
pub enum MarketDataError {
 #[error("Timeout must be positive")] InvalidTimeout,
 #[error("Market data request failed")] Request,
 #[error("Invalid token mint")] InvalidMint,
 #[error("Invalid market snapshot: {0}")] Price(#[from] PnlError),
 #[error("RPC address validation failed")] Address(#[from] SolanaError),
}
pub async fn fetch_market_price(mint: &str, timeout_seconds: u64) -> Result<Option<MarketPrice>, MarketDataError> {
 if timeout_seconds == 0 { return Err(MarketDataError::InvalidTimeout); }
 if !is_valid_public_key(mint) { return Ok(None); }
 let client=reqwest::Client::builder().timeout(std::time::Duration::from_secs(timeout_seconds)).user_agent(USER_AGENT).build().map_err(|_|MarketDataError::Request)?;
 let response=match client.get(format!("{URL}{mint}")).header(reqwest::header::ACCEPT,"application/json").send().await {
   Ok(r) if r.status().is_success()=>r, _=>return Ok(None)
 };
 let bytes=match response.bytes().await {Ok(b) if b.len()<=1_000_000=>b,_=>return Ok(None)};
 let payload:Value=match serde_json::from_slice(&bytes){Ok(v)=>v,Err(_)=>return Ok(None)};
 let pairs=match payload.as_array(){Some(v)=>v,None=>return Ok(None)};
 let mut candidates:Vec<(Decimal,Decimal)>=Vec::new();
 for pair in pairs {
   if pair.get("chainId").and_then(Value::as_str)!=Some("solana") {continue;}
   if pair.pointer("/baseToken/address").and_then(Value::as_str)!=Some(mint){continue;}
   let price=pair.get("priceUsd").and_then(Value::as_str).and_then(|s|s.parse::<Decimal>().ok());
   let liquidity=pair.pointer("/liquidity/usd").and_then(|v|v.as_f64()).and_then(|v|Decimal::try_from(v).ok())
     .or_else(||pair.pointer("/liquidity/usd").and_then(Value::as_str).and_then(|s|s.parse::<Decimal>().ok()));
   if let (Some(p),Some(l))=(price,liquidity) {if p>Decimal::ZERO && l>=Decimal::ZERO {candidates.push((l,p));}}
 }
 let Some((liquidity,price))=candidates.into_iter().max_by(|a,b|a.0.cmp(&b.0)) else{return Ok(None)};
 let now=Utc::now().to_rfc3339();
 Ok(Some(MarketPrice::new(mint.to_owned(),price,"USD".into(),"dexscreener_public_api".into(),now.clone(),Some(liquidity),"dexscreener_public_api".into(),Some(now))?))
}
