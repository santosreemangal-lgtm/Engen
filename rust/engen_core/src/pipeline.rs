//! Monitor observation → classify → paper queue → paper fill → SQLite dedupe.
//! No signing and no broadcast.

use crate::monitor_service::MonitorService;
use crate::paper_copy::MonitorCandidate;
use crate::paper_simulator::{MarketPrice, OrderSide, PaperSimulator};
use crate::parser::{classify_balance_changes, USDC};
use crate::profile_store::ProfileStore;
use crate::signature_store::SignatureStore;
use crate::Result;
use rust_decimal::Decimal;
use serde_json::{json, Value};
use std::str::FromStr;

pub fn process_observation(
    monitor: &mut MonitorService,
    sim: &mut PaperSimulator,
    signatures: &SignatureStore,
    profiles: &ProfileStore,
    input: &Value,
) -> Result<Value> {
    let wallet = input.get("wallet").and_then(|v| v.as_str()).unwrap_or("");
    let signature = input.get("signature").and_then(|v| v.as_str()).unwrap_or("");
    if wallet.is_empty() || signature.is_empty() {
        return Ok(json!({
            "status": "REJECTED",
            "reason": "wallet and signature are required",
            "real_trading": false,
            "transaction_broadcast": false
        }));
    }
    if signatures.is_processed(wallet, signature)? {
        return Ok(json!({
            "status": "DUPLICATE",
            "signature": signature,
            "real_trading": false,
            "transaction_broadcast": false
        }));
    }
    let changes = input
        .get("token_balance_changes")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();
    let signal = classify_balance_changes(wallet, &changes);
    let class = signal
        .get("classification")
        .and_then(|v| v.as_str())
        .unwrap_or("UNKNOWN");
    let asset = signal["asset_mints"].get(0).and_then(|v| v.as_str()).map(|s| s.to_string());
    let quote = signal["quote_mints"].get(0).and_then(|v| v.as_str()).map(|s| s.to_string());
    let _ = profiles.record_swap(signature, wallet, class, asset.as_deref(), quote.as_deref());

    if class != "BUY_CANDIDATE" && class != "SELL_CANDIDATE" {
        signatures.remember(wallet, signature)?;
        signatures.mark_processed(wallet, signature)?;
        return Ok(json!({
            "status": "IGNORED",
            "classification": class,
            "real_trading": false,
            "transaction_broadcast": false
        }));
    }

    let side = if class == "SELL_CANDIDATE" {
        OrderSide::Sell
    } else {
        OrderSide::Buy
    };
    let asset_mint = asset.unwrap_or_default();
    monitor.add_wallet(wallet);
    monitor.ingest(
        wallet,
        vec![MonitorCandidate {
            signature: signature.to_string(),
            wallet: wallet.to_string(),
            side,
            asset_mint: asset_mint.clone(),
            block_time: input.get("block_time").and_then(|v| v.as_i64()),
            confirmation_status: input
                .get("confirmation_status")
                .and_then(|v| v.as_str())
                .unwrap_or("finalized")
                .to_string(),
            metadata_complete: true,
            target_notional_usd: input
                .get("target_notional_usd")
                .and_then(|v| v.as_str())
                .map(|s| s.to_string()),
            source_sell_fraction: input
                .get("source_sell_fraction")
                .and_then(|v| v.as_str())
                .map(|s| s.to_string()),
        }],
    );

    let price = input
        .get("price_usd")
        .and_then(|v| v.as_str())
        .and_then(|s| Decimal::from_str(s).ok());
    let liquidity = input
        .get("liquidity_usd")
        .and_then(|v| v.as_str())
        .and_then(|s| Decimal::from_str(s).ok())
        .unwrap_or(Decimal::from(20_000));
    let outcomes = if let Some(price_usd) = price {
        let market = MarketPrice {
            price_usd,
            liquidity_usd: liquidity,
            age_seconds: 1,
        };
        monitor.tick_and_fill(sim, &|mint| {
            if mint == asset_mint {
                Some(market.clone())
            } else {
                None
            }
        })?
    } else {
        let queued = monitor.tick(sim)?;
        vec![json!({"status": "QUEUED", "queued": queued})]
    };
    signatures.remember(wallet, signature)?;
    signatures.mark_processed(wallet, signature)?;
    Ok(json!({
        "status": "PROCESSED",
        "classification": class,
        "outcomes": outcomes,
        "quote_hint": quote.unwrap_or_else(|| USDC.to_string()),
        "real_trading": false,
        "transaction_broadcast": false
    }))
}
