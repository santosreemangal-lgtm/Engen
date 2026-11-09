//! Exact decimal-based paper portfolio accounting and conservative P&L.

use std::collections::BTreeMap;
use std::str::FromStr;

use bigdecimal::BigDecimal;
use chrono::{DateTime, Utc};
use num_traits::Zero;
use serde::{Deserialize, Serialize};
use thiserror::Error;

pub const PORTFOLIO_CURRENCY: &str = "USD";
pub const COST_BASIS_METHOD: &str = "average_cost";

#[derive(Debug, Error)]
pub enum PnlError {
    #[error("Invalid exact decimal value for {0}.")]
    InvalidDecimal(String),
    #[error("Invalid timestamp: {0}")]
    InvalidTimestamp(String),
    #[error("Accounting rule violation: {0}")]
    Accounting(String),
}

pub fn decimal_value(value: &str, field: &str, allow_zero: bool) -> Result<BigDecimal, PnlError> {
    let parsed = BigDecimal::from_str(value.trim())
        .map_err(|_| PnlError::InvalidDecimal(field.to_string()))?;
    if parsed < BigDecimal::zero() || (!allow_zero && parsed.is_zero()) {
        return Err(PnlError::InvalidDecimal(field.to_string()));
    }
    Ok(parsed)
}

pub fn parse_timestamp(value: &str) -> Result<DateTime<Utc>, PnlError> {
    DateTime::parse_from_rfc3339(value)
        .map(|date| date.with_timezone(&Utc))
        .map_err(|_| PnlError::InvalidTimestamp(value.to_string()))
}

fn current_or_parse(now: Option<&str>) -> Result<DateTime<Utc>, PnlError> {
    match now {
        Some(value) => parse_timestamp(value),
        None => Ok(Utc::now()),
    }
}

