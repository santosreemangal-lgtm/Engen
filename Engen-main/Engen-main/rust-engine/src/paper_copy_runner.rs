//! Conservative source-wallet copy orchestration using virtual paper orders only.

use std::str::FromStr;
use std::time::{Duration, Instant};

use bigdecimal::rounding::RoundingMode;
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
        match value.trim().to_ascii_uppercase().as_str() {
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
    /// Optional profile-level override for global trading fee basis points.
    pub trading_fee_bps: Option<u16>,
    /// Optional fixed base-network fee override, USD per paper fill.
    pub network_fee_usd: Option<String>,
    /// Optional priority-fee estimate override, USD per paper fill.
    pub priority_fee_usd: Option<String>,
}

impl Default for CopySettings {
    fn default() -> Self {
        Self {
            sizing_mode: SizingMode::SourcePercent,
            sizing_value: "5".to_string(),
            max_buy_usd: Some("10".to_string()),
            trading_fee_bps: None,
            network_fee_usd: None,
            priority_fee_usd: None,
        }
    }
}

fn optional_fee_usd(
    settings: &Value,
    key: &str,
    label: &str,
) -> Result<Option<String>, PaperCopyError> {
    match settings.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(value) => {
            let raw = number_to_string(value).ok_or_else(|| {
                PaperCopyError::InvalidInput(format!(
                    "{key} must be a non-negative decimal no greater than 1000 USD."
                ))
            })?;
            let amount = decimal(&raw, label)?;
            if amount < 0 || amount > decimal("1000", "Fee limit")? {
                return Err(PaperCopyError::InvalidInput(format!(
                    "{key} must be a non-negative decimal no greater than 1000 USD."
                )));
            }
            Ok(Some(amount.normalized().to_plain_string()))
        }
    }
}

