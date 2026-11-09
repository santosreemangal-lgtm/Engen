//! Conservative average-cost P&L accounting using exact decimal arithmetic.
//! Unknown cost basis and stale/missing prices remain explicitly unavailable.

use std::collections::BTreeMap;

use chrono::{DateTime, Utc};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use thiserror::Error;

pub const PORTFOLIO_CURRENCY: &str = "USD";
pub const COST_BASIS_METHOD: &str = "average_cost";

#[derive(Debug, Error, PartialEq, Eq)]
pub enum PnlError {
    #[error("{0} must be a valid finite decimal")]
    InvalidDecimal(&'static str),
    #[error("{0} must be positive")]
    MustBePositive(&'static str),
    #[error("{0} must be non-negative")]
    MustBeNonNegative(&'static str),
    #[error("Timestamp must be a timezone-aware ISO-8601 value")]
    InvalidTimestamp,
    #[error("Only USD portfolio valuation is supported")]
    UnsupportedCurrency,
    #[error("Price source is required")]
    MissingPriceSource,
    #[error("Liquidity source and timestamp are required with liquidity")]
    MissingLiquidityMetadata,
    #[error("Liquidity metadata cannot be supplied without a liquidity value")]
    UnexpectedLiquidityMetadata,
    #[error("Cannot sell more than the tracked position")]
    InsufficientPosition,
    #[error("Cannot sell an empty position")]
    EmptyPosition,
    #[error("Fees cannot exceed sale proceeds")]
    FeesExceedProceeds,
    #[error("fee_included must be boolean")]
    InvalidFeeFlag,
    #[error("Internal transfer needs distinct non-empty wallets")]
    InvalidTransfer,
    #[error("Fill side must be BUY or SELL")]
    InvalidFillSide,
    #[error("Market price mint does not match the requested mint")]
    MintMismatch,
}

fn validate_amount(value: Decimal, field: &'static str, allow_zero: bool) -> Result<Decimal, PnlError> {
    if value.is_sign_negative() {
        return Err(PnlError::MustBeNonNegative(field));
    }
    if value.is_zero() && !allow_zero {
        return Err(PnlError::MustBePositive(field));
    }
    Ok(value)
}

pub fn parse_timestamp(value: &str) -> Result<DateTime<Utc>, PnlError> {
    DateTime::parse_from_rfc3339(value)
        .map(|v| v.with_timezone(&Utc))
        .map_err(|_| PnlError::InvalidTimestamp)
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct MarketPrice {
    pub mint: String,
    pub price: Decimal,
    pub currency: String,
    pub source: String,
    pub observed_at: String,
    pub liquidity_usd: Option<Decimal>,
    pub liquidity_source: Option<String>,
    pub liquidity_observed_at: Option<String>,
}

impl MarketPrice {
    pub fn new(
        mint: String, price: Decimal, currency: String, source: String, observed_at: String,
        liquidity_usd: Option<Decimal>, liquidity_source: Option<String>,
        liquidity_observed_at: Option<String>,
    ) -> Result<Self, PnlError> {
        if mint.trim().is_empty() { return Err(PnlError::MissingPriceSource); }
        validate_amount(price, "Price", false)?;
        if currency != PORTFOLIO_CURRENCY { return Err(PnlError::UnsupportedCurrency); }
        if source.trim().is_empty() { return Err(PnlError::MissingPriceSource); }
        parse_timestamp(&observed_at)?;
        match (&liquidity_usd, &liquidity_source, &liquidity_observed_at) {
            (Some(value), Some(source), Some(at)) => {
                validate_amount(*value, "USD liquidity", true)?;
                if source.trim().is_empty() { return Err(PnlError::MissingLiquidityMetadata); }
                parse_timestamp(at)?;
            }
            (Some(_), _, _) => return Err(PnlError::MissingLiquidityMetadata),
            (None, None, None) => {}
            (None, _, _) => return Err(PnlError::UnexpectedLiquidityMetadata),
        }
        Ok(Self { mint, price, currency, source, observed_at, liquidity_usd, liquidity_source, liquidity_observed_at })
    }

    pub fn age_seconds_at(&self, now: DateTime<Utc>) -> Result<i64, PnlError> {
        Ok((now - parse_timestamp(&self.observed_at)?).num_seconds().max(0))
    }

    pub fn liquidity_age_seconds_at(&self, now: DateTime<Utc>) -> Result<Option<i64>, PnlError> {
        match &self.liquidity_observed_at {
            Some(at) => Ok(Some((now - parse_timestamp(at)?).num_seconds().max(0))),
            None => Ok(None),
        }
    }

    pub fn is_fresh_at(&self, max_age_seconds: u64, now: DateTime<Utc>) -> Result<bool, PnlError> {
        Ok(self.age_seconds_at(now)? <= max_age_seconds.min(i64::MAX as u64) as i64)
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Position {
    pub quantity: Decimal,
    pub cost_basis: Decimal,
    pub basis_complete: bool,
}

impl Default for Position {
    fn default() -> Self { Self { quantity: Decimal::ZERO, cost_basis: Decimal::ZERO, basis_complete: true } }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct PnlEvent {
    #[serde(rename = "type")]
    pub event_type: String,
    pub mint: String,
    pub quantity: String,
    pub currency: Option<String>,
    pub cash_cost: Option<String>,
    pub cash_proceeds: Option<String>,
    pub allocated_cost_basis: Option<String>,
    pub realized_pnl: Option<String>,
    pub pnl_status: Option<String>,
    pub trading_pnl: Option<String>,
    pub basis_status: Option<String>,
    pub from_wallet: Option<String>,
    pub to_wallet: Option<String>,
    pub timestamp: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct UnrealizedPnl {
    pub pnl: Option<String>,
    pub status: String,
    pub currency: String,
    pub market_value: Option<String>,
    pub cost_basis: Option<String>,
    pub price_age_seconds: Option<i64>,
    pub price_source: Option<String>,
    pub price_timestamp: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct PositionSnapshot {
    pub mint: String,
    pub quantity: String,
    pub cost_basis: Option<String>,
    pub basis_complete: bool,
    pub currency: String,
    pub cost_basis_method: String,
}

#[derive(Debug, Clone)]
pub struct AverageCostLedger {
    pub currency: String,
    pub positions: BTreeMap<String, Position>,
    pub events: Vec<PnlEvent>,
}

impl Default for AverageCostLedger {
    fn default() -> Self { Self::new(PORTFOLIO_CURRENCY).expect("USD is supported") }
}

impl AverageCostLedger {
    pub fn new(currency: &str) -> Result<Self, PnlError> {
        if currency != PORTFOLIO_CURRENCY { return Err(PnlError::UnsupportedCurrency); }
        Ok(Self { currency: currency.to_owned(), positions: BTreeMap::new(), events: Vec::new() })
    }

    fn position_mut(&mut self, mint: &str) -> Result<&mut Position, PnlError> {
        if mint.trim().is_empty() { return Err(PnlError::MissingPriceSource); }
        Ok(self.positions.entry(mint.to_owned()).or_default())
    }

    pub fn buy(&mut self, mint: &str, quantity: Decimal, unit_price: Decimal, fee: Decimal,
        fee_included: bool, network_fee: Decimal, timestamp: Option<String>) -> Result<PnlEvent, PnlError> {
        validate_amount(quantity, "Quantity", false)?;
        validate_amount(unit_price, "Unit price", false)?;
        validate_amount(fee, "Fee", true)?;
        validate_amount(network_fee, "Network fee", true)?;
        let mut total = quantity * unit_price;
        if !fee_included { total += fee; }
        total += network_fee;
        let pos = self.position_mut(mint)?;
        pos.quantity += quantity;
        if pos.basis_complete { pos.cost_basis += total; }
        let event = PnlEvent { event_type:"BUY".into(), mint:mint.into(), quantity:quantity.to_string(),
            currency:Some(self.currency.clone()), cash_cost:Some(total.to_string()), cash_proceeds:None,
            allocated_cost_basis:None, realized_pnl:Some("0".into()), pnl_status:Some("calculated".into()),
            trading_pnl:None, basis_status:None, from_wallet:None, to_wallet:None, timestamp };
        self.events.push(event.clone()); Ok(event)
    }

    pub fn sell(&mut self, mint: &str, quantity: Decimal, unit_price: Decimal, fee: Decimal,
        fee_included: bool, network_fee: Decimal, timestamp: Option<String>) -> Result<PnlEvent, PnlError> {
        validate_amount(quantity, "Quantity", false)?;
        validate_amount(unit_price, "Unit price", false)?;
        validate_amount(fee, "Fee", true)?;
        validate_amount(network_fee, "Network fee", true)?;
        let pos = self.position_mut(mint)?;
        if quantity > pos.quantity { return Err(PnlError::InsufficientPosition); }
        if pos.quantity.is_zero() { return Err(PnlError::EmptyPosition); }
        let mut proceeds = quantity * unit_price;
        if !fee_included { proceeds -= fee; }
        proceeds -= network_fee;
        if proceeds.is_sign_negative() { return Err(PnlError::FeesExceedProceeds); }
        let (allocated, realized) = if pos.basis_complete {
            let allocated = pos.cost_basis * quantity / pos.quantity;
            let realized = proceeds - allocated;
            pos.cost_basis -= allocated;
            (Some(allocated.to_string()), Some(realized.to_string()))
        } else { (None, None) };
        pos.quantity -= quantity;
        if pos.quantity.is_zero() { pos.cost_basis = Decimal::ZERO; pos.basis_complete = true; }
        let event = PnlEvent { event_type:"SELL".into(), mint:mint.into(), quantity:quantity.to_string(),
            currency:Some(self.currency.clone()), cash_cost:None, cash_proceeds:Some(proceeds.to_string()),
            allocated_cost_basis:allocated, realized_pnl:realized.clone(),
            pnl_status:Some(if realized.is_some() {"calculated"} else {"unavailable_unknown_cost_basis"}.into()),
            trading_pnl:None, basis_status:None, from_wallet:None, to_wallet:None, timestamp };
        self.events.push(event.clone()); Ok(event)
    }

    pub fn deposit(&mut self, mint: &str, quantity: Decimal, unit_price: Option<Decimal>,
        timestamp: Option<String>) -> Result<PnlEvent, PnlError> {
        validate_amount(quantity, "Quantity", false)?;
        if let Some(price) = unit_price { validate_amount(price, "Unit price", false)?; }
        let pos = self.position_mut(mint)?;
        match unit_price {
            None => pos.basis_complete = false,
            Some(price) if pos.quantity.is_zero() || pos.basis_complete => pos.cost_basis += quantity * price,
            Some(_) => pos.basis_complete = false,
        }
        pos.quantity += quantity;
        let event = PnlEvent { event_type:"DEPOSIT".into(), mint:mint.into(), quantity:quantity.to_string(),
            currency:None, cash_cost:None, cash_proceeds:None, allocated_cost_basis:None, realized_pnl:None,
            pnl_status:None, trading_pnl:Some("0".into()),
            basis_status:Some(if unit_price.is_some() {"known"} else {"unknown"}.into()),
            from_wallet:None, to_wallet:None, timestamp };
        self.events.push(event.clone()); Ok(event)
    }

    pub fn airdrop(&mut self, mint: &str, quantity: Decimal, timestamp: Option<String>) -> Result<PnlEvent, PnlError> {
        let mut event = self.deposit(mint, quantity, None, timestamp)?;
        event.event_type = "AIRDROP".into();
        if let Some(last) = self.events.last_mut() { last.event_type = "AIRDROP".into(); }
        Ok(event)
    }

    pub fn withdrawal(&mut self, mint: &str, quantity: Decimal, timestamp: Option<String>) -> Result<PnlEvent, PnlError> {
        validate_amount(quantity, "Quantity", false)?;
        let pos = self.position_mut(mint)?;
        if quantity > pos.quantity { return Err(PnlError::InsufficientPosition); }
        if !pos.quantity.is_zero() && pos.basis_complete {
            let allocated = pos.cost_basis * quantity / pos.quantity;
            pos.cost_basis -= allocated;
        }
        pos.quantity -= quantity;
        if pos.quantity.is_zero() { pos.cost_basis = Decimal::ZERO; pos.basis_complete = true; }
        let event = PnlEvent { event_type:"WITHDRAWAL".into(), mint:mint.into(), quantity:quantity.to_string(),
            currency:None, cash_cost:None, cash_proceeds:None, allocated_cost_basis:None, realized_pnl:None,
            pnl_status:None, trading_pnl:Some("0".into()), basis_status:None, from_wallet:None, to_wallet:None, timestamp };
        self.events.push(event.clone()); Ok(event)
    }

    pub fn internal_transfer(&mut self, mint: &str, quantity: Decimal, from_wallet: &str,
        to_wallet: &str, timestamp: Option<String>) -> Result<PnlEvent, PnlError> {
        validate_amount(quantity, "Quantity", false)?;
        if from_wallet.trim().is_empty() || to_wallet.trim().is_empty() || from_wallet == to_wallet {
            return Err(PnlError::InvalidTransfer);
        }
        if quantity > self.position_mut(mint)?.quantity { return Err(PnlError::InsufficientPosition); }
        let event = PnlEvent { event_type:"INTERNAL_TRANSFER".into(), mint:mint.into(), quantity:quantity.to_string(),
            currency:None, cash_cost:None, cash_proceeds:None, allocated_cost_basis:None, realized_pnl:None,
            pnl_status:None, trading_pnl:Some("0".into()), basis_status:None,
            from_wallet:Some(from_wallet.into()), to_wallet:Some(to_wallet.into()), timestamp };
        self.events.push(event.clone()); Ok(event)
    }

    pub fn unrealized_pnl(&mut self, mint: &str, market: Option<&MarketPrice>, max_age_seconds: u64,
        now: DateTime<Utc>) -> Result<UnrealizedPnl, PnlError> {
        let pos = self.position_mut(mint)?.clone();
        let Some(price) = market else { return Ok(unavailable("price_unavailable", &self.currency, None, None)); };
        if price.mint != mint { return Ok(unavailable("price_unavailable", &self.currency, None, None)); }
        let age = price.age_seconds_at(now)?;
        if age > max_age_seconds.min(i64::MAX as u64) as i64 {
            return Ok(unavailable("stale_price", &self.currency, Some(age), Some(price.source.clone())));
        }
        if !pos.basis_complete {
            return Ok(unavailable("unknown_cost_basis", &self.currency, Some(age), Some(price.source.clone())));
        }
        let value = pos.quantity * price.price;
        let pnl = value - pos.cost_basis;
        Ok(UnrealizedPnl { pnl:Some(pnl.to_string()), status:"calculated".into(), currency:self.currency.clone(),
            market_value:Some(value.to_string()), cost_basis:Some(pos.cost_basis.to_string()),
            price_age_seconds:Some(age), price_source:Some(price.source.clone()), price_timestamp:Some(price.observed_at.clone()) })
    }

    pub fn snapshot(&mut self, mint: &str) -> Result<PositionSnapshot, PnlError> {
        let pos = self.position_mut(mint)?;
        Ok(PositionSnapshot { mint:mint.into(), quantity:pos.quantity.to_string(),
            cost_basis:if pos.basis_complete {Some(pos.cost_basis.to_string())} else {None},
            basis_complete:pos.basis_complete, currency:self.currency.clone(), cost_basis_method:COST_BASIS_METHOD.into() })
    }
}

fn unavailable(status: &str, currency: &str, age: Option<i64>, source: Option<String>) -> UnrealizedPnl {
    UnrealizedPnl { pnl:None, status:status.into(), currency:currency.into(), market_value:None,
        cost_basis:None, price_age_seconds:age, price_source:source, price_timestamp:None }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Fill {
    pub side: String,
    pub asset_mint: String,
    pub quantity: Decimal,
    pub execution_price: Decimal,
    #[serde(default)]
    pub fee: Decimal,
    #[serde(default)]
    pub network_cost: Decimal,
    pub created_at: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RealizedPnl {
    pub pnl: Option<String>,
    pub status: String,
    pub currency: String,
}

pub fn realized_pnl_from_fills(fills: &[Fill]) -> Result<BTreeMap<String, RealizedPnl>, PnlError> {
    let mut ledger = AverageCostLedger::default();
    let mut by_mint: BTreeMap<String, (Decimal, bool)> = BTreeMap::new();
    for fill in fills {
        match fill.side.as_str() {
            "BUY" => { ledger.buy(&fill.asset_mint, fill.quantity, fill.execution_price, fill.fee, false, fill.network_cost, fill.created_at.clone())?; }
            "SELL" => {
                let tracked = ledger.snapshot(&fill.asset_mint)?.quantity.parse::<Decimal>().map_err(|_| PnlError::InvalidDecimal("Quantity"))?;
                if fill.quantity > tracked {
                    ledger.deposit(&fill.asset_mint, fill.quantity - tracked, None, fill.created_at.clone())?;
                }
                let event = ledger.sell(&fill.asset_mint, fill.quantity, fill.execution_price, fill.fee, false, fill.network_cost, fill.created_at.clone())?;
                let item = by_mint.entry(fill.asset_mint.clone()).or_insert((Decimal::ZERO, true));
                if let Some(realized) = event.realized_pnl { item.0 += realized.parse::<Decimal>().map_err(|_| PnlError::InvalidDecimal("P&L"))?; }
                else { item.1 = false; }
            }
            _ => return Err(PnlError::InvalidFillSide),
        }
    }
    Ok(by_mint.into_iter().map(|(mint, (pnl, complete))| (mint, RealizedPnl {
        pnl:if complete {Some(pnl.to_string())} else {None},
        status:if complete {"calculated"} else {"unknown_cost_basis"}.into(),
        currency:PORTFOLIO_CURRENCY.into(),
    })).collect())
}

#[cfg(test)]
mod tests {
    use super::*;
    use rust_decimal_macros::dec;

    fn ts() -> String { "2026-11-09T12:00:00Z".into() }

    #[test]
    fn average_cost_buy_sell_and_fees() {
        let mut ledger = AverageCostLedger::default();
        ledger.buy("mint", dec!(2), dec!(10), dec!(1), false, dec!(0), Some(ts())).unwrap();
        let sale = ledger.sell("mint", dec!(1), dec!(15), dec!(0), false, dec!(0), None).unwrap();
        assert_eq!(sale.realized_pnl.as_deref(), Some("4.5"));
        assert_eq!(ledger.snapshot("mint").unwrap().quantity, "1");
    }

    #[test]
    fn unknown_basis_never_fabricates_pnl() {
        let mut ledger = AverageCostLedger::default();
        ledger.deposit("mint", dec!(2), None, None).unwrap();
        let sale = ledger.sell("mint", dec!(1), dec!(5), dec!(0), false, dec!(0), None).unwrap();
        assert_eq!(sale.realized_pnl, None);
    }

    #[test]
    fn refuses_oversell_and_invalid_timestamp() {
        let mut ledger = AverageCostLedger::default();
        ledger.buy("mint", dec!(1), dec!(1), dec!(0), false, dec!(0), None).unwrap();
        assert_eq!(ledger.sell("mint", dec!(2), dec!(1), dec!(0), false, dec!(0), None), Err(PnlError::InsufficientPosition));
        assert!(parse_timestamp("2026-11-09T12:00:00").is_err());
    }
}
