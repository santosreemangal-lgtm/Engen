//! Conservative source-wallet copy orchestration using virtual paper orders only.

use std::str::FromStr;
use std::time::{Duration, Instant};

use bigdecimal::BigDecimal;
use chrono::{SecondsFormat, Utc};
use num_traits::Zero;
use serde_json::{json, Value};
use thiserror::Error;

use crate::latency::LatencyRecorder;
use crate::market_data::fetch_market_price;
use crate::paper_simulator::{
    CreateOrderRequest, ExecutionPolicy, PaperSimulator, PaperSimulatorError,
};
use crate::pnl::MarketPrice;
use crate::source_freshness::source_block_time_ok;
use crate::transaction_analysis::WRAPPED_SOL_MINT;

const USDC_MINT: &str = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v";
const USDT_MINT: &str = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB";
const MAX_BUY_USD: &str = "1000000";

#[derive(Debug, Error)]
pub enum PaperCopyError {
    #[error("Invalid paper-copy input: {0}")]
    InvalidInput(String),
    #[error("Paper simulator operation failed.")]
    Simulator(#[from] PaperSimulatorError),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AllowedSides {
    Buy,
    Sell,
    Both,
    None,
}

impl AllowedSides {
    pub fn parse(value: &str) -> Result<Self, PaperCopyError> {
        match value.to_ascii_uppercase().as_str() {
            "BUY" => Ok(Self::Buy),
            "SELL" => Ok(Self::Sell),
            "BOTH" => Ok(Self::Both),
            "NONE" => Ok(Self::None),
            _ => Err(PaperCopyError::InvalidInput(
                "allowed_sides must be BUY, SELL, BOTH, or NONE.".to_string(),
            )),
        }
    }

    fn allows(self, side: &str) -> bool {
        matches!(
            (self, side),
            (Self::Both, "BUY" | "SELL") | (Self::Buy, "BUY") | (Self::Sell, "SELL")
        )
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SizingMode {
    SourcePercent,
    FixedUsd,
    MirrorAmount,
}

impl SizingMode {
    pub fn parse(value: &str) -> Result<Self, PaperCopyError> {
        match value {
            "SOURCE_PERCENT" | "FIXED_PERCENTAGE" => Ok(Self::SourcePercent),
            "FIXED_USD" | "FIXED_AMOUNT" => Ok(Self::FixedUsd),
            "MIRROR_AMOUNT" | "MIRROR_BUY_SELL_AMOUNT" => Ok(Self::MirrorAmount),
            _ => Err(PaperCopyError::InvalidInput(
                "sizing_mode must be SOURCE_PERCENT, FIXED_USD, or MIRROR_AMOUNT.".to_string(),
            )),
        }
    }

    fn as_str(self) -> &'static str {
        match self {
            Self::SourcePercent => "SOURCE_PERCENT",
            Self::FixedUsd => "FIXED_USD",
            Self::MirrorAmount => "MIRROR_AMOUNT",
        }
    }
}

#[derive(Debug, Clone)]
pub struct CopySettings {
    pub sizing_mode: SizingMode,
    pub sizing_value: String,
    pub max_buy_usd: Option<String>,
}

impl Default for CopySettings {
    fn default() -> Self {
        Self {
            sizing_mode: SizingMode::SourcePercent,
            sizing_value: "5".to_string(),
            max_buy_usd: Some("10".to_string()),
        }
    }
}

impl CopySettings {
    pub fn from_json(settings: Option<&Value>) -> Result<Self, PaperCopyError> {
        let Some(settings) = settings.filter(|value| value.is_object()) else {
            return Ok(Self::default());
        };
        let mode = settings
            .get("sizing_mode")
            .and_then(Value::as_str)
            .unwrap_or("SOURCE_PERCENT");
        let sizing_mode = SizingMode::parse(mode)?;
        let default_value = if sizing_mode == SizingMode::SourcePercent {
            "5"
        } else {
            "10"
        };
        let sizing_value = settings
            .get("sizing_value")
            .and_then(number_to_string)
            .unwrap_or_else(|| default_value.to_string());
        let max_buy_usd = match settings.get("max_buy_usd") {
            None => Some("10".to_string()),
            Some(Value::Null) => None,
            Some(Value::String(value)) if value.trim().is_empty() => None,
            Some(value) => Some(number_to_string(value).ok_or_else(|| {
                PaperCopyError::InvalidInput(
                    "Maximum BUY amount must be a valid number.".to_string(),
                )
            })?),
        };
        let parsed = Self {
            sizing_mode,
            sizing_value,
            max_buy_usd,
        };
        parsed.validate()?;
        Ok(parsed)
    }

