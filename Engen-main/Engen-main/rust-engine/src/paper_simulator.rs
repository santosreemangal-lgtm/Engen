//! Persistent, deterministic paper-only order simulation.
//!
//! Orders, virtual balances and fills are stored in the existing SQLite schema.
//! There is no signer, private-key input, transaction builder, or broadcast API.

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::str::FromStr;

use bigdecimal::BigDecimal;
use num_traits::Zero;
use rusqlite::{params, Connection, OptionalExtension, Transaction};
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use thiserror::Error;
use uuid::Uuid;

use crate::pnl::{decimal_value, AverageCostLedger, MarketPrice, PnlError};
use crate::safety::block_live_execution;

#[derive(Debug, Error)]
pub enum PaperSimulatorError {
    #[error("SQLite operation failed.")]
    Database(#[from] rusqlite::Error),
    #[error("JSON serialization or parsing failed.")]
    Json(#[from] serde_json::Error),
    #[error("Accounting calculation failed.")]
    Pnl(#[from] PnlError),
    #[error("Invalid paper-simulation input: {0}")]
    InvalidInput(String),
    #[error("Order was not found.")]
    OrderNotFound,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct PaperOrder {
    pub order_id: String,
    pub signal_id: String,
    pub side: String,
    pub asset_mint: String,
    pub quantity: String,
    pub quote_mint: String,
    pub status: String,
    pub reason: Option<String>,
    pub created_at: String,
    pub details: Value,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct PaperFill {
    pub fill_id: String,
    pub order_id: String,
    pub side: String,
    pub asset_mint: String,
    pub quantity: String,
    pub quote_mint: String,
    pub reference_price: String,
    pub execution_price: String,
    pub gross_quote: String,
    pub fee: String,
    pub network_cost: String,
    pub net_quote: String,
    pub currency: String,
    pub price_source: String,
    pub price_timestamp: String,
    pub price_age_seconds: u64,
    pub slippage_bps: u16,
    pub created_at: String,
}

#[derive(Debug, Clone)]
pub struct ExecutionPolicy {
    pub max_price_age_seconds: u64,
    pub slippage_bps: u16,
    pub fee_bps: u16,
    pub network_cost: String,
    pub min_liquidity_usd: String,
    pub max_liquidity_age_seconds: u64,
    pub require_liquidity: bool,
}

impl Default for ExecutionPolicy {
    fn default() -> Self {
        Self {
            max_price_age_seconds: 60,
            slippage_bps: 50,
            fee_bps: 10,
            network_cost: "0".to_string(),
            min_liquidity_usd: "10000".to_string(),
            max_liquidity_age_seconds: 60,
            require_liquidity: true,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PaperPnlSummary {
    pub cost_basis_method: String,
    pub currency: String,
    pub realized_by_mint: BTreeMap<String, Value>,
    pub unrealized_status: String,
    pub unrealized_note: String,
}

/// Inputs for creating one idempotent paper order.
#[derive(Debug, Clone)]
pub struct CreateOrderRequest<'a> {
    pub signal_id: &'a str,
    pub side: &'a str,
    pub asset_mint: &'a str,
    pub quantity: &'a str,
    pub quote_mint: &'a str,
    pub created_at: &'a str,
    pub details: Option<Value>,
}

pub struct PaperSimulator {
    db_path: Option<PathBuf>,
    balances: BTreeMap<String, BigDecimal>,
    orders: BTreeMap<String, PaperOrder>,
    fills: Vec<PaperFill>,
    processed_signals: BTreeSet<String>,
    initial_balance: Option<String>,
    initial_balance_mint: Option<String>,
}

impl PaperSimulator {
    pub fn in_memory(
        initial_balances: &BTreeMap<String, String>,
    ) -> Result<Self, PaperSimulatorError> {
        Self::build(None, initial_balances)
    }

    pub fn open(
        path: impl AsRef<Path>,
        initial_balances: &BTreeMap<String, String>,
    ) -> Result<Self, PaperSimulatorError> {
        Self::build(Some(path.as_ref().to_path_buf()), initial_balances)
    }

    fn build(
        db_path: Option<PathBuf>,
        initial_balances: &BTreeMap<String, String>,
    ) -> Result<Self, PaperSimulatorError> {
        let mut balances = BTreeMap::new();
        for (mint, amount) in initial_balances {
            if mint.trim().is_empty() {
                return Err(PaperSimulatorError::InvalidInput(
                    "Balance token mint is required.".to_string(),
                ));
            }
            balances.insert(mint.clone(), decimal_value(amount, "Initial balance", true)?);
        }

        let mut simulator = Self {
            db_path,
            balances,
            orders: BTreeMap::new(),
            fills: Vec::new(),
            processed_signals: BTreeSet::new(),
            initial_balance: None,
            initial_balance_mint: None,
        };
        if simulator.db_path.is_some() {
            simulator.initialize_and_load()?;
        }
        Ok(simulator)
    }

    fn connect(&self) -> Result<Connection, PaperSimulatorError> {
        let path = self.db_path.as_ref().ok_or_else(|| {
            PaperSimulatorError::InvalidInput("In-memory simulator has no database.".to_string())
        })?;
        let connection = Connection::open(path)?;
        connection.busy_timeout(std::time::Duration::from_secs(10))?;
        connection.execute_batch("PRAGMA foreign_keys=ON; PRAGMA busy_timeout=10000;")?;
        Ok(connection)
    }

    fn initialize_and_load(&mut self) -> Result<(), PaperSimulatorError> {
        if let Some(path) = &self.db_path {
            if let Some(parent) = path.parent() {
                if !parent.as_os_str().is_empty() {
                    std::fs::create_dir_all(parent).map_err(|_| {
                        PaperSimulatorError::InvalidInput(
                            "Unable to create paper database directory.".to_string(),
                        )
                    })?;
                }
            }
        }
        let mut connection = self.connect()?;
        let transaction = connection.transaction()?;
        transaction.execute_batch(
            "
            CREATE TABLE IF NOT EXISTS simulator_meta(
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS simulator_balances(
                mint TEXT PRIMARY KEY, amount TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS simulated_orders(
                order_id TEXT PRIMARY KEY,
                signal_id TEXT NOT NULL UNIQUE,
                side TEXT NOT NULL,
                asset_mint TEXT NOT NULL,
                quantity TEXT NOT NULL,
                quote_mint TEXT NOT NULL,
                status TEXT NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL,
                details_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS simulated_fills(
                fill_id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL UNIQUE REFERENCES simulated_orders(order_id),
                asset_mint TEXT NOT NULL,
                quantity TEXT NOT NULL,
                reference_price TEXT NOT NULL,
                execution_price TEXT NOT NULL,
                gross_quote TEXT NOT NULL,
                fee TEXT NOT NULL,
                network_cost TEXT NOT NULL,
                net_quote TEXT NOT NULL,
                currency TEXT NOT NULL,
                price_source TEXT NOT NULL,
                price_timestamp TEXT NOT NULL,
                price_age_seconds INTEGER NOT NULL,
                slippage_bps INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            ",
        )?;

        let initialized: Option<String> = transaction
            .query_row(
                "SELECT value FROM simulator_meta WHERE key='initialized'",
                [],
                |row| row.get(0),
            )
            .optional()?;

        if initialized.is_none() {
            for (mint, amount) in &self.balances {
                transaction.execute(
                    "INSERT INTO simulator_balances(mint,amount) VALUES(?1,?2)",
                    params![mint, amount.to_string()],
                )?;
            }
            transaction.execute(
                "INSERT INTO simulator_meta(key,value) VALUES('initialized','1')",
                [],
            )?;
        } else {
            self.balances.clear();
            let mut statement =
                transaction.prepare("SELECT mint,amount FROM simulator_balances ORDER BY mint")?;
            let rows = statement.query_map([], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
            })?;
            for row in rows {
                let (mint, amount) = row?;
                self.balances.insert(
                    mint,
                    BigDecimal::from_str(&amount).map_err(|_| {
                        rusqlite::Error::FromSqlConversionFailure(
                            1,
                            rusqlite::types::Type::Text,
                            Box::new(std::io::Error::new(
                                std::io::ErrorKind::InvalidData,
                                "Invalid decimal value in paper balance.",
                            )),
                        )
                    })?,
                );
            }
        }
        transaction.commit()?;

        self.load_orders_and_fills()?;
        self.initial_balance = self.read_meta("initial_balance")?;
        if self.initial_balance.is_some() {
            self.initial_balance_mint = Some("USD".to_string());
        }
        Ok(())
    }

    fn load_orders_and_fills(&mut self) -> Result<(), PaperSimulatorError> {
        let connection = self.connect()?;
        self.orders.clear();
        self.processed_signals.clear();
        self.fills.clear();

        {
            let mut statement = connection.prepare(
                "SELECT order_id,signal_id,side,asset_mint,quantity,quote_mint,status,reason,created_at,details_json
                 FROM simulated_orders ORDER BY rowid",
            )?;
            let rows = statement.query_map([], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                    row.get::<_, String>(4)?,
                    row.get::<_, String>(5)?,
                    row.get::<_, String>(6)?,
                    row.get::<_, Option<String>>(7)?,
                    row.get::<_, String>(8)?,
                    row.get::<_, String>(9)?,
                ))
            })?;
            for row in rows {
                let (order_id, signal_id, side, asset_mint, quantity, quote_mint, status, reason, created_at, details_json) = row?;
                let details = serde_json::from_str(&details_json)?;
                self.processed_signals.insert(signal_id.clone());
                self.orders.insert(order_id.clone(), PaperOrder {
                    order_id, signal_id, side, asset_mint, quantity, quote_mint, status,
                    reason, created_at, details,
                });
            }
        }

        {
            let mut statement = connection.prepare(
                "SELECT f.fill_id,f.order_id,o.side,f.asset_mint,f.quantity,o.quote_mint,
                        f.reference_price,f.execution_price,f.gross_quote,f.fee,f.network_cost,
                        f.net_quote,f.currency,f.price_source,f.price_timestamp,
                        f.price_age_seconds,f.slippage_bps,f.created_at
                 FROM simulated_fills f JOIN simulated_orders o ON o.order_id=f.order_id
                 ORDER BY f.rowid",
            )?;
            let rows = statement.query_map([], |row| {
                Ok(PaperFill {
                    fill_id: row.get(0)?,
                    order_id: row.get(1)?,
                    side: row.get(2)?,
                    asset_mint: row.get(3)?,
                    quantity: row.get(4)?,
                    quote_mint: row.get(5)?,
                    reference_price: row.get(6)?,
                    execution_price: row.get(7)?,
                    gross_quote: row.get(8)?,
                    fee: row.get(9)?,
                    network_cost: row.get(10)?,
                    net_quote: row.get(11)?,
                    currency: row.get(12)?,
                    price_source: row.get(13)?,
                    price_timestamp: row.get(14)?,
                    price_age_seconds: row.get::<_, i64>(15)?.max(0) as u64,
                    slippage_bps: row.get::<_, i64>(16)?.clamp(0, u16::MAX as i64) as u16,
                    created_at: row.get(17)?,
                })
            })?;
            for row in rows { self.fills.push(row?); }
        }

        Ok(())
    }

    fn read_meta(&self, key: &str) -> Result<Option<String>, PaperSimulatorError> {
        if self.db_path.is_none() { return Ok(None); }
        let connection = self.connect()?;
        Ok(connection.query_row(
            "SELECT value FROM simulator_meta WHERE key=?1",
            [key],
            |row| row.get(0),
        ).optional()?)
    }

    fn persist(
        &self,
        order: &PaperOrder,
        balances: Option<&BTreeMap<String, BigDecimal>>,
        fill: Option<&PaperFill>,
    ) -> Result<(), PaperSimulatorError> {
        if self.db_path.is_none() { return Ok(()); }
        let mut connection = self.connect()?;
        let transaction = connection.transaction()?;
        Self::persist_order(&transaction, order)?;
        if let Some(balances) = balances {
            for (mint, amount) in balances {
                transaction.execute(
                    "INSERT INTO simulator_balances(mint,amount) VALUES(?1,?2)
                     ON CONFLICT(mint) DO UPDATE SET amount=excluded.amount",
                    params![mint, amount.to_string()],
                )?;
            }
        }
        if let Some(fill) = fill {
            transaction.execute(
                "INSERT INTO simulated_fills(
                    fill_id,order_id,asset_mint,quantity,reference_price,execution_price,
                    gross_quote,fee,network_cost,net_quote,currency,price_source,price_timestamp,
                    price_age_seconds,slippage_bps,created_at
                 ) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15,?16)",
                params![
                    fill.fill_id, fill.order_id, fill.asset_mint, fill.quantity,
                    fill.reference_price, fill.execution_price, fill.gross_quote, fill.fee,
                    fill.network_cost, fill.net_quote, fill.currency, fill.price_source,
                    fill.price_timestamp, fill.price_age_seconds as i64,
                    i64::from(fill.slippage_bps), fill.created_at
                ],
            )?;
        }
        transaction.commit()?;
        Ok(())
    }

    fn persist_order(
        transaction: &Transaction<'_>,
        order: &PaperOrder,
    ) -> Result<(), PaperSimulatorError> {
        let details = serde_json::to_string(&order.details)?;
        transaction.execute(
            "INSERT INTO simulated_orders(
                order_id,signal_id,side,asset_mint,quantity,quote_mint,status,reason,created_at,details_json
             ) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10)
             ON CONFLICT(order_id) DO UPDATE SET
                quantity=excluded.quantity,status=excluded.status,reason=excluded.reason,
                details_json=excluded.details_json",
            params![
                order.order_id, order.signal_id, order.side, order.asset_mint, order.quantity,
                order.quote_mint, order.status, order.reason, order.created_at, details
            ],
        )?;
        Ok(())
    }

    pub fn balances(&self) -> &BTreeMap<String, BigDecimal> { &self.balances }
    pub fn orders(&self) -> &BTreeMap<String, PaperOrder> { &self.orders }
    pub fn fills(&self) -> &[PaperFill] { &self.fills }

    pub fn create_order(
        &mut self,
        request: CreateOrderRequest<'_>,
    ) -> Result<Value, PaperSimulatorError> {
        let CreateOrderRequest { signal_id, side, asset_mint, quantity, quote_mint, created_at, details } = request;
        if signal_id.trim().is_empty() {
            return Err(PaperSimulatorError::InvalidInput("Signal ID is required for deduplication.".to_string()));
        }
        if self.processed_signals.contains(signal_id) {
            return Ok(json!({"status":"DUPLICATE","signal_id":signal_id}));
        }
        if !matches!(side, "BUY" | "SELL") {
            return Err(PaperSimulatorError::InvalidInput("Side must be BUY or SELL.".to_string()));
        }
        if asset_mint.trim().is_empty() || quote_mint.trim().is_empty() || asset_mint == quote_mint {
            return Err(PaperSimulatorError::InvalidInput("Distinct asset and quote mints are required.".to_string()));
        }
        let qty = decimal_value(quantity, "Order quantity", false)?;
        crate::pnl::parse_timestamp(created_at)?;

        // Check the persistent unique key before mutating in-memory state. SQLite's
        // UNIQUE constraint remains the final idempotency boundary across processes.
        if self.db_path.is_some() {
            let connection = self.connect()?;
            let existing: Option<String> = connection.query_row(
                "SELECT order_id FROM simulated_orders WHERE signal_id=?1",
                [signal_id],
                |row| row.get(0),
            ).optional()?;
            if existing.is_some() {
                self.processed_signals.insert(signal_id.to_string());
                return Ok(json!({"status":"DUPLICATE","signal_id":signal_id}));
            }
        }

        let mut order_details = Map::new();
        order_details.insert("mode".to_string(), json!("PAPER_SIMULATION"));
        order_details.insert("real_trading".to_string(), json!(false));
        order_details.insert("transaction_broadcast".to_string(), json!(false));
        if let Some(Value::Object(extra)) = details {
            for (key, value) in extra { order_details.insert(key, value); }
        }
        let order = PaperOrder {
            order_id: Uuid::new_v4().to_string(),
            signal_id: signal_id.to_string(),
            side: side.to_string(),
            asset_mint: asset_mint.to_string(),
            quantity: qty.to_string(),
            quote_mint: quote_mint.to_string(),
            status: "CREATED".to_string(),
            reason: None,
            created_at: created_at.to_string(),
            details: Value::Object(order_details),
        };

        if let Err(error) = self.persist(&order, None, None) {
            if matches!(&error, PaperSimulatorError::Database(rusqlite::Error::SqliteFailure(code, _)) if code.code == rusqlite::ErrorCode::ConstraintViolation) {
                return Ok(json!({"status":"DUPLICATE","signal_id":signal_id}));
            }
            return Err(error);
        }
        self.processed_signals.insert(signal_id.to_string());
        self.orders.insert(order.order_id.clone(), order.clone());
        Ok(serde_json::to_value(order)?)
    }

    pub fn set_initial_virtual_balance(
        &mut self,
        amount: &str,
        mint: &str,
    ) -> Result<Value, PaperSimulatorError> {
        if mint.trim().is_empty() || mint.trim().len() > 128 {
            return Err(PaperSimulatorError::InvalidInput("A token mint or paper asset symbol (max 128 characters) is required.".to_string()));
        }
        let value = decimal_value(amount, "Initial virtual balance", true)?;
        let stored = match self.read_meta("initial_balance")? {
            Some(value) => Some(value),
            None => self.initial_balance.clone(),
        };

        if let Some(existing) = stored {
            let old = decimal_value(&existing, "Stored initial balance", true)?;
            if old != value || self.initial_balance_mint.as_deref().unwrap_or("USD") != mint {
                return Err(PaperSimulatorError::InvalidInput(
                    "Starting balance has already been set and cannot be changed.".to_string(),
                ));
            }
            if self.balances.get(mint) != Some(&value) {
                return Err(PaperSimulatorError::InvalidInput(
                    "The stored starting balance does not match this asset balance.".to_string(),
                ));
            }
            return Ok(json!({"mint":mint,"initial_balance":value.to_string(),"balance":value.to_string(),"simulated":true,"real_trading":false,"transaction_broadcast":false}));
        }

        if self.orders.values().any(|order| order.status == "FILLED") {
            return Err(PaperSimulatorError::InvalidInput(
                "Starting balance cannot be changed after a paper order has filled.".to_string(),
            ));
        }

        if self.db_path.is_some() {
            let mut connection = self.connect()?;
            let transaction = connection.transaction()?;
            transaction.execute(
                "INSERT INTO simulator_balances(mint,amount) VALUES(?1,?2)
                 ON CONFLICT(mint) DO UPDATE SET amount=excluded.amount",
                params![mint, value.to_string()],
            )?;
            transaction.execute(
                "INSERT INTO simulator_meta(key,value) VALUES('initial_balance',?1)
                 ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [value.to_string()],
            )?;
            transaction.commit()?;
        }
        self.balances.insert(mint.to_string(), value.clone());
        self.initial_balance = Some(value.to_string());
        self.initial_balance_mint = Some(mint.to_string());
        Ok(json!({"mint":mint,"initial_balance":value.to_string(),"balance":value.to_string(),"simulated":true,"real_trading":false,"transaction_broadcast":false}))
    }

    pub fn set_virtual_balance(&mut self, amount: &str, mint: &str) -> Result<Value, PaperSimulatorError> {
        if mint.trim().is_empty() || mint.trim().len() > 128 {
            return Err(PaperSimulatorError::InvalidInput("A virtual balance currency is required.".to_string()));
        }
        let value = decimal_value(amount, "Virtual balance", true)?;
        self.persist_balance(mint, &value, true)?;
        self.balances.insert(mint.to_string(), value.clone());
        Ok(json!({"mint":mint,"balance":value.to_string(),"simulated":true,"real_trading":false,"transaction_broadcast":false}))
    }

    pub fn add_virtual_balance(&mut self, mint: &str, amount: &str) -> Result<Value, PaperSimulatorError> {
        if mint.trim().is_empty() || mint.trim().len() > 128 {
            return Err(PaperSimulatorError::InvalidInput("A token mint or paper asset symbol (max 128 characters) is required.".to_string()));
        }
        let value = decimal_value(amount, "Virtual funding amount", false)?;
        let updated = self.balances.get(mint).cloned().unwrap_or_else(BigDecimal::zero) + &value;
        self.persist_balance(mint, &updated, false)?;
        self.balances.insert(mint.to_string(), updated.clone());
        Ok(json!({"mint":mint,"added":value.to_string(),"balance":updated.to_string(),"simulated":true,"real_trading":false,"transaction_broadcast":false}))
    }

    fn persist_balance(&self, mint: &str, value: &BigDecimal, set_initial_if_missing: bool) -> Result<(), PaperSimulatorError> {
        if self.db_path.is_none() { return Ok(()); }
        let mut connection = self.connect()?;
        let transaction = connection.transaction()?;
        transaction.execute(
            "INSERT INTO simulator_balances(mint,amount) VALUES(?1,?2)
             ON CONFLICT(mint) DO UPDATE SET amount=excluded.amount",
            params![mint, value.to_string()],
        )?;
        if set_initial_if_missing {
            transaction.execute(
                "INSERT INTO simulator_meta(key,value) VALUES('initial_balance',?1)
                 ON CONFLICT(key) DO NOTHING",
                [value.to_string()],
            )?;
        }
        transaction.commit()?;
        Ok(())
    }

    pub fn cancel_order(&mut self, order_id: &str) -> Result<Value, PaperSimulatorError> {
        let mut order = self.orders.get(order_id).cloned().ok_or(PaperSimulatorError::OrderNotFound)?;
        if order.status != "CREATED" {
            return Ok(json!({"status":"REJECTED","reason":"Only unexecuted orders can be cancelled."}));
        }
        order.status = "CANCELLED".to_string();
        order.reason = Some("Cancelled before simulated execution.".to_string());
        self.persist(&order, None, None)?;
        self.orders.insert(order_id.to_string(), order.clone());
        Ok(serde_json::to_value(order)?)
    }

    /// Update an unexecuted paper order quantity and persist it atomically.
    pub fn set_order_quantity(
        &mut self,
        order_id: &str,
        quantity: &str,
    ) -> Result<bool, PaperSimulatorError> {
        let mut order = self
            .orders
            .get(order_id)
            .cloned()
            .ok_or(PaperSimulatorError::OrderNotFound)?;
        if order.status != "CREATED" {
            return Ok(false);
        }
        let parsed = decimal_value(quantity, "Order quantity", false)?;
        order.quantity = parsed.to_string();
        self.persist(&order, None, None)?;
        self.orders.insert(order_id.to_string(), order);
        Ok(true)
    }

    pub fn execute_order(
        &mut self,
        order_id: &str,
        market_price: Option<&MarketPrice>,
        now: &str,
        policy: &ExecutionPolicy,
        rpc_ok: bool,
    ) -> Result<Value, PaperSimulatorError> {
        let mut order = self.orders.get(order_id).cloned().ok_or(PaperSimulatorError::OrderNotFound)?;
        if order.status != "CREATED" {
            return Ok(json!({"status":"REJECTED","reason":"Order is not awaiting execution."}));
        }
        if !rpc_ok {
            return self.wait_for_market_data(&mut order, "RPC/provider data unavailable; retrying without a fill.");
        }

        let Some(price) = market_price else {
            return self.wait_for_market_data(&mut order, "Market price unavailable; retrying.");
        };
        price.validate()?;
        if price.mint != order.asset_mint || !price.is_fresh(policy.max_price_age_seconds, Some(now))? {
            return self.wait_for_market_data(&mut order, "Market price is stale or for a different token; retrying without a fill.");
        }

        if let Some(liquidity) = &price.liquidity_usd {
            let liquidity_age = price.liquidity_age_seconds(Some(now))?.unwrap_or(u64::MAX);
            let minimum = decimal_value(&policy.min_liquidity_usd, "Minimum USD liquidity", true)?;
            if liquidity_age > policy.max_liquidity_age_seconds || liquidity < &minimum {
                return self.wait_for_market_data(&mut order, "Liquidity is stale or below the configured USD minimum; retrying without a fill.");
            }
        } else if policy.require_liquidity {
            return self.wait_for_market_data(&mut order, "Liquidity data unavailable; retrying without a fill.");
        }

        if policy.slippage_bps > 10_000 || policy.fee_bps > 10_000 {
            return Err(PaperSimulatorError::InvalidInput("Slippage and fee basis points must be between 0 and 10000.".to_string()));
        }
        let mut quantity = decimal_value(&order.quantity, "Order quantity", false)?;
        if let Some(target) = order.details.get("target_notional_usd").and_then(Value::as_str) {
            let target_value = decimal_value(target, "Target USD paper order size", false)?;
            quantity = target_value / &price.price;
            order.quantity = quantity.to_string();
        }

        let reference_price = price.price.clone();
        let slip_factor = BigDecimal::from(10_000_i64 + i64::from(policy.slippage_bps))
            / BigDecimal::from(10_000_i64);
        let execution_price = if order.side == "BUY" {
            &reference_price * slip_factor
        } else {
            &reference_price * (BigDecimal::from(10_000_i64 - i64::from(policy.slippage_bps)) / BigDecimal::from(10_000_i64))
        };
        let gross = &quantity * &execution_price;
        let fee = &gross * BigDecimal::from(i64::from(policy.fee_bps)) / BigDecimal::from(10_000_i64);
        let network = decimal_value(&policy.network_cost, "Network cost", true)?;
        let total_cost_or_proceeds = if order.side == "BUY" {
            &gross + &fee + &network
        } else {
            &gross - &fee - &network
        };
        if total_cost_or_proceeds < BigDecimal::zero() {
            return self.reject_order(&mut order, "Costs exceed simulated sale proceeds.");
        }

        let mut balances = self.balances.clone();
        let asset_balance = balances.get(&order.asset_mint).cloned().unwrap_or_else(BigDecimal::zero);
        let quote_balance = balances.get(&order.quote_mint).cloned().unwrap_or_else(BigDecimal::zero);
        if order.side == "BUY" {
            if quote_balance < total_cost_or_proceeds {
                return self.reject_order(&mut order, "Insufficient simulated quote balance.");
            }
            balances.insert(order.quote_mint.clone(), quote_balance - &total_cost_or_proceeds);
            balances.insert(order.asset_mint.clone(), asset_balance + &quantity);
        } else {
            if asset_balance < quantity {
                return self.reject_order(&mut order, "Insufficient simulated asset balance.");
            }
            balances.insert(order.asset_mint.clone(), asset_balance - &quantity);
            balances.insert(order.quote_mint.clone(), quote_balance + &total_cost_or_proceeds);
        }

        let fill = PaperFill {
            fill_id: Uuid::new_v4().to_string(),
            order_id: order.order_id.clone(),
            side: order.side.clone(),
            asset_mint: order.asset_mint.clone(),
            quantity: quantity.to_string(),
            quote_mint: order.quote_mint.clone(),
            reference_price: reference_price.to_string(),
            execution_price: execution_price.to_string(),
            gross_quote: gross.to_string(),
            fee: fee.to_string(),
            network_cost: network.to_string(),
            net_quote: total_cost_or_proceeds.to_string(),
            currency: "USD".to_string(),
            price_source: price.source.clone(),
            price_timestamp: price.observed_at.clone(),
            price_age_seconds: price.age_seconds(Some(now))?,
            slippage_bps: policy.slippage_bps,
            created_at: now.to_string(),
        };

        order.status = "FILLED".to_string();
        order.reason = None;
        set_detail(&mut order.details, "execution_state", json!("FILLED"));
        set_detail(&mut order.details, "reference_price", json!(reference_price.to_string()));
        set_detail(&mut order.details, "execution_price", json!(execution_price.to_string()));
        set_detail(&mut order.details, "fee", json!(fee.to_string()));
        set_detail(&mut order.details, "network_cost", json!(network.to_string()));
        set_detail(&mut order.details, "fill_id", json!(fill.fill_id));

        // This single SQLite transaction commits the order, balances, and fill together.
        self.persist(&order, Some(&balances), Some(&fill))?;
        self.orders.insert(order_id.to_string(), order.clone());
        self.balances = balances;
        self.fills.push(fill.clone());

        Ok(json!({
            "status":"FILLED","simulated":true,"real_trading":false,
            "transaction_broadcast":false,
            "order":order,
            "fill":fill
        }))
    }

    fn wait_for_market_data(&mut self, order: &mut PaperOrder, reason: &str) -> Result<Value, PaperSimulatorError> {
        order.status = "CREATED".to_string();
        order.reason = Some(reason.to_string());
        set_detail(&mut order.details, "execution_state", json!("WAITING_FOR_MARKET_DATA"));
        self.persist(order, None, None)?;
        self.orders.insert(order.order_id.clone(), order.clone());
        Ok(json!({
            "status":"WAITING_FOR_MARKET_DATA","reason":reason,
            "simulated":true,"real_trading":false,"transaction_broadcast":false
        }))
    }

    fn reject_order(&mut self, order: &mut PaperOrder, reason: &str) -> Result<Value, PaperSimulatorError> {
        order.status = "REJECTED".to_string();
        order.reason = Some(reason.to_string());
        set_detail(&mut order.details, "execution_state", json!("REJECTED"));
        self.persist(order, None, None)?;
        self.orders.insert(order.order_id.clone(), order.clone());
        Ok(json!({
            "status":"REJECTED","reason":reason,
            "simulated":true,"real_trading":false,"transaction_broadcast":false
        }))
    }

    pub fn pnl_summary(&self) -> Result<PaperPnlSummary, PaperSimulatorError> {
        let mut ledger = AverageCostLedger::new();
        let mut realized: BTreeMap<String, (BigDecimal, bool)> = BTreeMap::new();
        for fill in &self.fills {
            match fill.side.as_str() {
                "BUY" => {
                    ledger.buy(
                        &fill.asset_mint, &fill.quantity, &fill.execution_price,
                        &fill.fee, false, &fill.network_cost, Some(&fill.created_at),
                    )?;
                }
                "SELL" => {
                    let tracked = decimal_value(&ledger.snapshot(&fill.asset_mint).quantity, "Tracked quantity", true)?;
                    let sold = decimal_value(&fill.quantity, "Quantity", false)?;
                    if sold > tracked {
                        let missing = &sold - &tracked;
                        ledger.deposit(&fill.asset_mint, &missing.to_string(), None, Some(&fill.created_at))?;
                    }
                    let event = ledger.sell(
                        &fill.asset_mint, &fill.quantity, &fill.execution_price,
                        &fill.fee, false, &fill.network_cost, Some(&fill.created_at),
                    )?;
                    let entry = realized.entry(fill.asset_mint.clone())
                        .or_insert((BigDecimal::zero(), true));
                    match event.realized_pnl {
                        Some(value) => entry.0 += BigDecimal::from_str(&value)
                            .map_err(|_| PaperSimulatorError::InvalidInput("Invalid P&L result.".to_string()))?,
                        None => entry.1 = false,
                    }
                }
                _ => return Err(PaperSimulatorError::InvalidInput("Persisted fill has an invalid side.".to_string())),
            }
        }
        let realized_by_mint = realized.into_iter().map(|(mint,(value,complete))| {
            (mint, json!({
                "pnl": if complete { Some(value.to_string()) } else { None },
                "status": if complete { "calculated" } else { "unknown_cost_basis" },
                "currency":"USD"
            }))
        }).collect();

        Ok(PaperPnlSummary {
            cost_basis_method: "average_cost".to_string(),
            currency: "USD".to_string(),
            realized_by_mint,
            unrealized_status: "unavailable_no_verified_fresh_market_price".to_string(),
            unrealized_note: "No verified market-price provider is connected to the simulator.".to_string(),
        })
    }

    pub fn report(&self) -> Result<Value, PaperSimulatorError> {
        let balances = self.balances.iter().map(|(mint,amount)| (mint.clone(), json!(amount.to_string()))).collect::<Map<_,_>>();
        Ok(json!({
            "mode":"PAPER_SIMULATION",
            "simulated":true,
            "real_wallet_balances_included":false,
            "real_trading":false,
            "transaction_broadcast":false,
            "balances":balances,
            "initial_balance":self.initial_balance,
            "orders":self.orders.values().collect::<Vec<_>>(),
            "fills":self.fills,
            "pnl":self.pnl_summary()?
        }))
    }

    /// Always fails. A future live adapter is intentionally outside this crate.
    pub fn assert_paper_only(&self) -> Result<(), crate::safety::LiveExecutionDisabled> {
        block_live_execution()
    }
}

fn set_detail(details: &mut Value, key: &str, value: Value) {
    if !details.is_object() {
        *details = Value::Object(Map::new());
    }
    if let Some(object) = details.as_object_mut() {
        object.insert(key.to_string(), value);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    fn now() -> &'static str { "2026-11-09T12:00:00Z" }

    fn virtual_cash() -> BTreeMap<String,String> {
        BTreeMap::from([("USD".to_string(),"100".to_string())])
    }

    fn price(mint: &str) -> MarketPrice {
        MarketPrice::new(
            mint,"10","USD","test-feed",now(),Some("50000"),
            Some("test-liquidity".to_string()),Some(now().to_string())
        ).unwrap()
    }

    fn buy_order(sim: &mut PaperSimulator, signal: &str) -> String {
        sim.create_order(CreateOrderRequest {
            signal_id: signal, side: "BUY", asset_mint: "TOKEN", quantity: "1",
            quote_mint: "USD", created_at: now(), details: Some(json!({
                "target_notional_usd":"20","origin":"AUTOMATIC_WALLET_COPY"
            })),
        }).unwrap()["order_id"].as_str().unwrap().to_string()
    }

    #[test]
    fn fills_a_paper_buy_with_decimal_accounting() {
        let mut sim=PaperSimulator::in_memory(&virtual_cash()).unwrap();
        let id=buy_order(&mut sim,"signal-1");
        let result=sim.execute_order(&id,Some(&price("TOKEN")),now(),&ExecutionPolicy::default(),true).unwrap();
        assert_eq!(result["status"],"FILLED");
        assert_eq!(sim.balances()["USD"].to_string(),"79.8799");
        assert_eq!(sim.balances()["TOKEN"].to_string(),"2");
        assert_eq!(sim.fills().len(),1);
        assert_eq!(result["transaction_broadcast"],false);
    }

    #[test]
    fn duplicate_signal_cannot_create_a_second_order() {
        let mut sim=PaperSimulator::in_memory(&virtual_cash()).unwrap();
        let first=buy_order(&mut sim,"same-signal");
        let second=sim.create_order(CreateOrderRequest {
            signal_id: "same-signal", side: "BUY", asset_mint: "TOKEN", quantity: "1",
            quote_mint: "USD", created_at: now(), details: None,
        }).unwrap();
        assert_eq!(second["status"],"DUPLICATE");
        assert_eq!(sim.orders().len(),1);
        assert!(!first.is_empty());
    }

    #[test]
    fn stale_price_waits_without_fabricating_a_fill() {
        let mut sim=PaperSimulator::in_memory(&virtual_cash()).unwrap();
        let id=buy_order(&mut sim,"stale-price");
        let stale=MarketPrice::new(
            "TOKEN","10","USD","test-feed","2026-11-09T11:58:00Z",Some("50000"),
            Some("test-liquidity".to_string()),Some(now().to_string())
        ).unwrap();
        let outcome=sim.execute_order(&id,Some(&stale),now(),&ExecutionPolicy::default(),true).unwrap();
        assert_eq!(outcome["status"],"WAITING_FOR_MARKET_DATA");
        assert!(sim.fills().is_empty());
    }

    #[test]
    fn sell_cannot_exceed_virtual_token_balance() {
        let mut balances=virtual_cash();
        balances.insert("TOKEN".to_string(),"1".to_string());
        let mut sim=PaperSimulator::in_memory(&balances).unwrap();
        let order=sim.create_order(CreateOrderRequest {
            signal_id: "sell-too-much", side: "SELL", asset_mint: "TOKEN", quantity: "2",
            quote_mint: "USD", created_at: now(), details: None,
        }).unwrap();
        let id=order["order_id"].as_str().unwrap();
        let result=sim.execute_order(id,Some(&price("TOKEN")),now(),&ExecutionPolicy::default(),true).unwrap();
        assert_eq!(result["status"],"REJECTED");
        assert_eq!(sim.balances()["TOKEN"].to_string(),"1");
        assert!(sim.fills().is_empty());
    }

    #[test]
    fn restart_recovers_order_balances_and_fill() {
        let dir=tempdir().unwrap();
        let path=dir.path().join("paper.db");
        let id;
        {
            let mut sim=PaperSimulator::open(&path,&virtual_cash()).unwrap();
            id=buy_order(&mut sim,"restart-signal");
            sim.execute_order(&id,Some(&price("TOKEN")),now(),&ExecutionPolicy::default(),true).unwrap();
        }
        let sim=PaperSimulator::open(&path,&BTreeMap::new()).unwrap();
        assert_eq!(sim.orders()[&id].status,"FILLED");
        assert_eq!(sim.fills().len(),1);
        assert_eq!(sim.balances()["TOKEN"].to_string(),"2");
    }

    #[test]
    fn failed_provider_does_not_create_a_fill() {
        let mut sim=PaperSimulator::in_memory(&virtual_cash()).unwrap();
        let id=buy_order(&mut sim,"rpc-fail");
        let result=sim.execute_order(&id,Some(&price("TOKEN")),now(),&ExecutionPolicy::default(),false).unwrap();
        assert_eq!(result["status"],"WAITING_FOR_MARKET_DATA");
        assert_eq!(sim.balances()["USD"].to_string(),"100");
    }
}
