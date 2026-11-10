//! Paper ledger — maps to engine/paper_simulator.py (subset).
//! Virtual balances / orders only. No signing or broadcast.

use crate::error::{EngenError, Result};
use chrono::{DateTime, Utc};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::str::FromStr;
use uuid::Uuid;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum OrderSide {
    Buy,
    Sell,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum OrderStatus {
    Created,
    Filled,
    Rejected,
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
    pub filled_at: DateTime<Utc>,
}

#[derive(Debug, Default)]
pub struct PaperSimulator {
    balances: HashMap<String, Decimal>,
    orders: HashMap<String, PaperOrder>,
    fills: Vec<PaperFill>,
    signal_index: HashMap<String, String>,
}

impl PaperSimulator {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn set_virtual_balance(&mut self, amount: &str, mint: &str) -> Result<()> {
        let value = Decimal::from_str(amount)
            .map_err(|_| EngenError::InvalidInput("invalid balance amount".into()))?;
        if value.is_sign_negative() {
            return Err(EngenError::InvalidInput("balance must be >= 0".into()));
        }
        self.balances.insert(mint.to_string(), value);
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
        Ok(order)
    }

    pub fn pending_automatic_count(&self) -> usize {
        self.orders
            .values()
            .filter(|o| {
                o.status == OrderStatus::Created
                    && o.details
                        .get("origin")
                        .and_then(|v| v.as_str())
                        == Some("AUTOMATIC_WALLET_COPY")
            })
            .count()
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
}