fn age_seconds(observed_at: &str, now: Option<&str>) -> Result<u64, PnlError> {
    let observed = parse_timestamp(observed_at)?;
    let current = current_or_parse(now)?;
    Ok(current
        .signed_duration_since(observed)
        .num_seconds()
        .max(0) as u64)
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct MarketPrice {
    pub mint: String,
    pub price: BigDecimal,
    pub currency: String,
    pub source: String,
    pub observed_at: String,
    pub liquidity_usd: Option<BigDecimal>,
    pub liquidity_source: Option<String>,
    pub liquidity_observed_at: Option<String>,
}

impl MarketPrice {
    // Keep the market snapshot constructor aligned with all recorded source fields.
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        mint: impl Into<String>,
        price: &str,
        currency: impl Into<String>,
        source: impl Into<String>,
        observed_at: impl Into<String>,
        liquidity_usd: Option<&str>,
        liquidity_source: Option<String>,
        liquidity_observed_at: Option<String>,
    ) -> Result<Self, PnlError> {
        let value = Self {
            mint: mint.into(),
            price: decimal_value(price, "Price", false)?,
            currency: currency.into(),
            source: source.into(),
            observed_at: observed_at.into(),
            liquidity_usd: liquidity_usd
                .map(|raw| decimal_value(raw, "USD liquidity", true))
                .transpose()?,
            liquidity_source,
            liquidity_observed_at,
        };
        value.validate()?;
        Ok(value)
    }

    pub fn validate(&self) -> Result<(), PnlError> {
        if self.mint.trim().is_empty() {
            return Err(PnlError::Accounting("Price needs a token mint.".to_string()));
        }
        if self.currency != PORTFOLIO_CURRENCY {
            return Err(PnlError::Accounting(
                "Prices must be converted to USD by a documented source before valuation."
                    .to_string(),
            ));
        }
        if self.source.trim().is_empty() {
            return Err(PnlError::Accounting("Price source is required.".to_string()));
        }
        parse_timestamp(&self.observed_at)?;

        match (&self.liquidity_usd, &self.liquidity_source, &self.liquidity_observed_at) {
            (Some(_), Some(source), Some(observed)) if !source.trim().is_empty() => {
                parse_timestamp(observed)?;
            }
            (None, None, None) => {}
            (Some(_), _, _) => {
                return Err(PnlError::Accounting(
                    "Liquidity source and timestamp are required.".to_string(),
                ));
            }
            _ => {
                return Err(PnlError::Accounting(
                    "Liquidity source/time cannot be supplied without a liquidity value."
                        .to_string(),
                ));
            }
        }
        Ok(())
    }

    pub fn age_seconds(&self, now: Option<&str>) -> Result<u64, PnlError> {
        age_seconds(&self.observed_at, now)
    }

    pub fn liquidity_age_seconds(&self, now: Option<&str>) -> Result<Option<u64>, PnlError> {
        self.liquidity_observed_at
            .as_deref()
            .map(|value| age_seconds(value, now))
            .transpose()
    }

    pub fn is_fresh(&self, max_age_seconds: u64, now: Option<&str>) -> Result<bool, PnlError> {
        Ok(self.age_seconds(now)? <= max_age_seconds)
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct LedgerEvent {
    pub event_type: String,
    pub mint: String,
    pub quantity: String,
    pub currency: String,
    pub cash_cost: Option<String>,
    pub cash_proceeds: Option<String>,
    pub allocated_cost_basis: Option<String>,
    pub realized_pnl: Option<String>,
    pub pnl_status: Option<String>,
    pub timestamp: Option<String>,
}

#[derive(Debug, Clone, PartialEq)]
struct Position {
    quantity: BigDecimal,
    cost_basis: BigDecimal,
    basis_complete: bool,
}

impl Default for Position {
    fn default() -> Self {
        Self {
            quantity: BigDecimal::zero(),
            cost_basis: BigDecimal::zero(),
            basis_complete: true,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PositionSnapshot {
    pub mint: String,
    pub quantity: String,
    pub cost_basis: Option<String>,
    pub basis_complete: bool,
    pub currency: String,
    pub cost_basis_method: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct UnrealizedPnl {
    pub pnl: Option<String>,
    pub market_value: Option<String>,
    pub cost_basis: Option<String>,
    pub currency: String,
    pub status: String,
    pub price_age_seconds: Option<u64>,
    pub price_source: Option<String>,
    pub price_timestamp: Option<String>,
}

#[derive(Debug, Default)]
pub struct AverageCostLedger {
    positions: BTreeMap<String, Position>,
    pub events: Vec<LedgerEvent>,
}

impl AverageCostLedger {
    pub fn new() -> Self {
        Self::default()
    }

    fn position_mut(&mut self, mint: &str) -> Result<&mut Position, PnlError> {
        if mint.trim().is_empty() {
            return Err(PnlError::Accounting("Token mint is required.".to_string()));
        }
        Ok(self.positions.entry(mint.to_string()).or_default())
    }

    #[allow(clippy::too_many_arguments)]
    pub fn buy(
        &mut self, mint: &str, quantity: &str, unit_price: &str, fee: &str,
        fee_included: bool, network_fee: &str, timestamp: Option<&str>,
    ) -> Result<LedgerEvent, PnlError> {
        let qty = decimal_value(quantity, "Quantity", false)?;
        let price = decimal_value(unit_price, "Unit price", false)?;
        let fee_value = decimal_value(fee, "Fee", true)?;
        let network = decimal_value(network_fee, "Network fee", true)?;
        if let Some(ts) = timestamp { parse_timestamp(ts)?; }

        let mut total_cost = &qty * &price;
        if !fee_included { total_cost += fee_value; }
        total_cost += network;

        let pos = self.position_mut(mint)?;
        pos.quantity += &qty;
        if pos.basis_complete { pos.cost_basis += &total_cost; }

        let event = LedgerEvent {
            event_type: "BUY".to_string(), mint: mint.to_string(), quantity: qty.to_string(),
            currency: PORTFOLIO_CURRENCY.to_string(), cash_cost: Some(total_cost.to_string()),
            cash_proceeds: None, allocated_cost_basis: None, realized_pnl: Some("0".to_string()),
            pnl_status: Some("calculated".to_string()), timestamp: timestamp.map(str::to_string),
        };
        self.events.push(event.clone());
        Ok(event)
    }

    #[allow(clippy::too_many_arguments)]
    pub fn sell(
        &mut self, mint: &str, quantity: &str, unit_price: &str, fee: &str,
        fee_included: bool, network_fee: &str, timestamp: Option<&str>,
    ) -> Result<LedgerEvent, PnlError> {
        let qty = decimal_value(quantity, "Quantity", false)?;
        let price = decimal_value(unit_price, "Unit price", false)?;
        let fee_value = decimal_value(fee, "Fee", true)?;
        let network = decimal_value(network_fee, "Network fee", true)?;
        if let Some(ts) = timestamp { parse_timestamp(ts)?; }

        let pos = self.position_mut(mint)?;
        if qty > pos.quantity {
            return Err(PnlError::Accounting("Cannot sell more than the tracked position.".to_string()));
        }
        if pos.quantity.is_zero() {
            return Err(PnlError::Accounting("Cannot sell an empty position.".to_string()));
        }

        let mut proceeds = &qty * &price;
        if !fee_included { proceeds -= fee_value; }
        proceeds -= network;
        if proceeds < BigDecimal::zero() {
            return Err(PnlError::Accounting("Fees cannot exceed sale proceeds.".to_string()));
        }

        let (allocated, realized) = if pos.basis_complete {
            let allocated = &pos.cost_basis * &qty / &pos.quantity;
            let realized = &proceeds - &allocated;
            pos.cost_basis -= &allocated;
            (Some(allocated), Some(realized))
        } else { (None, None) };

        let pnl_available = realized.is_some();
        pos.quantity -= &qty;
        if pos.quantity.is_zero() {
            pos.cost_basis = BigDecimal::zero();
            pos.basis_complete = true;
        }

        let event = LedgerEvent {
            event_type: "SELL".to_string(), mint: mint.to_string(), quantity: qty.to_string(),
            currency: PORTFOLIO_CURRENCY.to_string(), cash_cost: None,
            cash_proceeds: Some(proceeds.to_string()),
            allocated_cost_basis: allocated.map(|value| value.to_string()),
            realized_pnl: realized.map(|value| value.to_string()),
            pnl_status: Some(if pnl_available { "calculated" } else { "unavailable_unknown_cost_basis" }.to_string()),
            timestamp: timestamp.map(str::to_string),
        };
        self.events.push(event.clone());
        Ok(event)
    }

    pub fn deposit(
        &mut self, mint: &str, quantity: &str, unit_price: Option<&str>,
        timestamp: Option<&str>,
    ) -> Result<LedgerEvent, PnlError> {
        let qty = decimal_value(quantity, "Quantity", false)?;
        if let Some(ts) = timestamp { parse_timestamp(ts)?; }
        let pos = self.position_mut(mint)?;
        if let Some(price) = unit_price {
            let value = decimal_value(price, "Unit price", false)?;
            if pos.quantity.is_zero() || pos.basis_complete {
                pos.cost_basis += &qty * value;
            } else {
                pos.basis_complete = false;
            }
        } else {
            pos.basis_complete = false;
        }
        pos.quantity += &qty;
        let known = pos.basis_complete;
        let event = LedgerEvent {
            event_type: "DEPOSIT".to_string(), mint: mint.to_string(), quantity: qty.to_string(),
            currency: PORTFOLIO_CURRENCY.to_string(), cash_cost: None, cash_proceeds: None,
            allocated_cost_basis: None, realized_pnl: Some("0".to_string()),
            pnl_status: Some(if known { "known" } else { "unknown" }.to_string()),
            timestamp: timestamp.map(str::to_string),
        };
        self.events.push(event.clone());
        Ok(event)
    }

    pub fn airdrop(&mut self, mint: &str, quantity: &str, timestamp: Option<&str>) -> Result<LedgerEvent, PnlError> {
        let mut event = self.deposit(mint, quantity, None, timestamp)?;
        event.event_type = "AIRDROP".to_string();
        if let Some(last) = self.events.last_mut() { last.event_type = "AIRDROP".to_string(); }
        Ok(event)
    }

    pub fn withdrawal(&mut self, mint: &str, quantity: &str, timestamp: Option<&str>) -> Result<LedgerEvent, PnlError> {
        let qty = decimal_value(quantity, "Quantity", false)?;
        if let Some(ts) = timestamp { parse_timestamp(ts)?; }
        let pos = self.position_mut(mint)?;
        if qty > pos.quantity {
            return Err(PnlError::Accounting("Cannot withdraw more than the tracked position.".to_string()));
        }
        if !pos.quantity.is_zero() && pos.basis_complete {
            let allocated = &pos.cost_basis * &qty / &pos.quantity;
            pos.cost_basis -= allocated;
        }
        pos.quantity -= &qty;
        if pos.quantity.is_zero() { pos.cost_basis = BigDecimal::zero(); pos.basis_complete = true; }
        let event = LedgerEvent {
            event_type: "WITHDRAWAL".to_string(), mint: mint.to_string(), quantity: qty.to_string(),
            currency: PORTFOLIO_CURRENCY.to_string(), cash_cost: None, cash_proceeds: None,
            allocated_cost_basis: None, realized_pnl: Some("0".to_string()),
            pnl_status: Some("not_a_trade".to_string()), timestamp: timestamp.map(str::to_string),
        };
        self.events.push(event.clone());
        Ok(event)
    }

    pub fn internal_transfer(
        &mut self, mint: &str, quantity: &str, from_wallet: &str, to_wallet: &str,
        timestamp: Option<&str>,
    ) -> Result<LedgerEvent, PnlError> {
        let qty = decimal_value(quantity, "Quantity", false)?;
        if from_wallet.trim().is_empty() || to_wallet.trim().is_empty() || from_wallet == to_wallet {
            return Err(PnlError::Accounting("Internal transfer needs distinct source and destination wallets.".to_string()));
        }
        if let Some(ts) = timestamp { parse_timestamp(ts)?; }
        if qty > self.snapshot(mint).quantity.parse::<BigDecimal>().map_err(|_| PnlError::InvalidDecimal("tracked quantity".to_string()))? {
            return Err(PnlError::Accounting("Cannot transfer more than the tracked position.".to_string()));
        }
        let event = LedgerEvent {
            event_type: "INTERNAL_TRANSFER".to_string(), mint: mint.to_string(), quantity: qty.to_string(),
            currency: PORTFOLIO_CURRENCY.to_string(), cash_cost: None, cash_proceeds: None,
            allocated_cost_basis: None, realized_pnl: Some("0".to_string()),
            pnl_status: Some("not_a_trade".to_string()), timestamp: timestamp.map(str::to_string),
        };
        self.events.push(event.clone());
        Ok(event)
    }

    pub fn snapshot(&self, mint: &str) -> PositionSnapshot {
        let pos = self.positions.get(mint).cloned().unwrap_or_default();
        PositionSnapshot {
            mint: mint.to_string(), quantity: pos.quantity.to_string(),
            cost_basis: if pos.basis_complete { Some(pos.cost_basis.to_string()) } else { None },
            basis_complete: pos.basis_complete, currency: PORTFOLIO_CURRENCY.to_string(),
            cost_basis_method: COST_BASIS_METHOD.to_string(),
        }
    }

    pub fn unrealized_pnl(
        &self, mint: &str, market_price: Option<&MarketPrice>, max_age_seconds: u64,
        now: Option<&str>,
    ) -> Result<UnrealizedPnl, PnlError> {
        let Some(price) = market_price else {
            return Ok(unavailable("price_unavailable", None, None, None));
        };
        if price.mint != mint {
            return Ok(unavailable("price_unavailable", None, None, None));
        }
        let age = price.age_seconds(now)?;
        let pos = self.positions.get(mint).cloned().unwrap_or_default();
        let status = if age > max_age_seconds { "stale_price" }
            else if !pos.basis_complete { "unknown_cost_basis" }
            else { "calculated" };
        if status != "calculated" {
            return Ok(unavailable(status, Some(age), Some(price.source.clone()), Some(price.observed_at.clone())));
        }
        let value = &pos.quantity * &price.price;
        let pnl = &value - &pos.cost_basis;
        Ok(UnrealizedPnl {
            pnl: Some(pnl.to_string()), market_value: Some(value.to_string()),
            cost_basis: Some(pos.cost_basis.to_string()), currency: PORTFOLIO_CURRENCY.to_string(),
            status: status.to_string(), price_age_seconds: Some(age),
            price_source: Some(price.source.clone()), price_timestamp: Some(price.observed_at.clone()),
        })
    }
}

fn unavailable(status: &str, age: Option<u64>, source: Option<String>, timestamp: Option<String>) -> UnrealizedPnl {
    UnrealizedPnl {
        pnl: None, market_value: None, cost_basis: None, currency: PORTFOLIO_CURRENCY.to_string(),
        status: status.to_string(), price_age_seconds: age, price_source: source,
        price_timestamp: timestamp,
    }
}

pub fn realized_pnl_from_fills(
    fills: &[serde_json::Value],
) -> Result<BTreeMap<String, serde_json::Value>, PnlError> {
    let mut ledger = AverageCostLedger::new();
    let mut totals: BTreeMap<String, (BigDecimal, bool)> = BTreeMap::new();

    for fill in fills {
        let side = fill.get("side").and_then(serde_json::Value::as_str).unwrap_or("");
        let mint = fill.get("asset_mint").and_then(serde_json::Value::as_str).unwrap_or("");
        let quantity = fill.get("quantity").and_then(serde_json::Value::as_str).unwrap_or("");
        let price = fill.get("execution_price").and_then(serde_json::Value::as_str).unwrap_or("");
        let fee = fill.get("fee").and_then(serde_json::Value::as_str).unwrap_or("0");
        let network = fill.get("network_cost").and_then(serde_json::Value::as_str).unwrap_or("0");
        let timestamp = fill.get("created_at").and_then(serde_json::Value::as_str);

        match side {
            "BUY" => { ledger.buy(mint, quantity, price, fee, false, network, timestamp)?; }
            "SELL" => {
                let tracked = decimal_value(&ledger.snapshot(mint).quantity, "Tracked quantity", true)?;
                let sold = decimal_value(quantity, "Quantity", false)?;
                if sold > tracked {
                    let missing = &sold - &tracked;
                    ledger.deposit(mint, &missing.to_string(), None, timestamp)?;
                }
                let event = ledger.sell(mint, quantity, price, fee, false, network, timestamp)?;
                let total = totals.entry(mint.to_string()).or_insert((BigDecimal::zero(), true));
                if let Some(realized) = event.realized_pnl {
                    total.0 += BigDecimal::from_str(&realized)
                        .map_err(|_| PnlError::InvalidDecimal("realized pnl".to_string()))?;
                } else {
                    total.1 = false;
                }
            }
            _ => return Err(PnlError::Accounting("Fill side must be BUY or SELL.".to_string())),
        }
    }

    let mut output = BTreeMap::new();
    for (mint, (pnl, complete)) in totals {
        output.insert(mint, serde_json::json!({
            "pnl": if complete { Some(pnl.to_string()) } else { None },
            "status": if complete { "calculated" } else { "unknown_cost_basis" },
            "currency": PORTFOLIO_CURRENCY
        }));
    }
    Ok(output)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn average_cost_realized_pnl_uses_decimal_arithmetic() {
        let mut ledger = AverageCostLedger::new();
        ledger.buy("TOKEN", "2", "10", "0", false, "0", None).unwrap();
        let event = ledger.sell("TOKEN", "1", "15", "0", false, "0", None).unwrap();
        assert_eq!(event.realized_pnl.as_deref(), Some("5"));
        assert_eq!(ledger.snapshot("TOKEN").quantity, "1");
    }

    #[test]
    fn unknown_deposit_basis_stays_unknown_until_position_closes() {
        let mut ledger = AverageCostLedger::new();
        ledger.deposit("TOKEN", "3", None, None).unwrap();
        let event = ledger.sell("TOKEN", "1", "20", "0", false, "0", None).unwrap();
        assert_eq!(event.realized_pnl, None);
        assert!(!ledger.snapshot("TOKEN").basis_complete);
        ledger.sell("TOKEN", "2", "20", "0", false, "0", None).unwrap();
        assert!(ledger.snapshot("TOKEN").basis_complete);
    }

    #[test]
    fn stale_market_prices_do_not_produce_unrealized_pnl() {
        let mut ledger = AverageCostLedger::new();
        ledger.buy("TOKEN", "1", "10", "0", false, "0", None).unwrap();
        let price = MarketPrice::new(
            "TOKEN", "15", "USD", "test", "2026-01-01T00:00:00Z", None, None, None
        ).unwrap();
        let result = ledger.unrealized_pnl("TOKEN", Some(&price), 60, Some("2026-01-01T00:02:00Z")).unwrap();
        assert_eq!(result.pnl, None);
        assert_eq!(result.status, "stale_price");
    }

    #[test]
    fn unknown_basis_sale_is_unavailable_even_when_it_closes_position() {
        let mut ledger = AverageCostLedger::new();
        ledger.deposit("TOKEN", "2", None, None).unwrap();
        let event = ledger.sell("TOKEN", "2", "20", "0", false, "0", None).unwrap();
        assert_eq!(event.realized_pnl, None);
        assert_eq!(event.pnl_status.as_deref(), Some("unavailable_unknown_cost_basis"));
    }
}