    fn validate(&self) -> Result<(), PaperCopyError> {
        let sizing = decimal(&self.sizing_value, "Sizing value")?;
        if sizing <= 0 {
            return Err(PaperCopyError::InvalidInput(
                "sizing_value must be a positive finite number.".to_string(),
            ));
        }
        if self.sizing_mode == SizingMode::SourcePercent && sizing >= 100 {
            return Err(PaperCopyError::InvalidInput(
                "Source percentage must be greater than 0 and less than 100.".to_string(),
            ));
        }
        if self.sizing_mode == SizingMode::FixedUsd && sizing > decimal(MAX_BUY_USD, "Maximum")? {
            return Err(PaperCopyError::InvalidInput(
                "Fixed USD paper size exceeds the safety limit.".to_string(),
            ));
        }
        if let Some(maximum) = &self.max_buy_usd {
            let maximum = decimal(maximum, "Maximum BUY amount")?;
            if maximum <= 0 || maximum > decimal(MAX_BUY_USD, "Maximum")? {
                return Err(PaperCopyError::InvalidInput(
                    "Maximum BUY amount must be positive and at most $1,000,000.".to_string(),
                ));
            }
        }
        Ok(())
    }
}

#[derive(Debug, Clone)]
struct AggregateChange {
    decimals: u32,
    delta: BigDecimal,
    pre_amount: Option<BigDecimal>,
    post_amount: Option<BigDecimal>,
}

fn number_to_string(value: &Value) -> Option<String> {
    value
        .as_str()
        .map(str::to_string)
        .or_else(|| value.as_number().map(ToString::to_string))
}

fn decimal(value: &str, field: &str) -> Result<BigDecimal, PaperCopyError> {
    BigDecimal::from_str(value.trim())
        .map_err(|_| PaperCopyError::InvalidInput(format!("{field} must be a valid decimal.")))
}

fn raw_integer(value: &Value) -> Option<BigDecimal> {
    let raw = value.as_str()?;
    let digits = raw.strip_prefix('-').unwrap_or(raw);
    if digits.is_empty() || !digits.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    BigDecimal::from_str(raw).ok()
}

fn aggregate_wallet_mint_changes(
    changes: &[Value],
    wallet_address: &str,
    mint: &str,
    native_sol: bool,
) -> Option<AggregateChange> {
    let matches = changes.iter().filter(|item| {
        item.is_object()
            && item.get("owner").and_then(Value::as_str) == Some(wallet_address)
            && item.get("mint").and_then(Value::as_str) == Some(mint)
            && if native_sol {
                item.get("source").and_then(Value::as_str) == Some("native_sol")
            } else {
                item.get("source").and_then(Value::as_str) != Some("native_sol")
            }
    });

    let mut decimals = None;
    let mut delta = BigDecimal::zero();
    let mut pre_amount = BigDecimal::zero();
    let mut post_amount = BigDecimal::zero();
    let mut pre_complete = true;
    let mut post_complete = true;
    let mut count = 0usize;

    for item in matches {
        let current_decimals = item.get("decimals").and_then(Value::as_u64)?;
        if current_decimals > 30 || decimals.is_some_and(|value| value != current_decimals) {
            return None;
        }
        decimals = Some(current_decimals);
        delta += raw_integer(item.get("delta_amount")?)?;
        match raw_integer(item.get("pre_amount").unwrap_or(&Value::Null)) {
            Some(value) => pre_amount += value,
            None => pre_complete = false,
        }
        match raw_integer(item.get("post_amount").unwrap_or(&Value::Null)) {
            Some(value) => post_amount += value,
            None => post_complete = false,
        }
        count += 1;
    }
    if count == 0 {
        return None;
    }
    Some(AggregateChange {
        decimals: u32::try_from(decimals?).ok()?,
        delta,
        pre_amount: pre_complete.then_some(pre_amount),
        post_amount: post_complete.then_some(post_amount),
    })
}

fn token_amount(change: &AggregateChange) -> BigDecimal {
    let factor = BigDecimal::from_str(&format!("1{}", "0".repeat(change.decimals as usize)))
        .unwrap_or_else(|_| BigDecimal::from(1));
    &change.delta / factor
}

fn raw_to_string(value: &BigDecimal) -> String {
    value.normalized().to_string()
}

fn result_status(result: &Value) -> &str {
    result
        .get("status")
        .and_then(Value::as_str)
        .unwrap_or("UNKNOWN")
}

fn outcome(
    order_id: Option<&str>,
    signal_id: Option<&str>,
    status: &str,
    reason: Option<&str>,
    elapsed_ms: f64,
) -> Value {
    json!({
        "order_id":order_id,
        "signal_id":signal_id,
        "status":status,
        "reason":reason,
        "elapsed_ms":(elapsed_ms*1000.0).round()/1000.0
    })
}

/// Queue deduplicated paper orders from fully verified monitored-wallet swap observations.
pub fn queue_paper_orders(
    wallet_address: &str,
    results: &[Value],
    simulator: &mut PaperSimulator,
    allowed_sides: AllowedSides,
    settings: &CopySettings,
    latency_recorder: Option<&LatencyRecorder>,
) -> Result<Vec<Value>, PaperCopyError> {
    queue_paper_orders_at(
        wallet_address,
        results,
        simulator,
        allowed_sides,
        settings,
        latency_recorder,
        Utc::now().timestamp(),
    )
}

/// Deterministic clock-injected queue entrypoint used by tests and controlled replay.
pub fn queue_paper_orders_at(
    wallet_address: &str,
    results: &[Value],
    simulator: &mut PaperSimulator,
    allowed_sides: AllowedSides,
    settings: &CopySettings,
    latency_recorder: Option<&LatencyRecorder>,
    now_ts: i64,
) -> Result<Vec<Value>, PaperCopyError> {
    if wallet_address.trim().is_empty() {
        return Err(PaperCopyError::InvalidInput(
            "A monitored public wallet address is required.".to_string(),
        ));
    }
    settings.validate()?;
    let started = Instant::now();
    let mut queued = Vec::new();

    for result in results {
        if !result.is_object() {
            continue;
        }
        if result
            .get("source_lane")
            .and_then(Value::as_str)
            .is_some_and(|lane| lane != "fresh")
            || result.get("paper_copy_eligible").and_then(Value::as_bool) == Some(false)
        {
            continue;
        }
        let Some(signature) = result.get("signature").and_then(Value::as_str) else {
            continue;
        };
        let observation = result.get("signature_observation");
        let block_time = observation
            .and_then(|value| value.get("block_time"))
            .and_then(Value::as_i64);
        if !source_block_time_ok(block_time, now_ts) {
            continue;
        }
        let signal = result.get("trade_signal");
        if observation
            .and_then(|value| value.get("confirmation_status"))
            .and_then(Value::as_str)
            .is_none_or(|status| !matches!(status, "confirmed" | "finalized"))
            || result.get("status").and_then(Value::as_str) != Some("success")
            || result.get("metadata_complete").and_then(Value::as_bool) != Some(true)
            || !result
                .get("signers")
                .and_then(Value::as_array)
                .is_some_and(|signers| {
                    signers
                        .iter()
                        .any(|signer| signer.as_str() == Some(wallet_address))
                })
            || !signal.is_some_and(Value::is_object)
        {
            continue;
        }
        let signal = signal.unwrap();
        let classification = signal.get("classification").and_then(Value::as_str);
        let side = match classification {
            Some("BUY_CANDIDATE") => "BUY",
            Some("SELL_CANDIDATE") => "SELL",
            _ => continue,
        };
        if !allowed_sides.allows(side) {
            continue;
        }

        let Some(asset_mints) = signal.get("asset_mints").and_then(Value::as_array) else {
            continue;
        };
        let Some(quote_mints) = signal.get("quote_mints").and_then(Value::as_array) else {
            continue;
        };
        let Some(changes) = result
            .get("token_balance_changes")
            .and_then(Value::as_array)
        else {
            continue;
        };
        if asset_mints.len() != 1 || quote_mints.len() != 1 {
            continue;
        }
        let Some(asset_mint) = asset_mints[0].as_str() else {
            continue;
        };
        let Some(quote_mint) = quote_mints[0].as_str() else {
            continue;
        };
        let Some(asset_change) =
            aggregate_wallet_mint_changes(changes, wallet_address, asset_mint, false)
        else {
            continue;
        };
        let quote_change = if quote_mint == WRAPPED_SOL_MINT {
            aggregate_wallet_mint_changes(changes, wallet_address, quote_mint, true).or_else(|| {
                aggregate_wallet_mint_changes(changes, wallet_address, quote_mint, false)
            })
        } else {
            aggregate_wallet_mint_changes(changes, wallet_address, quote_mint, false)
        };
        let Some(quote_change) = quote_change else {
            continue;
        };
        let asset_amount = token_amount(&asset_change);
        let quote_amount = token_amount(&quote_change);
        if (side == "BUY" && (asset_amount <= 0 || quote_amount >= 0))
            || (side == "SELL" && (asset_amount >= 0 || quote_amount <= 0))
        {
            continue;
        }

        let source_quantity = asset_amount.abs();
        let mut target_notional_usd: Option<BigDecimal> = None;
        let mut source_before: Option<BigDecimal> = None;
        let order_quantity;
        if side == "BUY" {
            let virtual_usd = simulator
                .balances()
                .get("USD")
                .cloned()
                .unwrap_or_else(BigDecimal::zero);
            if virtual_usd <= 0 {
                continue;
            }
            let sizing = decimal(&settings.sizing_value, "Sizing value")?;
            let mut target = match settings.sizing_mode {
                SizingMode::SourcePercent => &virtual_usd * sizing / BigDecimal::from(100),
                SizingMode::FixedUsd => sizing,
                SizingMode::MirrorAmount => {
                    if !matches!(quote_mint, USDC_MINT | USDT_MINT) {
                        continue;
                    }
                    quote_amount.abs()
                }
            };
            if let Some(maximum) = &settings.max_buy_usd {
                target = target.min(decimal(maximum, "Maximum BUY amount")?);
            }
            target = target.min(virtual_usd);
            if target <= 0 {
                continue;
            }
            target_notional_usd = Some(target);
            order_quantity = "1".to_string();
        } else {
            let Some(pre_source_raw) = asset_change.pre_amount.clone() else {
                continue;
            };
            let pre_source = &pre_source_raw
                / BigDecimal::from_str(&format!("1{}", "0".repeat(asset_change.decimals as usize)))
                    .unwrap_or_else(|_| BigDecimal::from(1));
            if pre_source <= 0 || source_quantity > pre_source {
                continue;
            }
            let fraction = &source_quantity / &pre_source;
            if fraction <= 0 || fraction > 1 {
                continue;
            }
            source_before = Some(pre_source);
            order_quantity = "1".to_string();
        }

        let signal_id = format!("wallet-copy:{wallet_address}:{signature}");
        let created_at = Utc::now().to_rfc3339_opts(SecondsFormat::Millis, true);
        let sell_fraction = if side == "SELL" {
            source_before
                .as_ref()
                .map(|pre| raw_to_string(&(&source_quantity / pre)))
        } else {
            None
        };
        let order = simulator.create_order(CreateOrderRequest {
            signal_id: &signal_id,
            side,
            asset_mint,
            quantity: &order_quantity,
            quote_mint: "USD",
            created_at: &created_at,
            details: Some(json!({
                "sizing_mode": if side == "BUY" {settings.sizing_mode.as_str()} else {"SOURCE_POSITION_REDUCTION"},
                "sizing_value": if side == "BUY" {Some(settings.sizing_value.clone())} else {None},
                "target_notional_usd": target_notional_usd.as_ref().map(raw_to_string),
                "max_buy_usd": settings.max_buy_usd,
                "source_asset_quantity": raw_to_string(&source_quantity),
                "source_asset_pre_amount": asset_change.pre_amount.as_ref().map(raw_to_string),
                "source_asset_post_amount": asset_change.post_amount.as_ref().map(raw_to_string),
                "source_sell_fraction": sell_fraction,
                "origin":"AUTOMATIC_WALLET_COPY",
                "source_quote_mint":quote_mint,
                "source_quote_amount":raw_to_string(&quote_amount.abs()),
                "source_wallet":wallet_address,
                "source_signature":signature,
                "source_slot":observation.and_then(|value|value.get("slot")).cloned().unwrap_or(Value::Null),
                "source_block_time":observation.and_then(|value|value.get("block_time")).cloned().unwrap_or(Value::Null),
                "source_classification":classification,
                "execution_state":"AWAITING_VERIFIED_MARKET_DATA",
                "real_trading":false,
                "transaction_broadcast":false
            })),
        })?;
        if result_status(&order) == "DUPLICATE" {
            continue;
        }
        queued.push(json!({
            "signature":signature,
            "order_id":order.get("order_id"),
            "side":side,
            "asset_mint":asset_mint,
            "quantity":order_quantity,
            "sizing_mode":if side == "BUY" {settings.sizing_mode.as_str()} else {"SOURCE_POSITION_REDUCTION"},
            "sizing_value":if side == "BUY" {Some(settings.sizing_value.clone())} else {None},
            "target_notional_usd":target_notional_usd.as_ref().map(raw_to_string),
            "source_quote_mint":quote_mint,
            "status":order.get("status"),
            "execution_state":"AWAITING_VERIFIED_MARKET_DATA"
        }));
    }

    if let Some(recorder) = latency_recorder {
        let _ = recorder.record(
            "paper.copy_queue_batch",
            started.elapsed().as_secs_f64() * 1_000.0,
        );
    }
    Ok(queued)
}

/// Execute queued automatic-copy paper orders with an injectable read-only market snapshot source.
pub fn execute_pending_paper_orders_with<F>(
    simulator: &mut PaperSimulator,
    wallet_address: Option<&str>,
    price_provider: F,
    latency_recorder: Option<&LatencyRecorder>,
) -> Vec<Value>
where
    F: FnMut(&str) -> Option<MarketPrice>,
{
    execute_pending_paper_orders_with_at(
        simulator,
        wallet_address,
        price_provider,
        latency_recorder,
        Utc::now().timestamp(),
    )
}

/// Deterministic clock-injected execution entrypoint for source-age boundary tests.
pub fn execute_pending_paper_orders_with_at<F>(
    simulator: &mut PaperSimulator,
    wallet_address: Option<&str>,
    mut price_provider: F,
    latency_recorder: Option<&LatencyRecorder>,
    now_ts: i64,
) -> Vec<Value>
where
    F: FnMut(&str) -> Option<MarketPrice>,
{
    let orders: Vec<_> = simulator.orders().values().cloned().collect();
    let mut outcomes = Vec::new();
    for order in orders {
        let details = &order.details;
        if order.status != "CREATED"
            || details.get("origin").and_then(Value::as_str) != Some("AUTOMATIC_WALLET_COPY")
            || wallet_address.is_some_and(|wallet| {
                details.get("source_wallet").and_then(Value::as_str) != Some(wallet)
            })
        {
            continue;
        }
        let started = Instant::now();
        let signal_id = Some(order.signal_id.as_str());
        let source_block_time = details.get("source_block_time").and_then(Value::as_i64);
        if !source_block_time_ok(source_block_time, now_ts) {
            let reason = "Source transaction became stale before paper-fill commit.";
            let _ = simulator.cancel_order_with_reason(&order.order_id, reason);
            outcomes.push(outcome(
                Some(&order.order_id),
                signal_id,
                "SOURCE_STALE",
                Some(reason),
                started.elapsed().as_secs_f64() * 1_000.0,
            ));
            continue;
        }
        if order.side == "SELL" {
            let fraction = details
                .get("source_sell_fraction")
                .and_then(Value::as_str)
                .and_then(|raw| BigDecimal::from_str(raw).ok());
            let owned = simulator
                .balances()
                .get(&order.asset_mint)
                .cloned()
                .unwrap_or_else(BigDecimal::zero);
            let Some(fraction) = fraction.filter(|value| *value > 0 && *value <= 1) else {
                outcomes.push(outcome(
                    Some(&order.order_id),
                    signal_id,
                    "WAITING_FOR_POSITION",
                    Some("Source sell fraction is unavailable or invalid; no paper sell was executed."),
                    started.elapsed().as_secs_f64() * 1_000.0,
                ));
                continue;
            };
            if owned <= 0 {
                outcomes.push(outcome(
                    Some(&order.order_id),
                    signal_id,
                    "WAITING_FOR_POSITION",
                    Some("No copied token position is currently held; sell remains queued."),
                    started.elapsed().as_secs_f64() * 1_000.0,
                ));
                continue;
            }
            let quantity = owned * fraction;
            if simulator
                .set_order_quantity(&order.order_id, &raw_to_string(&quantity))
                .is_err()
            {
                outcomes.push(outcome(
                    Some(&order.order_id),
                    signal_id,
                    "EXECUTION_ERROR",
                    Some("Paper order quantity could not be updated."),
                    started.elapsed().as_secs_f64() * 1_000.0,
                ));
                continue;
            }
        }

        let market_price = price_provider(&order.asset_mint);
        let Some(market_price) = market_price else {
            outcomes.push(outcome(
                Some(&order.order_id),
                signal_id,
                "WAITING_FOR_MARKET_DATA",
                Some("No usable public USD price/liquidity snapshot; order remains queued."),
                started.elapsed().as_secs_f64() * 1_000.0,
            ));
            continue;
        };
        let now = Utc::now().to_rfc3339_opts(SecondsFormat::Millis, true);
        let result = simulator.execute_order(
            &order.order_id,
            Some(&market_price),
            &now,
            &ExecutionPolicy::default(),
            true,
        );
        match result {
            Ok(value) => {
                let status = result_status(&value);
                let reason = value.get("reason").and_then(Value::as_str);
                let elapsed = started.elapsed().as_secs_f64() * 1_000.0;
                outcomes.push(outcome(
                    Some(&order.order_id),
                    signal_id,
                    status,
                    reason,
                    elapsed,
                ));
                if let Some(recorder) = latency_recorder {
                    let _ = recorder.record("paper.copy_order_total", elapsed);
                }
            }
            Err(_) => outcomes.push(outcome(
                Some(&order.order_id),
                signal_id,
                "EXECUTION_ERROR",
                Some("Paper execution failed; no real transaction was sent."),
                started.elapsed().as_secs_f64() * 1_000.0,
            )),
        }
    }
    outcomes
}

/// Execute queued orders using DEX Screener public snapshots, with no live transaction path.
pub fn execute_pending_paper_orders(
    simulator: &mut PaperSimulator,
    wallet_address: Option<&str>,
    timeout: Duration,
    latency_recorder: Option<&LatencyRecorder>,
) -> Vec<Value> {
    execute_pending_paper_orders_with(
        simulator,
        wallet_address,
        |mint| fetch_market_price(mint, timeout).ok().flatten(),
        latency_recorder,
    )
}

pub fn get_pending_copy_status(simulator: &PaperSimulator, wallet_address: Option<&str>) -> Value {
    let pending: Vec<Value> = simulator
        .orders()
        .values()
        .filter(|order| {
            order.status == "CREATED"
                && order.details.get("origin").and_then(Value::as_str)
                    == Some("AUTOMATIC_WALLET_COPY")
                && wallet_address.is_none_or(|wallet| {
                    order.details.get("source_wallet").and_then(Value::as_str) == Some(wallet)
                })
        })
        .map(|order| {
            json!({
                "order_id":order.order_id,
                "signal_id":order.signal_id,
                "side":order.side,
                "asset_mint":order.asset_mint,
                "source_wallet":order.details.get("source_wallet"),
                "source_signature":order.details.get("source_signature"),
                "execution_state":order.details.get("execution_state"),
                "created_at":order.created_at
            })
        })
        .collect();
    json!({
        "pending_count":pending.len(),
        "pending_orders":pending,
        "real_trading":false,
        "transaction_broadcast":false
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::collections::BTreeMap;

    const WALLET: &str = "11111111111111111111111111111111";
    const ASSET: &str = "So11111111111111111111111111111111111111113";
    const USDC: &str = USDC_MINT;

    // Existing fixtures use a fixed timestamp; inject the matching clock rather than
    // weakening the production 60-second source-freshness guard.
    fn queue_paper_orders(
        wallet_address: &str,
        results: &[Value],
        simulator: &mut PaperSimulator,
        allowed_sides: AllowedSides,
        settings: &CopySettings,
        latency_recorder: Option<&LatencyRecorder>,
    ) -> Result<Vec<Value>, PaperCopyError> {
        super::queue_paper_orders_at(
            wallet_address,
            results,
            simulator,
            allowed_sides,
            settings,
            latency_recorder,
            1_760_000_001,
        )
    }

    fn execute_pending_paper_orders_with<F>(
        simulator: &mut PaperSimulator,
        wallet_address: Option<&str>,
        price_provider: F,
        latency_recorder: Option<&LatencyRecorder>,
    ) -> Vec<Value>
    where
        F: FnMut(&str) -> Option<MarketPrice>,
    {
        super::execute_pending_paper_orders_with_at(
            simulator,
            wallet_address,
            price_provider,
            latency_recorder,
            1_760_000_001,
        )
    }

    fn simulator() -> PaperSimulator {
        let mut balances = BTreeMap::new();
        balances.insert("USD".to_string(), "100".to_string());
        balances.insert(ASSET.to_string(), "10".to_string());
        PaperSimulator::in_memory(&balances).unwrap()
    }

    fn buy_signal(signature: &str) -> Value {
        json!({
            "signature":signature,
            "signature_observation":{"confirmation_status":"confirmed","slot":100,"block_time":1760000000},
            "status":"success",
            "metadata_complete":true,
            "signers":[WALLET],
            "trade_signal":{"classification":"BUY_CANDIDATE","asset_mints":[ASSET],"quote_mints":[USDC]},
            "token_balance_changes":[
                {"owner":WALLET,"mint":ASSET,"decimals":6,"delta_amount":"1000000","pre_amount":"0","post_amount":"1000000"},
                {"owner":WALLET,"mint":USDC,"decimals":6,"delta_amount":"-2000000","pre_amount":"10000000","post_amount":"8000000"}
            ]
        })
    }

    fn sell_signal(signature: &str) -> Value {
        json!({
            "signature":signature,
            "signature_observation":{"confirmation_status":"finalized","slot":101,"block_time":1760000001},
            "status":"success",
            "metadata_complete":true,
            "signers":[WALLET],
            "trade_signal":{"classification":"SELL_CANDIDATE","asset_mints":[ASSET],"quote_mints":[USDC]},
            "token_balance_changes":[
                {"owner":WALLET,"mint":ASSET,"decimals":6,"delta_amount":"-5000000","pre_amount":"10000000","post_amount":"5000000"},
                {"owner":WALLET,"mint":USDC,"decimals":6,"delta_amount":"1000000","pre_amount":"8000000","post_amount":"9000000"}
            ]
        })
    }

    #[test]
    fn stale_source_transactions_are_never_queued_for_paper_execution() {
        let mut simulator = simulator();
        let mut stale = buy_signal(&"3".repeat(64));
        stale["signature_observation"]["block_time"] = json!(1_759_999_900);
        let queued = super::queue_paper_orders_at(
            WALLET,
            &[stale],
            &mut simulator,
            AllowedSides::Both,
            &CopySettings::default(),
            None,
            1_760_000_001,
        )
        .unwrap();
        assert!(queued.is_empty());
        assert!(simulator.orders().is_empty());
    }

    #[test]
    fn history_lane_results_are_never_queued_for_paper_execution() {
        let mut simulator = simulator();
        let mut history = buy_signal(&"8".repeat(64));
        history["source_lane"] = json!("history");
        history["paper_copy_eligible"] = json!(false);
        let queued = queue_paper_orders(
            WALLET,
            &[history],
            &mut simulator,
            AllowedSides::Both,
            &CopySettings::default(),
            None,
        )
        .unwrap();
        assert!(queued.is_empty());
        assert!(simulator.orders().is_empty());
    }

    #[test]
    fn source_age_is_rechecked_before_fill_commit_and_stale_order_is_retained_cancelled() {
        let mut simulator = simulator();
        let result = vec![buy_signal(&"4".repeat(64))];
        let queued = queue_paper_orders(
            WALLET,
            &result,
            &mut simulator,
            AllowedSides::Both,
            &CopySettings::default(),
            None,
        )
        .unwrap();
        let order_id = queued[0]["order_id"].as_str().unwrap().to_string();
        let outcomes = super::execute_pending_paper_orders_with_at(
            &mut simulator,
            Some(WALLET),
            |_mint| panic!("stale source must be rejected before market-data lookup"),
            None,
            1_760_000_061,
        );
        assert_eq!(outcomes[0]["status"], "SOURCE_STALE");
        let order = simulator.orders().get(&order_id).unwrap();
        assert_eq!(order.status, "CANCELLED");
        assert!(order.reason.as_deref().unwrap().contains("stale"));
        assert_eq!(simulator.fills().len(), 0);
    }

    #[test]
    fn buy_queue_uses_virtual_percentage_and_is_idempotent() {
        let mut simulator = simulator();
        let result = vec![buy_signal("1".repeat(64).as_str())];
        let settings = CopySettings::default();
        let queued = queue_paper_orders(
            WALLET,
            &result,
            &mut simulator,
            AllowedSides::Both,
            &settings,
            None,
        )
        .unwrap();
        assert_eq!(queued.len(), 1);
        assert_eq!(queued[0]["side"], "BUY");
        let order = simulator.orders().values().next().unwrap();
        assert_eq!(order.details["target_notional_usd"], "5");
        assert_eq!(order.details["real_trading"], false);
        let duplicate = queue_paper_orders(
            WALLET,
            &result,
            &mut simulator,
            AllowedSides::Both,
            &settings,
            None,
        )
        .unwrap();
        assert!(duplicate.is_empty());
    }

    #[test]
    fn sell_queue_uses_source_position_reduction_not_buy_sizing() {
        let mut simulator = simulator();
        let result = vec![sell_signal("2".repeat(64).as_str())];
        let queued = queue_paper_orders(
            WALLET,
            &result,
            &mut simulator,
            AllowedSides::Sell,
            &CopySettings::default(),
            None,
        )
        .unwrap();
        assert_eq!(queued.len(), 1);
        let order = simulator.orders().values().next().unwrap();
        assert_eq!(order.details["source_sell_fraction"], "0.5");
        assert_eq!(order.details["sizing_mode"], "SOURCE_POSITION_REDUCTION");
    }

    #[test]
    fn incomplete_unconfirmed_or_wrong_signer_signals_are_not_queued() {
        let mut simulator = simulator();
        let mut incomplete = buy_signal("3".repeat(64).as_str());
        incomplete["metadata_complete"] = json!(false);
        let mut unconfirmed = buy_signal("4".repeat(64).as_str());
        unconfirmed["signature_observation"]["confirmation_status"] = json!("processed");
        let mut wrong_signer = buy_signal("5".repeat(64).as_str());
        wrong_signer["signers"] = json!(["DifferentWallet"]);
        let rows = vec![incomplete, unconfirmed, wrong_signer];
        let queued = queue_paper_orders(
            WALLET,
            &rows,
            &mut simulator,
            AllowedSides::Both,
            &CopySettings::default(),
            None,
        )
        .unwrap();
        assert!(queued.is_empty());
    }

    #[test]
    fn sell_execution_uses_fraction_of_current_paper_position() {
        let mut simulator = simulator();
        let result = vec![sell_signal("6".repeat(64).as_str())];
        queue_paper_orders(
            WALLET,
            &result,
            &mut simulator,
            AllowedSides::Both,
            &CopySettings::default(),
            None,
        )
        .unwrap();
        let now = Utc::now().to_rfc3339_opts(SecondsFormat::Millis, true);
        let price = MarketPrice::new(
            ASSET,
            "1",
            "USD",
            "test-fixture",
            now.clone(),
            Some("25000"),
            Some("test-fixture".to_string()),
            Some(now),
        )
        .unwrap();
        let outcomes = execute_pending_paper_orders_with(
            &mut simulator,
            Some(WALLET),
            |_mint| Some(price.clone()),
            None,
        );
        assert_eq!(outcomes.len(), 1);
        assert_eq!(outcomes[0]["status"], "FILLED");
        assert_eq!(simulator.fills()[0].quantity, "5");
        assert_eq!(simulator.balances()[ASSET].to_string(), "5");
    }

    #[test]
    fn pending_status_is_observable_and_never_claims_live_trading() {
        let mut simulator = simulator();
        queue_paper_orders(
            WALLET,
            &[buy_signal("7".repeat(64).as_str())],
            &mut simulator,
            AllowedSides::Both,
            &CopySettings::default(),
            None,
        )
        .unwrap();
        let status = get_pending_copy_status(&simulator, Some(WALLET));
        assert_eq!(status["pending_count"], 1);
        assert_eq!(status["real_trading"], false);
        assert_eq!(status["transaction_broadcast"], false);
    }

    #[test]
    fn validates_copy_settings_and_allowed_sides() {
        assert!(AllowedSides::parse("UNKNOWN").is_err());
        assert!(CopySettings::from_json(Some(&json!({
            "sizing_mode":"SOURCE_PERCENT","sizing_value":"100"
        })))
        .is_err());
        assert!(CopySettings::from_json(Some(&json!({
            "sizing_mode":"FIXED_USD","sizing_value":"10","max_buy_usd":"-1"
        })))
        .is_err());
    }
}