impl CopySettings {
    pub fn from_json(settings: Option<&Value>) -> Result<Self, PaperCopyError> {
        let Some(settings) = settings else {
            return Ok(Self::default());
        };
        if !settings.is_object() {
            return Err(PaperCopyError::InvalidInput(
                "Copy settings must be a JSON object.".to_string(),
            ));
        }
        let mode = settings
            .get("sizing_mode")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .unwrap_or("SOURCE_PERCENT");
        let sizing_mode = SizingMode::parse(mode)?;
        let default_value = if sizing_mode == SizingMode::SourcePercent {
            "5"
        } else {
            "10"
        };
        let sizing_value = match settings.get("sizing_value") {
            None | Some(Value::Null) => default_value.to_string(),
            Some(value) => number_to_string(value).ok_or_else(|| {
                PaperCopyError::InvalidInput("Sizing value must be a valid number.".to_string())
            })?,
        };
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
        let trading_fee_bps = match settings.get("trading_fee_bps") {
            None | Some(Value::Null) => None,
            Some(value) => {
                let raw = number_to_string(value).ok_or_else(|| {
                    PaperCopyError::InvalidInput(
                        "trading_fee_bps must be an integer from 0 to 10000.".to_string(),
                    )
                })?;
                let parsed = raw.parse::<u16>().map_err(|_| {
                    PaperCopyError::InvalidInput(
                        "trading_fee_bps must be an integer from 0 to 10000.".to_string(),
                    )
                })?;
                if parsed > 10_000 {
                    return Err(PaperCopyError::InvalidInput(
                        "trading_fee_bps must be an integer from 0 to 10000.".to_string(),
                    ));
                }
                Some(parsed)
            }
        };
        let network_fee_usd = optional_fee_usd(settings, "network_fee_usd", "Base network fee")?;
        let priority_fee_usd = optional_fee_usd(settings, "priority_fee_usd", "Priority fee")?;

        let parsed = Self {
            sizing_mode,
            sizing_value,
            max_buy_usd,
            trading_fee_bps,
            network_fee_usd,
            priority_fee_usd,
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
                "Fixed percentage must be greater than 0 and less than 100.".to_string(),
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
        if self.trading_fee_bps.is_some_and(|value| value > 10_000) {
            return Err(PaperCopyError::InvalidInput(
                "trading_fee_bps must be an integer from 0 to 10000.".to_string(),
            ));
        }
        let base_network_fee = self
            .network_fee_usd
            .as_deref()
            .map(|raw| decimal(raw, "Base network fee"))
            .transpose()?
            .unwrap_or_else(BigDecimal::zero);
        let priority_fee = self
            .priority_fee_usd
            .as_deref()
            .map(|raw| decimal(raw, "Priority fee"))
            .transpose()?
            .unwrap_or_else(BigDecimal::zero);
        if base_network_fee < 0
            || priority_fee < 0
            || base_network_fee + priority_fee > decimal("1000", "Fee limit")?
        {
            return Err(PaperCopyError::InvalidInput(
                "Combined network and priority fee overrides must be between 0 and 1000 USD per paper fill."
                    .to_string(),
            ));
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

fn minimum_quantity_for_decimals(decimals: u32) -> Result<String, PaperCopyError> {
    if decimals > 255 {
        return Err(PaperCopyError::InvalidInput(
            "Asset decimals must be between 0 and 255.".to_string(),
        ));
    }
    if decimals == 0 {
        return Ok("1".to_string());
    }
    Ok(format!(
        "0.{}1",
        "0".repeat(decimals.saturating_sub(1) as usize)
    ))
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

/// Queue deduplicated paper orders using the default paper fee policy.
pub fn queue_paper_orders(
    wallet_address: &str,
    results: &[Value],
    simulator: &mut PaperSimulator,
    allowed_sides: AllowedSides,
    settings: &CopySettings,
    latency_recorder: Option<&LatencyRecorder>,
) -> Result<Vec<Value>, PaperCopyError> {
    queue_paper_orders_with_policy(
        wallet_address,
        results,
        simulator,
        allowed_sides,
        settings,
        latency_recorder,
        &ExecutionPolicy::default(),
    )
}

/// Queue deduplicated paper orders and snapshot the supplied global fee policy.
/// Per-profile fee values in CopySettings override the matching global values.
pub fn queue_paper_orders_with_policy(
    wallet_address: &str,
    results: &[Value],
    simulator: &mut PaperSimulator,
    allowed_sides: AllowedSides,
    settings: &CopySettings,
    latency_recorder: Option<&LatencyRecorder>,
    execution_policy: &ExecutionPolicy,
) -> Result<Vec<Value>, PaperCopyError> {
    queue_paper_orders_at_with_policy(
        wallet_address,
        results,
        simulator,
        allowed_sides,
        settings,
        latency_recorder,
        execution_policy,
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
    queue_paper_orders_at_with_policy(
        wallet_address,
        results,
        simulator,
        allowed_sides,
        settings,
        latency_recorder,
        &ExecutionPolicy::default(),
        now_ts,
    )
}

/// Deterministic queue entrypoint that persists the resolved fee settings on every order.
#[allow(clippy::too_many_arguments)]
pub fn queue_paper_orders_at_with_policy(
    wallet_address: &str,
    results: &[Value],
    simulator: &mut PaperSimulator,
    allowed_sides: AllowedSides,
    settings: &CopySettings,
    latency_recorder: Option<&LatencyRecorder>,
    execution_policy: &ExecutionPolicy,
    now_ts: i64,
) -> Result<Vec<Value>, PaperCopyError> {
    if wallet_address.trim().is_empty() {
        return Err(PaperCopyError::InvalidInput(
            "A monitored public wallet address is required.".to_string(),
        ));
    }
    settings.validate()?;
    if execution_policy.fee_bps > 10_000 {
        return Err(PaperCopyError::InvalidInput(
            "Global trading_fee_bps must be between 0 and 10000.".to_string(),
        ));
    }
    let global_base_network_fee =
        decimal(&execution_policy.network_cost, "Global base network fee")?;
    let global_priority_fee = decimal(&execution_policy.priority_fee_usd, "Global priority fee")?;
    if global_base_network_fee < 0
        || global_priority_fee < 0
        || &global_base_network_fee + &global_priority_fee > decimal("1000", "Fee limit")?
    {
        return Err(PaperCopyError::InvalidInput(
            "Combined global network and priority fees must be between 0 and 1000 USD per paper fill."
                .to_string(),
        ));
    }
    let resolved_trading_fee_bps = settings.trading_fee_bps.unwrap_or(execution_policy.fee_bps);
    let resolved_network_fee_usd = settings
        .network_fee_usd
        .clone()
        .unwrap_or_else(|| global_base_network_fee.normalized().to_plain_string());
    let resolved_priority_fee_usd = settings
        .priority_fee_usd
        .clone()
        .unwrap_or_else(|| global_priority_fee.normalized().to_plain_string());
    let profile_fee_override = settings.trading_fee_bps.is_some()
        || settings.network_fee_usd.is_some()
        || settings.priority_fee_usd.is_some();

    let started = Instant::now();
    let mut queued = Vec::new();

    // Reserve in source chronology, not input or random-order-ID order. This is
    // especially important when several partial SELLs are discovered in one scan.
    let mut ordered_results: Vec<&Value> = results.iter().collect();
    ordered_results.sort_by(|left, right| {
        let left_slot = left
            .get("signature_observation")
            .and_then(|value| value.get("slot"))
            .and_then(Value::as_u64)
            .unwrap_or(u64::MAX);
        let right_slot = right
            .get("signature_observation")
            .and_then(|value| value.get("slot"))
            .and_then(Value::as_u64)
            .unwrap_or(u64::MAX);
        let left_time = left
            .get("signature_observation")
            .and_then(|value| value.get("block_time"))
            .and_then(Value::as_i64)
            .unwrap_or(i64::MAX);
        let right_time = right
            .get("signature_observation")
            .and_then(|value| value.get("block_time"))
            .and_then(Value::as_i64)
            .unwrap_or(i64::MAX);
        left_slot
            .cmp(&right_slot)
            .then_with(|| left_time.cmp(&right_time))
            .then_with(|| {
                left.get("signature")
                    .and_then(Value::as_str)
                    .cmp(&right.get("signature").and_then(Value::as_str))
            })
    });

    for result in ordered_results {
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
        let mut sell_reservation_quantity: Option<BigDecimal> = None;
        let order_quantity;
        if side == "BUY" {
            let virtual_usd = simulator.available_balance("USD");
            if virtual_usd <= 0 {
                continue;
            }
            let sizing = decimal(&settings.sizing_value, "Sizing value")?;
            let mut target_budget = match settings.sizing_mode {
                // Percentage is applied to the profile's current virtual USD cash,
                // not the source wallet's trade size or its portfolio balance.
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
                target_budget = target_budget.min(decimal(maximum, "Maximum BUY amount")?);
            }
            // Keep a global guard even when the profile explicitly disables its
            // per-profile maximum.
            target_budget = target_budget.min(decimal(MAX_BUY_USD, "Maximum BUY amount")?);
            if settings.sizing_mode == SizingMode::FixedUsd {
                // Fixed-dollar means the configured/capped amount, never a silent
                // partial order just because the virtual account lacks the cash.
                if target_budget > virtual_usd {
                    continue;
                }
            } else {
                target_budget = target_budget.min(virtual_usd);
            }
            if target_budget <= 0 {
                continue;
            }
            target_notional_usd = Some(target_budget);
            order_quantity = "1".to_string();
        } else {
            let (Some(pre_source_raw), Some(post_source_raw)) = (
                asset_change.pre_amount.clone(),
                asset_change.post_amount.clone(),
            ) else {
                continue;
            };
            let unit_factor =
                BigDecimal::from_str(&format!("1{}", "0".repeat(asset_change.decimals as usize)))
                    .unwrap_or_else(|_| BigDecimal::from(1));
            let pre_source = &pre_source_raw / &unit_factor;
            let post_source = &post_source_raw / &unit_factor;

            // A SELL fraction is valid only when pre - post equals the absolute
            // source token delta. Inconsistent balance snapshots are never copied.
            if pre_source <= 0 || post_source < 0 || post_source >= pre_source {
                continue;
            }
            let sold_from_balances = &pre_source - &post_source;
            if sold_from_balances != source_quantity {
                continue;
            }
            let fraction = &sold_from_balances / &pre_source;
            if fraction <= 0 || fraction > 1 {
                continue;
            }
            let paper_available = simulator.available_balance(asset_mint);
            if paper_available <= 0 {
                continue;
            }
            let quantity_step = decimal(
                &minimum_quantity_for_decimals(asset_change.decimals)?,
                "Token quantity step",
            )?;
            let requested_quantity = paper_available * &fraction;
            let whole_steps =
                (&requested_quantity / &quantity_step).with_scale_round(0, RoundingMode::Down);
            let reserved_quantity = whole_steps * &quantity_step;
            if reserved_quantity < quantity_step {
                continue;
            }
            source_before = Some(pre_source);
            order_quantity = raw_to_string(&reserved_quantity);
            sell_reservation_quantity = Some(reserved_quantity);
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
        let reservation_amount = if side == "BUY" {
            target_notional_usd.clone()
        } else {
            sell_reservation_quantity.clone()
        };
        let reservation_mint = if side == "BUY" { "USD" } else { asset_mint };

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
                "target_budget_usd": target_notional_usd.as_ref().map(raw_to_string),
                "target_budget_semantics":"ALL_IN_USD_BUDGET_INCLUDING_SLIPPAGE_FEES_AND_NETWORK_COST",
                "trading_fee_bps":resolved_trading_fee_bps,
                "network_fee_usd":resolved_network_fee_usd,
                "priority_fee_usd":resolved_priority_fee_usd,
                "fee_configuration_source":if profile_fee_override {"PROFILE_OVERRIDE"} else {"GLOBAL_CONFIG"},
                "reservation_mint":reservation_amount.as_ref().map(|_| reservation_mint),
                "reservation_amount":reservation_amount.as_ref().map(raw_to_string),
                "max_buy_usd": settings.max_buy_usd,
                "asset_decimals": asset_change.decimals,
                "minimum_order_quantity": minimum_quantity_for_decimals(asset_change.decimals)?,
                "asset_quantity_step": minimum_quantity_for_decimals(asset_change.decimals)?,
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
        if result_status(&order) != "CREATED" {
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
            "target_budget_usd":target_notional_usd.as_ref().map(raw_to_string),
            "trading_fee_bps":resolved_trading_fee_bps,
            "network_fee_usd":resolved_network_fee_usd,
            "priority_fee_usd":resolved_priority_fee_usd,
            "fee_configuration_source":if profile_fee_override {"PROFILE_OVERRIDE"} else {"GLOBAL_CONFIG"},
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
    price_provider: F,
    latency_recorder: Option<&LatencyRecorder>,
    now_ts: i64,
) -> Vec<Value>
where
    F: FnMut(&str) -> Option<MarketPrice>,
{
    execute_pending_paper_orders_with_at_and_policy(
        simulator,
        wallet_address,
        price_provider,
        latency_recorder,
        &ExecutionPolicy::default(),
        now_ts,
    )
}

/// Execute queued paper orders with an explicit global fee policy.
/// Individual persisted order settings override the matching fee fields only.
pub fn execute_pending_paper_orders_with_policy<F>(
    simulator: &mut PaperSimulator,
    wallet_address: Option<&str>,
    price_provider: F,
    latency_recorder: Option<&LatencyRecorder>,
    execution_policy: &ExecutionPolicy,
) -> Vec<Value>
where
    F: FnMut(&str) -> Option<MarketPrice>,
{
    execute_pending_paper_orders_with_at_and_policy(
        simulator,
        wallet_address,
        price_provider,
        latency_recorder,
        execution_policy,
        Utc::now().timestamp(),
    )
}

/// Deterministic clock-injected paper execution with an explicit global fee policy.
pub fn execute_pending_paper_orders_with_at_and_policy<F>(
    simulator: &mut PaperSimulator,
    wallet_address: Option<&str>,
    mut price_provider: F,
    latency_recorder: Option<&LatencyRecorder>,
    execution_policy: &ExecutionPolicy,
    now_ts: i64,
) -> Vec<Value>
where
    F: FnMut(&str) -> Option<MarketPrice>,
{
    let mut orders: Vec<_> = simulator.orders().values().cloned().collect();
    // Orders are stored by UUID, which is not source chronology. Preserve the
    // source event order so sequential partial sells consume the intended
    // remaining paper position and fills reach the P&L ledger in time order.
    orders.sort_by(|left, right| {
        let left_slot = left
            .details
            .get("source_slot")
            .and_then(Value::as_u64)
            .unwrap_or(u64::MAX);
        let right_slot = right
            .details
            .get("source_slot")
            .and_then(Value::as_u64)
            .unwrap_or(u64::MAX);
        let left_block_time = left
            .details
            .get("source_block_time")
            .and_then(Value::as_i64)
            .unwrap_or(i64::MAX);
        let right_block_time = right
            .details
            .get("source_block_time")
            .and_then(Value::as_i64)
            .unwrap_or(i64::MAX);

        left_slot
            .cmp(&right_slot)
            .then_with(|| left_block_time.cmp(&right_block_time))
            .then_with(|| left.created_at.cmp(&right.created_at))
            .then_with(|| left.signal_id.cmp(&right.signal_id))
    });
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
        // SELL quantity is fixed at queue time and protected by its token
        // reservation. Do not recalculate against a balance already reserved
        // for other pending orders.
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
        // Resolve per-profile overrides from the order snapshot so fees remain
        // deterministic across retries and process restarts.
        let mut order_policy = execution_policy.clone();
        if let Some(raw) = details.get("trading_fee_bps") {
            if !raw.is_null() {
                order_policy.fee_bps = number_to_string(raw)
                    .and_then(|value| value.parse::<u16>().ok())
                    .filter(|value| *value <= 10_000)
                    .unwrap_or(u16::MAX);
            }
        }
        if let Some(raw) = details.get("network_fee_usd") {
            if !raw.is_null() {
                order_policy.network_cost =
                    number_to_string(raw).unwrap_or_else(|| "invalid".to_string());
            }
        }
        if let Some(raw) = details.get("priority_fee_usd") {
            if !raw.is_null() {
                order_policy.priority_fee_usd =
                    number_to_string(raw).unwrap_or_else(|| "invalid".to_string());
            }
        }
        let result = simulator.execute_order(
            &order.order_id,
            Some(&market_price),
            &now,
            &order_policy,
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

/// Build a paper report using fresh public DEX Screener USD snapshots.
///
/// Only exact mint matches with at least $10,000 reported pool liquidity are used.
/// Missing, stale, or low-liquidity data remains unavailable rather than becoming zero.
pub fn report_with_verified_market_prices(
    simulator: &PaperSimulator,
    timeout: Duration,
) -> Result<Value, PaperCopyError> {
    if timeout.is_zero() || timeout > Duration::from_secs(60) {
        return Err(PaperCopyError::InvalidInput(
            "Market-data timeout must be greater than zero and no more than 60 seconds."
                .to_string(),
        ));
    }
    let minimum_liquidity = decimal("10000", "Minimum valuation liquidity")?;
    let mut prices = std::collections::BTreeMap::new();
    for mint in simulator.open_position_mints()? {
        let Ok(Some(price)) = fetch_market_price(&mint, timeout) else {
            continue;
        };
        let sufficiently_liquid = price
            .liquidity_usd
            .as_ref()
            .is_some_and(|liquidity| liquidity >= &minimum_liquidity);
        let fresh = price.is_fresh(60, None).unwrap_or(false);
        if price.mint == mint && price.validate().is_ok() && sufficiently_liquid && fresh {
            prices.insert(mint, price);
        }
    }
    Ok(simulator.report_with_market_prices(&prices, 60, None)?)
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
    fn minimum_quantity_uses_asset_atomic_precision() {
        assert_eq!(minimum_quantity_for_decimals(0).unwrap(), "1");
        assert_eq!(minimum_quantity_for_decimals(1).unwrap(), "0.1");
        assert_eq!(minimum_quantity_for_decimals(6).unwrap(), "0.000001");
        assert_eq!(minimum_quantity_for_decimals(9).unwrap(), "0.000000001");
        assert!(minimum_quantity_for_decimals(256).is_err());
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
        assert_eq!(order.details["asset_decimals"], 6);
        assert_eq!(order.details["minimum_order_quantity"], "0.000001");
        assert_eq!(order.details["asset_quantity_step"], "0.000001");
        assert_eq!(order.details["real_trading"], false);
        assert_eq!(order.details["reservation_mint"], "USD");
        assert_eq!(order.details["reservation_amount"], "5");
        assert_eq!(simulator.reserved_balance("USD"), BigDecimal::from(5));
        assert_eq!(simulator.available_balance("USD"), BigDecimal::from(95));
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
        assert_eq!(
            BigDecimal::from_str(&order.quantity).unwrap(),
            BigDecimal::from(5)
        );
        assert_eq!(order.details["reservation_mint"], ASSET);
        assert_eq!(order.details["reservation_amount"], "5");
        assert_eq!(simulator.reserved_balance(ASSET), BigDecimal::from(5));
        assert_eq!(simulator.available_balance(ASSET), BigDecimal::from(5));
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
        assert_eq!(
            BigDecimal::from_str(&simulator.fills()[0].quantity).unwrap(),
            BigDecimal::from(5)
        );
        assert_eq!(simulator.balances()[ASSET], BigDecimal::from(5));
    }

    #[test]
    fn sell_queue_rejects_inconsistent_source_pre_post_and_delta_amounts() {
        let mut simulator = simulator();
        let mut inconsistent = sell_signal(&"c".repeat(64));
        inconsistent["token_balance_changes"][0]["post_amount"] = json!("4000000");

        let queued = queue_paper_orders(
            WALLET,
            &[inconsistent],
            &mut simulator,
            AllowedSides::Sell,
            &CopySettings::default(),
            None,
        )
        .unwrap();

        assert!(queued.is_empty());
        assert!(simulator.orders().is_empty());
        assert_eq!(simulator.balances()[ASSET], BigDecimal::from(10));
    }

    #[test]
    fn partial_sell_uses_source_percentage_and_updates_both_virtual_balances() {
        let mut simulator = simulator();
        let mut source = sell_signal(&"e".repeat(64));
        source["token_balance_changes"][0]["delta_amount"] = json!("-2500000");
        source["token_balance_changes"][0]["pre_amount"] = json!("10000000");
        source["token_balance_changes"][0]["post_amount"] = json!("7500000");

        let queued = queue_paper_orders(
            WALLET,
            &[source],
            &mut simulator,
            AllowedSides::Sell,
            &CopySettings::default(),
            None,
        )
        .unwrap();
        assert_eq!(queued.len(), 1);
        assert_eq!(
            simulator.orders().values().next().unwrap().details["source_sell_fraction"],
            "0.25"
        );

        let now = Utc::now().to_rfc3339_opts(SecondsFormat::Millis, true);
        let price = MarketPrice::new(
            ASSET,
            "1",
            "USD",
            "partial-sell-fixture",
            now.clone(),
            Some("25000"),
            Some("test-liquidity".to_string()),
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
        let fill = &simulator.fills()[0];
        assert_eq!(
            BigDecimal::from_str(&fill.quantity).unwrap(),
            BigDecimal::from_str("2.5").unwrap()
        );
        assert_eq!(
            simulator.balances()[ASSET],
            BigDecimal::from_str("7.5").unwrap()
        );

        let expected_proceeds = BigDecimal::from_str(&fill.gross_quote).unwrap()
            - BigDecimal::from_str(&fill.fee).unwrap()
            - BigDecimal::from_str(&fill.network_cost).unwrap();
        assert_eq!(
            simulator.balances()["USD"],
            BigDecimal::from(100) + expected_proceeds
        );
        assert!(BigDecimal::from_str(&fill.fee).unwrap() > BigDecimal::zero());
    }

    #[test]
    fn fractional_partial_sell_rounds_down_to_token_atomic_precision() {
        let mut simulator = simulator();
        let mut source = sell_signal(&"9".repeat(64));
        source["token_balance_changes"][0]["delta_amount"] = json!("-1000000");
        source["token_balance_changes"][0]["pre_amount"] = json!("3000000");
        source["token_balance_changes"][0]["post_amount"] = json!("2000000");

        let queued = queue_paper_orders(
            WALLET,
            &[source],
            &mut simulator,
            AllowedSides::Sell,
            &CopySettings::default(),
            None,
        )
        .unwrap();
        assert_eq!(queued.len(), 1);

        let now = Utc::now().to_rfc3339_opts(SecondsFormat::Millis, true);
        let price = MarketPrice::new(
            ASSET,
            "1",
            "USD",
            "fractional-sell-fixture",
            now.clone(),
            Some("25000"),
            Some("test-liquidity".to_string()),
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
        assert_eq!(
            BigDecimal::from_str(&simulator.fills()[0].quantity).unwrap(),
            BigDecimal::from_str("3.333333").unwrap()
        );
        assert_eq!(
            simulator.balances()[ASSET],
            BigDecimal::from_str("6.666667").unwrap()
        );
        assert_eq!(
            simulator.orders().values().next().unwrap().details["asset_quantity_step"],
            "0.000001"
        );
    }

    #[test]
    fn sequential_partial_sells_execute_in_source_slot_order_and_are_idempotent() {
        let mut simulator = simulator();
        let mut later = sell_signal(&"f".repeat(64));
        later["signature_observation"]["slot"] = json!(102);
        later["signature_observation"]["block_time"] = json!(1760000001);
        later["token_balance_changes"][0]["delta_amount"] = json!("-2500000");
        later["token_balance_changes"][0]["pre_amount"] = json!("5000000");
        later["token_balance_changes"][0]["post_amount"] = json!("2500000");

        let mut earlier = sell_signal(&"a".repeat(64));
        earlier["signature_observation"]["slot"] = json!(101);
        earlier["signature_observation"]["block_time"] = json!(1760000000);

        // Intentionally queue the later source trade first. Execution must sort
        // by source chronology, not by the random order UUID or insertion order.
        let queued = queue_paper_orders(
            WALLET,
            &[later, earlier],
            &mut simulator,
            AllowedSides::Sell,
            &CopySettings::default(),
            None,
        )
        .unwrap();
        assert_eq!(queued.len(), 2);
        assert_eq!(
            BigDecimal::from_str(
                &simulator
                    .orders()
                    .values()
                    .find(|order| { order.details["source_slot"] == 101 })
                    .unwrap()
                    .quantity
            )
            .unwrap(),
            BigDecimal::from(5)
        );
        assert_eq!(
            simulator.reserved_balance(ASSET),
            BigDecimal::from_str("7.5").unwrap()
        );
        assert_eq!(
            simulator.available_balance(ASSET),
            BigDecimal::from_str("2.5").unwrap()
        );

        let now = Utc::now().to_rfc3339_opts(SecondsFormat::Millis, true);
        let mut price_call = 0usize;
        let outcomes = execute_pending_paper_orders_with(
            &mut simulator,
            Some(WALLET),
            |_mint| {
                let (value, source) = if price_call == 0 {
                    ("1", "source-slot-101")
                } else {
                    ("2", "source-slot-102")
                };
                price_call += 1;
                Some(
                    MarketPrice::new(
                        ASSET,
                        value,
                        "USD",
                        source,
                        now.clone(),
                        Some("25000"),
                        Some("test-liquidity".to_string()),
                        Some(now.clone()),
                    )
                    .unwrap(),
                )
            },
            None,
        );

        assert_eq!(outcomes.len(), 2);
        assert!(outcomes.iter().all(|value| value["status"] == "FILLED"));
        assert_eq!(simulator.fills().len(), 2);
        assert_eq!(simulator.fills()[0].price_source, "source-slot-101");
        assert_eq!(simulator.fills()[1].price_source, "source-slot-102");
        assert_eq!(
            BigDecimal::from_str(&simulator.fills()[0].quantity).unwrap(),
            BigDecimal::from(5)
        );
        assert_eq!(
            BigDecimal::from_str(&simulator.fills()[1].quantity).unwrap(),
            BigDecimal::from_str("2.5").unwrap()
        );
        assert_eq!(
            simulator.balances()[ASSET],
            BigDecimal::from_str("2.5").unwrap()
        );

        let expected_usd = BigDecimal::from(100)
            + BigDecimal::from_str(&simulator.fills()[0].net_quote).unwrap()
            + BigDecimal::from_str(&simulator.fills()[1].net_quote).unwrap();
        assert_eq!(simulator.balances()["USD"], expected_usd);

        let balance_after_fills = simulator.balances().clone();
        let no_second_fill = execute_pending_paper_orders_with(
            &mut simulator,
            Some(WALLET),
            |_mint| panic!("filled SELL orders must not request another market price"),
            None,
        );
        assert!(no_second_fill.is_empty());
        assert_eq!(simulator.fills().len(), 2);
        assert_eq!(simulator.balances(), &balance_after_fills);
    }

    #[test]
    fn profile_fee_overrides_are_persisted_and_applied_over_global_policy() {
        let mut simulator = simulator();
        let settings = CopySettings::from_json(Some(&json!({
            "sizing_mode":"FIXED_USD",
            "sizing_value":"10",
            "max_buy_usd":null,
            "trading_fee_bps":25,
            "network_fee_usd":"0.12",
            "priority_fee_usd":"0.03"
        })))
        .unwrap();
        let queued = queue_paper_orders(
            WALLET,
            &[buy_signal(&"d".repeat(64))],
            &mut simulator,
            AllowedSides::Buy,
            &settings,
            None,
        )
        .unwrap();
        assert_eq!(queued.len(), 1);

        let order_id = queued[0]["order_id"].as_str().unwrap().to_string();
        let order = &simulator.orders()[&order_id];
        assert_eq!(order.details["trading_fee_bps"], 25);
        assert_eq!(order.details["network_fee_usd"], "0.12");
        assert_eq!(order.details["priority_fee_usd"], "0.03");

        let observed = Utc::now().to_rfc3339_opts(SecondsFormat::Millis, true);
        let price = MarketPrice::new(
            ASSET,
            "1",
            "USD",
            "profile-fee-fixture",
            observed.clone(),
            Some("25000"),
            Some("test-liquidity".to_string()),
            Some(observed.clone()),
        )
        .unwrap();
        let global_policy = ExecutionPolicy {
            fee_bps: 100,
            network_cost: "1.00".to_string(),
            priority_fee_usd: "0.50".to_string(),
            ..ExecutionPolicy::default()
        };
        let outcomes = execute_pending_paper_orders_with_at_and_policy(
            &mut simulator,
            Some(WALLET),
            |_mint| Some(price.clone()),
            None,
            &global_policy,
            1_760_000_001,
        );

        assert_eq!(outcomes.len(), 1);
        assert_eq!(outcomes[0]["status"], "FILLED");
        let fill = &simulator.fills()[0];
        let gross = BigDecimal::from_str(&fill.gross_quote).unwrap();
        let expected_trading_fee = &gross * BigDecimal::from_str("0.0025").unwrap();
        assert_eq!(
            BigDecimal::from_str(&fill.fee).unwrap(),
            expected_trading_fee
        );
        assert_eq!(fill.base_network_fee, "0.12");
        assert_eq!(fill.priority_fee, "0.03");
        assert_eq!(fill.network_cost, "0.15");
        assert_eq!(simulator.orders()[&order_id].details["trading_fee_bps"], 25);
    }

    #[test]
    fn global_execution_fee_policy_applies_when_profile_has_no_override() {
        let mut simulator = simulator();
        let global_policy = ExecutionPolicy {
            fee_bps: 40,
            network_cost: "0.20".to_string(),
            priority_fee_usd: "0.10".to_string(),
            ..ExecutionPolicy::default()
        };
        let queued = queue_paper_orders_at_with_policy(
            WALLET,
            &[buy_signal(&"e".repeat(64))],
            &mut simulator,
            AllowedSides::Buy,
            &CopySettings::default(),
            None,
            &global_policy,
            1_760_000_001,
        )
        .unwrap();
        assert_eq!(queued.len(), 1);
        assert_eq!(
            simulator.orders().values().next().unwrap().details["fee_configuration_source"],
            "GLOBAL_CONFIG"
        );

        let observed = Utc::now().to_rfc3339_opts(SecondsFormat::Millis, true);
        let price = MarketPrice::new(
            ASSET,
            "1",
            "USD",
            "global-fee-fixture",
            observed.clone(),
            Some("25000"),
            Some("test-liquidity".to_string()),
            Some(observed.clone()),
        )
        .unwrap();
        // Simulate a changed environment after queue/restart. The order must
        // retain the policy snapshot written when it was queued.
        let changed_environment_policy = ExecutionPolicy {
            fee_bps: 100,
            network_cost: "10".to_string(),
            priority_fee_usd: "5".to_string(),
            ..ExecutionPolicy::default()
        };
        let outcomes = execute_pending_paper_orders_with_at_and_policy(
            &mut simulator,
            Some(WALLET),
            |_mint| Some(price.clone()),
            None,
            &changed_environment_policy,
            1_760_000_001,
        );

        assert_eq!(outcomes[0]["status"], "FILLED");
        let fill = &simulator.fills()[0];
        let gross = BigDecimal::from_str(&fill.gross_quote).unwrap();
        let expected_fee = &gross * BigDecimal::from_str("0.004").unwrap();
        assert_eq!(BigDecimal::from_str(&fill.fee).unwrap(), expected_fee);
        assert_eq!(fill.base_network_fee, "0.2");
        assert_eq!(fill.priority_fee, "0.1");
        assert_eq!(fill.network_cost, "0.3");
    }

    #[test]
    fn validates_profile_fee_overrides_and_bounds() {
        for raw in ["-1", "10001", "10.5", "NaN", "not-an-integer"] {
            assert!(
                CopySettings::from_json(Some(&json!({
                    "trading_fee_bps":raw
                })))
                .is_err(),
                "accepted invalid profile trading fee {raw}"
            );
        }

        for key in ["network_fee_usd", "priority_fee_usd"] {
            for raw in ["-0.01", "1000.01", "NaN", "invalid"] {
                let settings = json!({key:raw});
                assert!(
                    CopySettings::from_json(Some(&settings)).is_err(),
                    "accepted invalid profile {key} {raw}"
                );
            }
        }

        let zero_fee = CopySettings::from_json(Some(&json!({
            "trading_fee_bps":0,
            "network_fee_usd":"0",
            "priority_fee_usd":"0"
        })))
        .unwrap();
        assert_eq!(zero_fee.trading_fee_bps, Some(0));
        assert_eq!(zero_fee.network_fee_usd.as_deref(), Some("0"));
        assert_eq!(zero_fee.priority_fee_usd.as_deref(), Some("0"));
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
    fn multiple_pending_buy_signals_reserve_decreasing_available_cash() {
        let mut simulator = simulator();
        let rows = vec![buy_signal(&"b".repeat(64)), buy_signal(&"c".repeat(64))];
        let queued = queue_paper_orders(
            WALLET,
            &rows,
            &mut simulator,
            AllowedSides::Buy,
            &CopySettings::default(),
            None,
        )
        .unwrap();

        assert_eq!(queued.len(), 2);
        let mut budgets: Vec<BigDecimal> = simulator
            .orders()
            .values()
            .map(|order| {
                BigDecimal::from_str(order.details["target_budget_usd"].as_str().unwrap()).unwrap()
            })
            .collect();
        budgets.sort();
        assert_eq!(
            budgets,
            vec![BigDecimal::from_str("4.75").unwrap(), BigDecimal::from(5),]
        );
        assert_eq!(
            simulator.reserved_balance("USD"),
            BigDecimal::from_str("9.75").unwrap()
        );
        assert_eq!(
            simulator.available_balance("USD"),
            BigDecimal::from_str("90.25").unwrap()
        );
        assert_eq!(simulator.balances()["USD"], BigDecimal::from(100));
    }

    #[test]
    fn fixed_dollar_buy_queues_the_configured_budget_and_fills_within_it() {
        let mut simulator = simulator();
        let settings = CopySettings::from_json(Some(&json!({
            "sizing_mode":" fixed_usd ",
            "sizing_value":"7.50",
            "max_buy_usd":null
        })))
        .unwrap();
        let queued = queue_paper_orders(
            WALLET,
            &[buy_signal(&"9".repeat(64))],
            &mut simulator,
            AllowedSides::Buy,
            &settings,
            None,
        )
        .unwrap();
        assert_eq!(queued.len(), 1);
        assert_eq!(queued[0]["target_budget_usd"], "7.5");
        let order_id = queued[0]["order_id"].as_str().unwrap();

        let observed = Utc::now().to_rfc3339_opts(SecondsFormat::Millis, true);
        let price = MarketPrice::new(
            ASSET,
            "2",
            "USD",
            "test-fixture",
            &observed,
            Some("25000"),
            Some("test-fixture".to_string()),
            Some(observed.clone()),
        )
        .unwrap();
        let outcomes = execute_pending_paper_orders_with(
            &mut simulator,
            Some(WALLET),
            |_mint| Some(price.clone()),
            None,
        );
        assert_eq!(outcomes[0]["status"], "FILLED");
        let order = simulator.orders().get(order_id).unwrap();
        assert_eq!(order.details["sizing_mode"], "FIXED_USD");
        let fill = &simulator.fills()[0];
        let spent = BigDecimal::from_str(&fill.gross_quote).unwrap()
            + BigDecimal::from_str(&fill.fee).unwrap()
            + BigDecimal::from_str(&fill.network_cost).unwrap();
        let budget = BigDecimal::from_str("7.5").unwrap();
        let unspent = &budget - &spent;
        assert!(unspent >= BigDecimal::zero());
        assert!(unspent < BigDecimal::from_str("0.0000021").unwrap());
        assert_eq!(
            simulator.balances()["USD"],
            BigDecimal::from_str("100").unwrap() - spent
        );
    }

    #[test]
    fn fixed_percentage_buy_uses_current_virtual_usd_balance() {
        let mut balances = BTreeMap::new();
        balances.insert("USD".to_string(), "80".to_string());
        let mut simulator = PaperSimulator::in_memory(&balances).unwrap();
        let settings = CopySettings::from_json(Some(&json!({
            "sizing_mode":"FIXED_PERCENTAGE",
            "sizing_value":"12.5",
            "max_buy_usd":null
        })))
        .unwrap();
        let queued = queue_paper_orders(
            WALLET,
            &[buy_signal(&"a".repeat(64))],
            &mut simulator,
            AllowedSides::Buy,
            &settings,
            None,
        )
        .unwrap();
        assert_eq!(queued.len(), 1);
        assert_eq!(queued[0]["target_budget_usd"], "10");
        assert_eq!(
            simulator.orders().values().next().unwrap().details["sizing_mode"],
            "SOURCE_PERCENT"
        );
    }

    #[test]
    fn fixed_dollar_buy_is_not_silently_reduced_when_cash_is_insufficient() {
        let mut balances = BTreeMap::new();
        balances.insert("USD".to_string(), "5".to_string());
        let mut simulator = PaperSimulator::in_memory(&balances).unwrap();
        let settings = CopySettings::from_json(Some(&json!({
            "sizing_mode":"FIXED_USD",
            "sizing_value":"6",
            "max_buy_usd":null
        })))
        .unwrap();
        let queued = queue_paper_orders(
            WALLET,
            &[buy_signal(&"b".repeat(64))],
            &mut simulator,
            AllowedSides::Buy,
            &settings,
            None,
        )
        .unwrap();
        assert!(queued.is_empty());
        assert!(simulator.orders().is_empty());
    }

    #[test]
    fn validates_fixed_dollar_percentage_and_maximum_inputs() {
        assert!(AllowedSides::parse("UNKNOWN").is_err());
        for invalid_percent in ["0", "-1", "100", "100.01", "NaN", "not-a-number"] {
            assert!(
                CopySettings::from_json(Some(&json!({
                    "sizing_mode":"FIXED_PERCENTAGE","sizing_value":invalid_percent
                })))
                .is_err(),
                "accepted invalid percentage {invalid_percent}"
            );
        }
        for invalid_usd in ["0", "-1", "1000000.01", "NaN", "not-a-number"] {
            assert!(
                CopySettings::from_json(Some(&json!({
                    "sizing_mode":"FIXED_USD","sizing_value":invalid_usd,"max_buy_usd":null
                })))
                .is_err(),
                "accepted invalid fixed USD value {invalid_usd}"
            );
        }
        for invalid_max in ["0", "-1", "1000000.01", "NaN", "not-a-number"] {
            assert!(
                CopySettings::from_json(Some(&json!({
                    "sizing_mode":"FIXED_USD","sizing_value":"1","max_buy_usd":invalid_max
                })))
                .is_err(),
                "accepted invalid maximum BUY amount {invalid_max}"
            );
        }
        assert!(CopySettings::from_json(Some(&json!("FIXED_USD"))).is_err());
        assert!(CopySettings::from_json(Some(&json!({
            "sizing_mode":"FIXED_USD","sizing_value":true
        })))
        .is_err());
    }
}
