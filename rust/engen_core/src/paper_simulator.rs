//! Paper ledger — maps to engine/paper_simulator.py (paper only).
//! Virtual balances / orders / fills. No signing or broadcast.

use crate::error::{EngenError, Result};
use chrono::{DateTime, Utc};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::str::FromStr;
use uuid::Uuid;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum OrderSide {
    Buy,
    Sell,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub enum OrderStatus {
    #[serde(rename = "CREATED")]
    Created,
    #[serde(rename = "FILLED")]
    Filled,
    #[serde(rename = "REJECTED")]
    Rejected,
    #[serde(rename = "CANCELLED")]
    Cancelled,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PaperOrder {
    pub order_id: String,
    pub signal_id: String,
    pub side: OrderSide,
    pub asset_mint: String,
    pub quantity: String,
    pub quote_mint: String,
    pub status: OrderStatus,
    pub created_at: DateTime<Utc>,
    #[serde(default)]
    pub details: serde_json::Value,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PaperFill {
    pub order_id: String,
    pub signal_id: String,
    pub side: OrderSide,
    pub asset_mint: String,
    pub quantity: String,
    pub price_usd: String,
    pub fee_usd: String,
    pub filled_at: DateTime<Utc>,
}

#[derive(Debug, Clone)]
pub struct MarketPrice {
    pub price_usd: Decimal,
    pub liquidity_usd: Decimal,
    pub age_seconds: u64,
}

#[derive(Debug)]
pub struct PaperSimulator {
    balances: HashMap<String, Decimal>,
    orders: HashMap<String, PaperOrder>,
    fills: Vec<PaperFill>,
    signal_index: HashMap<String, String>,
    db_path: Option<PathBuf>,
}

impl Default for PaperSimulator {
    fn default() -> Self {
        Self::new()
    }
}

impl PaperSimulator {
    pub fn new() -> Self {
        Self {
            balances: HashMap::new(),
            orders: HashMap::new(),
            fills: Vec::new(),
            signal_index: HashMap::new(),
            db_path: None,
        }
    }

    /// Optional SQLite path for restart survival (paper only).
    pub fn with_sqlite(path: impl AsRef<Path>) -> Result<Self> {
        let path = path.as_ref().to_path_buf();
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(|e| EngenError::Other(e.to_string()))?;
        }
        let mut sim = Self {
            db_path: Some(path.clone()),
            ..Self::new()
        };
        sim.init_db()?;
        sim.load_from_db()?;
        Ok(sim)
    }

    fn init_db(&self) -> Result<()> {
        let Some(path) = &self.db_path else {
            return Ok(());
        };
        let conn = rusqlite::Connection::open(path)?;
        conn.execute_batch(
            r#"
            CREATE TABLE IF NOT EXISTS paper_balances (
                mint TEXT PRIMARY KEY,
                amount TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS paper_orders (
                order_id TEXT PRIMARY KEY,
                signal_id TEXT NOT NULL UNIQUE,
                side TEXT NOT NULL,
                asset_mint TEXT NOT NULL,
                quantity TEXT NOT NULL,
                quote_mint TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                details TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS paper_fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT NOT NULL,
                signal_id TEXT NOT NULL,
                side TEXT NOT NULL,
                asset_mint TEXT NOT NULL,
                quantity TEXT NOT NULL,
                price_usd TEXT NOT NULL,
                fee_usd TEXT NOT NULL,
                filled_at TEXT NOT NULL
            );
            "#,
        )?;
        Ok(())
    }

    fn load_from_db(&mut self) -> Result<()> {
        let Some(path) = &self.db_path else {
            return Ok(());
        };
        let conn = rusqlite::Connection::open(path)?;
        {
            let mut stmt = conn.prepare("SELECT mint, amount FROM paper_balances")?;
            let rows = stmt.query_map([], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
            })?;
            for row in rows {
                let (mint, amount) = row?;
                if let Ok(v) = Decimal::from_str(&amount) {
                    self.balances.insert(mint, v);
                }
            }
        }
        {
            let mut stmt = conn.prepare(
                "SELECT order_id, signal_id, side, asset_mint, quantity, quote_mint, status, created_at, details FROM paper_orders",
            )?;
            let rows = stmt.query_map([], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                    row.get::<_, String>(4)?,
                    row.get::<_, String>(5)?,
                    row.get::<_, String>(6)?,
                    row.get::<_, String>(7)?,
                    row.get::<_, String>(8)?,
                ))
            })?;
            for row in rows {
                let (order_id, signal_id, side, asset_mint, quantity, quote_mint, status, created_at, details) =
                    row?;
                let side = match side.as_str() {
                    "SELL" => OrderSide::Sell,
                    _ => OrderSide::Buy,
                };
                let status = match status.as_str() {
                    "FILLED" => OrderStatus::Filled,
                    "REJECTED" => OrderStatus::Rejected,
                    "CANCELLED" => OrderStatus::Cancelled,
                    _ => OrderStatus::Created,
                };
                let created_at = DateTime::parse_from_rfc3339(&created_at)
                    .map(|d| d.with_timezone(&Utc))
                    .unwrap_or_else(|_| Utc::now());
                let details: serde_json::Value =
                    serde_json::from_str(&details).unwrap_or(serde_json::json!({}));
                let order = PaperOrder {
                    order_id: order_id.clone(),
                    signal_id: signal_id.clone(),
                    side,
                    asset_mint,
                    quantity,
                    quote_mint,
                    status,
                    created_at,
                    details,
                };
                self.signal_index.insert(signal_id, order_id.clone());
                self.orders.insert(order_id, order);
            }
        }
        Ok(())
    }

    fn persist_balance(&self, mint: &str, amount: Decimal) -> Result<()> {
        let Some(path) = &self.db_path else {
            return Ok(());
        };
        let conn = rusqlite::Connection::open(path)?;
        conn.execute(
            "INSERT INTO paper_balances(mint, amount) VALUES(?1, ?2)
             ON CONFLICT(mint) DO UPDATE SET amount=excluded.amount",
            rusqlite::params![mint, amount.normalize().to_string()],
        )?;
        Ok(())
    }

    fn persist_order(&self, order: &PaperOrder) -> Result<()> {
        let Some(path) = &self.db_path else {
            return Ok(());
        };
        let conn = rusqlite::Connection::open(path)?;
        let side = match order.side {
            OrderSide::Buy => "BUY",
            OrderSide::Sell => "SELL",
        };
        let status = match order.status {
            OrderStatus::Created => "CREATED",
            OrderStatus::Filled => "FILLED",
            OrderStatus::Rejected => "REJECTED",
            OrderStatus::Cancelled => "CANCELLED",
        };
        conn.execute(
            "INSERT INTO paper_orders(order_id, signal_id, side, asset_mint, quantity, quote_mint, status, created_at, details)
             VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9)
             ON CONFLICT(order_id) DO UPDATE SET
               status=excluded.status, quantity=excluded.quantity, details=excluded.details",
            rusqlite::params![
                order.order_id,
                order.signal_id,
                side,
                order.asset_mint,
                order.quantity,
                order.quote_mint,
                status,
                order.created_at.to_rfc3339(),
                order.details.to_string(),
            ],
        )?;
        Ok(())
    }

    fn persist_fill(&self, fill: &PaperFill) -> Result<()> {
        let Some(path) = &self.db_path else {
            return Ok(());
        };
        let conn = rusqlite::Connection::open(path)?;
        let side = match fill.side {
            OrderSide::Buy => "BUY",
            OrderSide::Sell => "SELL",
        };
        conn.execute(
            "INSERT INTO paper_fills(order_id, signal_id, side, asset_mint, quantity, price_usd, fee_usd, filled_at)
             VALUES(?1,?2,?3,?4,?5,?6,?7,?8)",
            rusqlite::params![
                fill.order_id,
                fill.signal_id,
                side,
                fill.asset_mint,
                fill.quantity,
                fill.price_usd,
                fill.fee_usd,
                fill.filled_at.to_rfc3339(),
            ],
        )?;
        Ok(())
    }

    pub fn set_virtual_balance(&mut self, amount: &str, mint: &str) -> Result<()> {
        let value = Decimal::from_str(amount)
            .map_err(|_| EngenError::InvalidInput("invalid balance amount".into()))?;
        if value.is_sign_negative() {
            return Err(EngenError::InvalidInput("balance must be >= 0".into()));
        }
        self.balances.insert(mint.to_string(), value);
        self.persist_balance(mint, value)?;
        Ok(())
    }

    pub fn balance(&self, mint: &str) -> Decimal {
        self.balances.get(mint).copied().unwrap_or(Decimal::ZERO)
    }

    pub fn create_order(
        &mut self,
        signal_id: &str,
        side: OrderSide,
        asset_mint: &str,
        quantity: &str,
        quote_mint: &str,
        details: serde_json::Value,
    ) -> Result<PaperOrder> {
        if let Some(existing_id) = self.signal_index.get(signal_id) {
            if let Some(existing) = self.orders.get(existing_id) {
                return Ok(existing.clone());
            }
        }
        let order = PaperOrder {
            order_id: Uuid::new_v4().to_string(),
            signal_id: signal_id.to_string(),
            side,
            asset_mint: asset_mint.to_string(),
            quantity: quantity.to_string(),
            quote_mint: quote_mint.to_string(),
            status: OrderStatus::Created,
            created_at: Utc::now(),
            details,
        };
        self.signal_index
            .insert(signal_id.to_string(), order.order_id.clone());
        self.orders.insert(order.order_id.clone(), order.clone());
        self.persist_order(&order)?;
        Ok(order)
    }

    /// Fill a CREATED order using a market snapshot. Paper only.
    pub fn execute_order(
        &mut self,
        order_id: &str,
        market: &MarketPrice,
        slippage_bps: u32,
        fee_bps: u32,
        min_liquidity_usd: Decimal,
        max_price_age_seconds: u64,
    ) -> Result<serde_json::Value> {
        let order = self
            .orders
            .get(order_id)
            .cloned()
            .ok_or_else(|| EngenError::NotFound(format!("order {order_id}")))?;

        if order.status != OrderStatus::Created {
            return Ok(serde_json::json!({
                "status": "SKIPPED",
                "reason": "Order is not in CREATED state",
                "order_id": order_id,
            }));
        }
        if market.age_seconds > max_price_age_seconds {
            return Ok(serde_json::json!({
                "status": "WAITING_FOR_MARKET_DATA",
                "reason": "Price snapshot too old",
                "order_id": order_id,
            }));
        }
        if market.liquidity_usd < min_liquidity_usd {
            return Ok(serde_json::json!({
                "status": "WAITING_FOR_MARKET_DATA",
                "reason": "Liquidity below minimum",
                "order_id": order_id,
            }));
        }
        if market.price_usd <= Decimal::ZERO {
            return Ok(serde_json::json!({
                "status": "WAITING_FOR_MARKET_DATA",
                "reason": "Invalid price",
                "order_id": order_id,
            }));
        }

        let slip = Decimal::from(slippage_bps) / Decimal::from(10_000u32);
        let fee_rate = Decimal::from(fee_bps) / Decimal::from(10_000u32);

        match order.side {
            OrderSide::Buy => {
                let target = order
                    .details
                    .get("target_notional_usd")
                    .and_then(|v| v.as_str())
                    .and_then(|s| Decimal::from_str(s).ok())
                    .unwrap_or_else(|| self.balance("USD"));
                let usd = self.balance("USD");
                let spend = if target > usd { usd } else { target };
                if spend <= Decimal::ZERO {
                    return Ok(serde_json::json!({
                        "status": "REJECTED",
                        "reason": "Insufficient virtual USD",
                        "order_id": order_id,
                    }));
                }
                let px = market.price_usd * (Decimal::ONE + slip);
                let fee = spend * fee_rate;
                let net = spend - fee;
                if net <= Decimal::ZERO || px <= Decimal::ZERO {
                    return Ok(serde_json::json!({
                        "status": "REJECTED",
                        "reason": "Spend too small after fees",
                        "order_id": order_id,
                    }));
                }
                let qty = net / px;
                self.balances.insert("USD".into(), usd - spend);
                let asset_bal = self.balance(&order.asset_mint) + qty;
                self.balances.insert(order.asset_mint.clone(), asset_bal);
                self.persist_balance("USD", self.balance("USD"))?;
                self.persist_balance(&order.asset_mint, asset_bal)?;

                let mut filled = order.clone();
                filled.status = OrderStatus::Filled;
                filled.quantity = qty.normalize().to_string();
                self.orders.insert(order_id.to_string(), filled.clone());
                self.persist_order(&filled)?;

                let fill = PaperFill {
                    order_id: order_id.to_string(),
                    signal_id: order.signal_id.clone(),
                    side: OrderSide::Buy,
                    asset_mint: order.asset_mint.clone(),
                    quantity: filled.quantity.clone(),
                    price_usd: px.normalize().to_string(),
                    fee_usd: fee.normalize().to_string(),
                    filled_at: Utc::now(),
                };
                self.persist_fill(&fill)?;
                self.fills.push(fill);

                Ok(serde_json::json!({
                    "status": "FILLED",
                    "order_id": order_id,
                    "side": "BUY",
                    "quantity": filled.quantity,
                    "real_trading": false,
                    "transaction_broadcast": false,
                }))
            }
            OrderSide::Sell => {
                let owned = self.balance(&order.asset_mint);
                if owned <= Decimal::ZERO {
                    return Ok(serde_json::json!({
                        "status": "WAITING_FOR_POSITION",
                        "reason": "No paper position",
                        "order_id": order_id,
                    }));
                }
                let fraction = order
                    .details
                    .get("source_sell_fraction")
                    .and_then(|v| v.as_str())
                    .and_then(|s| Decimal::from_str(s).ok())
                    .unwrap_or(Decimal::ONE);
                let fraction = if fraction <= Decimal::ZERO || fraction > Decimal::ONE {
                    Decimal::ONE
                } else {
                    fraction
                };
                let qty = owned * fraction;
                let px = market.price_usd * (Decimal::ONE - slip);
                let gross = qty * px;
                let fee = gross * fee_rate;
                let net = gross - fee;
                self.balances
                    .insert(order.asset_mint.clone(), owned - qty);
                let usd = self.balance("USD") + net;
                self.balances.insert("USD".into(), usd);
                self.persist_balance(&order.asset_mint, self.balance(&order.asset_mint))?;
                self.persist_balance("USD", usd)?;

                let mut filled = order.clone();
                filled.status = OrderStatus::Filled;
                filled.quantity = qty.normalize().to_string();
                self.orders.insert(order_id.to_string(), filled.clone());
                self.persist_order(&filled)?;

                let fill = PaperFill {
                    order_id: order_id.to_string(),
                    signal_id: order.signal_id.clone(),
                    side: OrderSide::Sell,
                    asset_mint: order.asset_mint.clone(),
                    quantity: filled.quantity.clone(),
                    price_usd: px.normalize().to_string(),
                    fee_usd: fee.normalize().to_string(),
                    filled_at: Utc::now(),
                };
                self.persist_fill(&fill)?;
                self.fills.push(fill);

                Ok(serde_json::json!({
                    "status": "FILLED",
                    "order_id": order_id,
                    "side": "SELL",
                    "quantity": filled.quantity,
                    "real_trading": false,
                    "transaction_broadcast": false,
                }))
            }
        }
    }

    pub fn pending_automatic_count(&self) -> usize {
        self.orders
            .values()
            .filter(|o| {
                o.status == OrderStatus::Created
                    && o.details.get("origin").and_then(|v| v.as_str())
                        == Some("AUTOMATIC_WALLET_COPY")
            })
            .count()
    }

    pub fn pending_order_ids(&self) -> Vec<String> {
        self.orders
            .values()
            .filter(|o| o.status == OrderStatus::Created)
            .map(|o| o.order_id.clone())
            .collect()
    }

    pub fn report(&self) -> serde_json::Value {
        let balances: HashMap<String, String> = self
            .balances
            .iter()
            .map(|(k, v)| (k.clone(), v.normalize().to_string()))
            .collect();
        serde_json::json!({
            "mode": "PAPER_SIMULATION",
            "real_trading": false,
            "transaction_broadcast": false,
            "balances": balances,
            "orders": self.orders.values().cloned().collect::<Vec<_>>(),
            "fills": self.fills,
            "pending_automatic": self.pending_automatic_count(),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dedupe_by_signal_id() {
        let mut sim = PaperSimulator::new();
        let a = sim
            .create_order(
                "sig-1",
                OrderSide::Buy,
                "mint",
                "1",
                "USD",
                serde_json::json!({"origin": "AUTOMATIC_WALLET_COPY"}),
            )
            .unwrap();
        let b = sim
            .create_order(
                "sig-1",
                OrderSide::Buy,
                "mint",
                "1",
                "USD",
                serde_json::json!({"origin": "AUTOMATIC_WALLET_COPY"}),
            )
            .unwrap();
        assert_eq!(a.order_id, b.order_id);
        assert_eq!(sim.pending_automatic_count(), 1);
    }

    #[test]
    fn buy_fill_updates_balances() {
        let mut sim = PaperSimulator::new();
        sim.set_virtual_balance("100", "USD").unwrap();
        let order = sim
            .create_order(
                "buy-1",
                OrderSide::Buy,
                "TOKEN",
                "1",
                "USD",
                serde_json::json!({
                    "origin": "AUTOMATIC_WALLET_COPY",
                    "target_notional_usd": "10"
                }),
            )
            .unwrap();
        let market = MarketPrice {
            price_usd: Decimal::from(2),
            liquidity_usd: Decimal::from(50_000),
            age_seconds: 1,
        };
        let result = sim
            .execute_order(&order.order_id, &market, 50, 10, Decimal::from(10_000), 60)
            .unwrap();
        assert_eq!(result["status"], "FILLED");
        assert!(sim.balance("USD") < Decimal::from(100));
        assert!(sim.balance("TOKEN") > Decimal::ZERO);
        assert_eq!(sim.pending_automatic_count(), 0);
    }
}
