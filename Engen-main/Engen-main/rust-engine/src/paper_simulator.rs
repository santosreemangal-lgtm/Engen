//! Persistent, deterministic paper-only order simulation.
//!
//! Orders, virtual balances and fills are stored in the existing SQLite schema.
//! There is no signer, private-key input, transaction builder, or broadcast API.

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::str::FromStr;

use bigdecimal::rounding::RoundingMode;
use bigdecimal::BigDecimal;
use num_traits::Zero;
use rusqlite::{params, Connection, OptionalExtension, Transaction, TransactionBehavior};
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
    #[error("Paper balance changed in another simulator instance; reload before retrying.")]
    ConcurrentStateConflict,
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
    /// Fixed base-network fee estimate charged in the paper quote currency.
    pub base_network_fee: String,
    /// Configured priority-fee estimate charged in the paper quote currency.
    pub priority_fee: String,
    /// Sum of base_network_fee and priority_fee (kept for existing P&L consumers).
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
    /// Base network-fee estimate in USD per paper fill.
    pub network_cost: String,
    /// Priority-fee estimate in USD per paper fill; this is an explicit
    /// simulation input, not a live Solana priority-fee quote.
    pub priority_fee_usd: String,
    pub min_liquidity_usd: String,
    pub min_order_value_usd: String,
    pub min_order_quantity: String,
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
            priority_fee_usd: "0".to_string(),
            min_liquidity_usd: "10000".to_string(),
            min_order_value_usd: "1".to_string(),
            min_order_quantity: "0.000000000000000001".to_string(),
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

#[derive(Debug, Clone)]
struct BalanceReservation {
    mint: String,
    amount: BigDecimal,
}

pub struct PaperSimulator {
    db_path: Option<PathBuf>,
    balances: BTreeMap<String, BigDecimal>,
    reservations: BTreeMap<String, BalanceReservation>,
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
            balances.insert(
                mint.clone(),
                decimal_value(amount, "Initial balance", true)?,
            );
        }

        let mut simulator = Self {
            db_path,
            balances,
            reservations: BTreeMap::new(),
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
            CREATE TABLE IF NOT EXISTS simulator_reservations(
                order_id TEXT PRIMARY KEY REFERENCES simulated_orders(order_id) ON DELETE CASCADE,
                mint TEXT NOT NULL,
                amount TEXT NOT NULL
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
                base_network_fee TEXT NOT NULL DEFAULT '0',
                priority_fee TEXT NOT NULL DEFAULT '0',
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

        // Additive migration keeps existing paper-fill databases readable.
        let has_base_network_fee: bool = transaction.query_row(
            "SELECT EXISTS(
                SELECT 1 FROM pragma_table_info('simulated_fills')
                WHERE name='base_network_fee'
            )",
            [],
            |row| row.get(0),
        )?;
        if !has_base_network_fee {
            transaction.execute_batch(
                "ALTER TABLE simulated_fills
                 ADD COLUMN base_network_fee TEXT NOT NULL DEFAULT '0'",
            )?;
        }
        let has_priority_fee: bool = transaction.query_row(
            "SELECT EXISTS(
                SELECT 1 FROM pragma_table_info('simulated_fills')
                WHERE name='priority_fee'
            )",
            [],
            |row| row.get(0),
        )?;
        if !has_priority_fee {
            transaction.execute_batch(
                "ALTER TABLE simulated_fills
                 ADD COLUMN priority_fee TEXT NOT NULL DEFAULT '0'",
            )?;
        }

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
        transaction.execute(
            "DELETE FROM simulator_reservations
             WHERE order_id NOT IN (
                 SELECT order_id FROM simulated_orders WHERE status='CREATED'
             )",
            [],
        )?;
        transaction.commit()?;

        self.load_orders_and_fills()?;
        self.load_reservations()?;
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
                let (
                    order_id,
                    signal_id,
                    side,
                    asset_mint,
                    quantity,
                    quote_mint,
                    status,
                    reason,
                    created_at,
                    details_json,
                ) = row?;
                let details = serde_json::from_str(&details_json)?;
                self.processed_signals.insert(signal_id.clone());
                self.orders.insert(
                    order_id.clone(),
                    PaperOrder {
                        order_id,
                        signal_id,
                        side,
                        asset_mint,
                        quantity,
                        quote_mint,
                        status,
                        reason,
                        created_at,
                        details,
                    },
                );
            }
        }

        {
            let mut statement = connection.prepare(
                "SELECT f.fill_id,f.order_id,o.side,f.asset_mint,f.quantity,o.quote_mint,
                        f.reference_price,f.execution_price,f.gross_quote,f.fee,f.network_cost,
                        f.net_quote,f.currency,f.price_source,f.price_timestamp,
                        f.price_age_seconds,f.slippage_bps,f.created_at,
                        f.base_network_fee,f.priority_fee
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
                    base_network_fee: row.get(18)?,
                    priority_fee: row.get(19)?,
                })
            })?;
            for row in rows {
                self.fills.push(row?);
            }
        }

        Ok(())
    }

    fn load_reservations(&mut self) -> Result<(), PaperSimulatorError> {
        self.reservations.clear();
        if self.db_path.is_none() {
            return Ok(());
        }
        let connection = self.connect()?;
        let mut statement = connection.prepare(
            "SELECT r.order_id,r.mint,r.amount
             FROM simulator_reservations r
             JOIN simulated_orders o ON o.order_id=r.order_id
             WHERE o.status='CREATED'
             ORDER BY o.rowid",
        )?;
        let rows = statement.query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
            ))
        })?;
        for row in rows {
            let (order_id, mint, raw_amount) = row?;
            let amount = decimal_value(&raw_amount, "Persisted reservation amount", false)?;
            self.reservations
                .insert(order_id, BalanceReservation { mint, amount });
        }
        Ok(())
    }

    fn read_meta(&self, key: &str) -> Result<Option<String>, PaperSimulatorError> {
        if self.db_path.is_none() {
            return Ok(None);
        }
        let connection = self.connect()?;
        Ok(connection
            .query_row(
                "SELECT value FROM simulator_meta WHERE key=?1",
                [key],
                |row| row.get(0),
            )
            .optional()?)
    }

    fn persist(
        &self,
        order: &PaperOrder,
        balances: Option<&BTreeMap<String, BigDecimal>>,
        fill: Option<&PaperFill>,
    ) -> Result<(), PaperSimulatorError> {
        self.persist_with_reservation_release(order, balances, fill, false)
    }

    fn persist_releasing_reservation(
        &self,
        order: &PaperOrder,
        balances: Option<&BTreeMap<String, BigDecimal>>,
        fill: Option<&PaperFill>,
    ) -> Result<(), PaperSimulatorError> {
        self.persist_with_reservation_release(order, balances, fill, true)
    }

    fn persist_with_reservation_release(
        &self,
        order: &PaperOrder,
        balances: Option<&BTreeMap<String, BigDecimal>>,
        fill: Option<&PaperFill>,
        release_reservation: bool,
    ) -> Result<(), PaperSimulatorError> {
        if self.db_path.is_none() {
            return Ok(());
        }
        let mut connection = self.connect()?;
        let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
        Self::persist_order(&transaction, order)?;

        if release_reservation {
            transaction.execute(
                "DELETE FROM simulator_reservations WHERE order_id=?1",
                [&order.order_id],
            )?;
        }

        if let Some(balances) = balances {
            // Reject stale simulator snapshots. This is the final guard against
            // two independently opened simulator instances spending the same cash.
            for (mint, amount) in balances {
                let stored_raw: Option<String> = transaction
                    .query_row(
                        "SELECT amount FROM simulator_balances WHERE mint=?1",
                        [mint],
                        |row| row.get(0),
                    )
                    .optional()?;
                let stored = stored_raw
                    .as_deref()
                    .map(|raw| decimal_value(raw, "Persisted virtual balance", true))
                    .transpose()?
                    .unwrap_or_else(BigDecimal::zero);
                let expected = self
                    .balances
                    .get(mint)
                    .cloned()
                    .unwrap_or_else(BigDecimal::zero);
                if stored != expected {
                    return Err(PaperSimulatorError::ConcurrentStateConflict);
                }

                let reserved = Self::persisted_reserved_balance(&transaction, mint, None)?;
                if amount < &reserved {
                    return Err(PaperSimulatorError::ConcurrentStateConflict);
                }
            }

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
                    price_age_seconds,slippage_bps,created_at,base_network_fee,priority_fee
                 ) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15,?16,?17,?18)",
                params![
                    fill.fill_id,
                    fill.order_id,
                    fill.asset_mint,
                    fill.quantity,
                    fill.reference_price,
                    fill.execution_price,
                    fill.gross_quote,
                    fill.fee,
                    fill.network_cost,
                    fill.net_quote,
                    fill.currency,
                    fill.price_source,
                    fill.price_timestamp,
                    fill.price_age_seconds as i64,
                    i64::from(fill.slippage_bps),
                    fill.created_at,
                    fill.base_network_fee,
                    fill.priority_fee
                ],
            )?;
        }
        transaction.commit()?;
        Ok(())
    }

    fn persist_new_order(
        &self,
        order: &PaperOrder,
        reservation: Option<&BalanceReservation>,
    ) -> Result<bool, PaperSimulatorError> {
        if self.db_path.is_none() {
            if let Some(reservation) = reservation {
                if reservation.amount > self.available_balance(&reservation.mint) {
                    return Ok(false);
                }
            }
            self.persist(order, None, None)?;
            return Ok(true);
        }

        let mut connection = self.connect()?;
        let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
        if let Some(reservation) = reservation {
            let stored_raw: Option<String> = transaction
                .query_row(
                    "SELECT amount FROM simulator_balances WHERE mint=?1",
                    [&reservation.mint],
                    |row| row.get(0),
                )
                .optional()?;
            let stored = stored_raw
                .as_deref()
                .map(|raw| decimal_value(raw, "Persisted virtual balance", true))
                .transpose()?
                .unwrap_or_else(BigDecimal::zero);
            let reserved = Self::persisted_reserved_balance(&transaction, &reservation.mint, None)?;
            if reservation.amount > stored - reserved {
                return Ok(false);
            }
        }

        Self::persist_order(&transaction, order)?;
        if let Some(reservation) = reservation {
            transaction.execute(
                "INSERT INTO simulator_reservations(order_id,mint,amount) VALUES(?1,?2,?3)",
                params![
                    order.order_id,
                    reservation.mint,
                    reservation.amount.normalized().to_plain_string()
                ],
            )?;
        }
        transaction.commit()?;
        Ok(true)
    }

    fn persisted_reserved_balance(
        transaction: &Transaction<'_>,
        mint: &str,
        exclude_order_id: Option<&str>,
    ) -> Result<BigDecimal, PaperSimulatorError> {
        let mut statement = transaction.prepare(
            "SELECT amount FROM simulator_reservations
             WHERE mint=?1 AND (?2 IS NULL OR order_id<>?2)",
        )?;
        let rows = statement.query_map(params![mint, exclude_order_id], |row| {
            row.get::<_, String>(0)
        })?;
        let mut total = BigDecimal::zero();
        for row in rows {
            total += decimal_value(&row?, "Persisted reservation amount", false)?;
        }
        Ok(total)
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

    pub fn balances(&self) -> &BTreeMap<String, BigDecimal> {
        &self.balances
    }

    pub fn reserved_balance(&self, mint: &str) -> BigDecimal {
        self.reservations
            .values()
            .filter(|reservation| reservation.mint == mint)
            .fold(BigDecimal::zero(), |total, reservation| {
                total + &reservation.amount
            })
    }

    pub fn available_balance(&self, mint: &str) -> BigDecimal {
        let balance = self
            .balances
            .get(mint)
            .cloned()
            .unwrap_or_else(BigDecimal::zero);
        let available = balance - self.reserved_balance(mint);
        if available < BigDecimal::zero() {
            BigDecimal::zero()
        } else {
            available
        }
    }

    fn available_balance_for_order(&self, mint: &str, order_id: &str) -> BigDecimal {
        let balance = self
            .balances
            .get(mint)
            .cloned()
            .unwrap_or_else(BigDecimal::zero);
        let reserved = self
            .reservations
            .iter()
            .filter(|(reserved_order_id, reservation)| {
                reservation.mint == mint && reserved_order_id.as_str() != order_id
            })
            .fold(BigDecimal::zero(), |total, (_, reservation)| {
                total + &reservation.amount
            });
        let available = balance - reserved;
        if available < BigDecimal::zero() {
            BigDecimal::zero()
        } else {
            available
        }
    }

    pub fn orders(&self) -> &BTreeMap<String, PaperOrder> {
        &self.orders
    }
    pub fn fills(&self) -> &[PaperFill] {
        &self.fills
    }

    pub fn create_order(
        &mut self,
        request: CreateOrderRequest<'_>,
    ) -> Result<Value, PaperSimulatorError> {
        let CreateOrderRequest {
            signal_id,
            side,
            asset_mint,
            quantity,
            quote_mint,
            created_at,
            details,
        } = request;
        if signal_id.trim().is_empty() {
            return Err(PaperSimulatorError::InvalidInput(
                "Signal ID is required for deduplication.".to_string(),
            ));
        }
        if self.processed_signals.contains(signal_id) {
            return Ok(json!({"status":"DUPLICATE","signal_id":signal_id}));
        }
        if !matches!(side, "BUY" | "SELL") {
            return Err(PaperSimulatorError::InvalidInput(
                "Side must be BUY or SELL.".to_string(),
            ));
        }
        if asset_mint.trim().is_empty() || quote_mint.trim().is_empty() || asset_mint == quote_mint
        {
            return Err(PaperSimulatorError::InvalidInput(
                "Distinct asset and quote mints are required.".to_string(),
            ));
        }
        let qty = decimal_value(quantity, "Order quantity", false)?;
        crate::pnl::parse_timestamp(created_at)?;

        // Check the persistent unique key before mutating in-memory state. SQLite's
        // UNIQUE constraint remains the final idempotency boundary across processes.
        if self.db_path.is_some() {
            let connection = self.connect()?;
            let existing: Option<String> = connection
                .query_row(
                    "SELECT order_id FROM simulated_orders WHERE signal_id=?1",
                    [signal_id],
                    |row| row.get(0),
                )
                .optional()?;
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
            for (key, value) in extra {
                order_details.insert(key, value);
            }
        }

        // The order owns exactly one reservation. Automatic BUYs reserve their
        // all-in USD budget; SELLs reserve their token quantity. Older BUY orders
        // with only target_notional_usd retain a conservative notional reservation.
        let explicit_reservation = order_details
            .get("reservation_mint")
            .and_then(Value::as_str)
            .zip(
                order_details
                    .get("reservation_amount")
                    .and_then(Value::as_str),
            );
        let reservation = if let Some((mint, raw_amount)) = explicit_reservation {
            let amount = decimal_value(raw_amount, "Order reservation", false)?;
            if mint.trim().is_empty() {
                return Err(PaperSimulatorError::InvalidInput(
                    "Reservation mint is required.".to_string(),
                ));
            }
            Some(BalanceReservation {
                mint: mint.to_string(),
                amount,
            })
        } else if side == "SELL" {
            Some(BalanceReservation {
                mint: asset_mint.to_string(),
                amount: qty.clone(),
            })
        } else {
            let raw_budget = order_details
                .get("target_budget_usd")
                .and_then(Value::as_str)
                .or_else(|| {
                    order_details
                        .get("target_notional_usd")
                        .and_then(Value::as_str)
                });
            raw_budget
                .and_then(|raw| decimal_value(raw, "Order reservation", false).ok())
                .map(|amount| BalanceReservation {
                    mint: quote_mint.to_string(),
                    amount,
                })
        };
        if let Some(reservation) = &reservation {
            order_details.insert("reservation_mint".to_string(), json!(reservation.mint));
            order_details.insert(
                "reservation_amount".to_string(),
                json!(reservation.amount.normalized().to_plain_string()),
            );
            order_details.insert("reservation_state".to_string(), json!("RESERVED"));
        }

        let mut order = PaperOrder {
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

        let reservation_created = match self.persist_new_order(&order, reservation.as_ref()) {
            Ok(created) => created,
            Err(error) => {
                if matches!(&error, PaperSimulatorError::Database(rusqlite::Error::SqliteFailure(code, _)) if code.code == rusqlite::ErrorCode::ConstraintViolation)
                {
                    return Ok(json!({"status":"DUPLICATE","signal_id":signal_id}));
                }
                return Err(error);
            }
        };
        if !reservation_created {
            if self.db_path.is_some() {
                // Another process may have reserved funds after this instance
                // loaded its snapshot. Refresh the reservation view before exposing it.
                self.load_reservations()?;
            }
            order.status = "REJECTED".to_string();
            order.reason = Some(
                "Insufficient available virtual balance; existing pending orders retain their reservations."
                    .to_string(),
            );
            set_detail(&mut order.details, "execution_state", json!("REJECTED"));
            set_detail(&mut order.details, "reservation_state", json!("REJECTED"));
            self.persist(&order, None, None)?;
        } else if let Some(reservation) = reservation {
            self.reservations
                .insert(order.order_id.clone(), reservation);
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
            return Err(PaperSimulatorError::InvalidInput(
                "A token mint or paper asset symbol (max 128 characters) is required.".to_string(),
            ));
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
            return Ok(
                json!({"mint":mint,"initial_balance":value.to_string(),"balance":value.to_string(),"simulated":true,"real_trading":false,"transaction_broadcast":false}),
            );
        }

        if self.orders.values().any(|order| order.status == "FILLED")
            || !self.reservations.is_empty()
        {
            return Err(PaperSimulatorError::InvalidInput(
                "Starting balance cannot be changed after a paper order has filled or reserved funds."
                    .to_string(),
            ));
        }

        if self.db_path.is_some() {
            let mut connection = self.connect()?;
            let transaction =
                connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
            let persisted_reserved = Self::persisted_reserved_balance(&transaction, mint, None)?;
            if value < persisted_reserved {
                return Err(PaperSimulatorError::InvalidInput(
                    "Starting balance cannot be set below funds reserved by pending orders."
                        .to_string(),
                ));
            }
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
        Ok(
            json!({"mint":mint,"initial_balance":value.to_string(),"balance":value.to_string(),"simulated":true,"real_trading":false,"transaction_broadcast":false}),
        )
    }

    pub fn set_virtual_balance(
        &mut self,
        amount: &str,
        mint: &str,
    ) -> Result<Value, PaperSimulatorError> {
        if mint.trim().is_empty() || mint.trim().len() > 128 {
            return Err(PaperSimulatorError::InvalidInput(
                "A virtual balance currency is required.".to_string(),
            ));
        }
        let value = decimal_value(amount, "Virtual balance", true)?;
        if value < self.reserved_balance(mint) {
            return Err(PaperSimulatorError::InvalidInput(
                "Virtual balance cannot be reduced below funds reserved by pending orders."
                    .to_string(),
            ));
        }
        self.persist_balance(mint, &value, true)?;
        self.balances.insert(mint.to_string(), value.clone());
        Ok(
            json!({"mint":mint,"balance":value.to_string(),"simulated":true,"real_trading":false,"transaction_broadcast":false}),
        )
    }

    pub fn add_virtual_balance(
        &mut self,
        mint: &str,
        amount: &str,
    ) -> Result<Value, PaperSimulatorError> {
        if mint.trim().is_empty() || mint.trim().len() > 128 {
            return Err(PaperSimulatorError::InvalidInput(
                "A token mint or paper asset symbol (max 128 characters) is required.".to_string(),
            ));
        }
        let value = decimal_value(amount, "Virtual funding amount", false)?;
        let updated = self
            .balances
            .get(mint)
            .cloned()
            .unwrap_or_else(BigDecimal::zero)
            + &value;
        self.persist_balance(mint, &updated, false)?;
        self.balances.insert(mint.to_string(), updated.clone());
        Ok(
            json!({"mint":mint,"added":value.to_string(),"balance":updated.to_string(),"simulated":true,"real_trading":false,"transaction_broadcast":false}),
        )
    }

    fn persist_balance(
        &self,
        mint: &str,
        value: &BigDecimal,
        set_initial_if_missing: bool,
    ) -> Result<(), PaperSimulatorError> {
        if self.db_path.is_none() {
            return Ok(());
        }
        let mut connection = self.connect()?;
        let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let persisted_reserved = Self::persisted_reserved_balance(&transaction, mint, None)?;
        if value < &persisted_reserved {
            return Err(PaperSimulatorError::InvalidInput(
                "Virtual balance cannot be reduced below funds reserved by pending orders."
                    .to_string(),
            ));
        }
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
        self.cancel_order_with_reason(order_id, "Cancelled before simulated execution.")
    }

    pub fn cancel_order_with_reason(
        &mut self,
        order_id: &str,
        reason: &str,
    ) -> Result<Value, PaperSimulatorError> {
        let safe_reason = reason.trim();
        if safe_reason.is_empty() || safe_reason.len() > 300 {
            return Err(PaperSimulatorError::InvalidInput(
                "Cancellation reason must contain 1 to 300 characters.".to_string(),
            ));
        }
        let mut order = self
            .orders
            .get(order_id)
            .cloned()
            .ok_or(PaperSimulatorError::OrderNotFound)?;
        if order.status != "CREATED" {
            return Ok(
                json!({"status":"REJECTED","reason":"Only unexecuted orders can be cancelled."}),
            );
        }
        order.status = "CANCELLED".to_string();
        order.reason = Some(safe_reason.to_string());
        if let Some(details) = order.details.as_object_mut() {
            details.insert(
                "execution_state".to_string(),
                Value::String("CANCELLED".to_string()),
            );
            if self.reservations.contains_key(order_id) {
                details.insert("reservation_state".to_string(), json!("RELEASED"));
            }
            details.insert(
                "paper_copy_skip_reason".to_string(),
                Value::String(safe_reason.to_string()),
            );
        }
        self.persist_releasing_reservation(&order, None, None)?;
        self.reservations.remove(order_id);
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
        if let Some(reservation) = self.reservations.get(order_id) {
            if reservation.mint == order.asset_mint && reservation.amount != parsed {
                return Err(PaperSimulatorError::InvalidInput(
                    "A reserved SELL quantity cannot be changed; cancel and recreate the order."
                        .to_string(),
                ));
            }
        }
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
        let mut order = self
            .orders
            .get(order_id)
            .cloned()
            .ok_or(PaperSimulatorError::OrderNotFound)?;
        if order.status != "CREATED" {
            return Ok(json!({"status":"REJECTED","reason":"Order is not awaiting execution."}));
        }
        if !rpc_ok {
            return self.wait_for_market_data(
                &mut order,
                "RPC/provider data unavailable; retrying without a fill.",
            );
        }

        let Some(price) = market_price else {
            return self.wait_for_market_data(&mut order, "Market price unavailable; retrying.");
        };
        if price.validate().is_err() {
            return self.reject_order(&mut order, "Market price is invalid.");
        }
        let price_is_fresh = match price.is_fresh(policy.max_price_age_seconds, Some(now)) {
            Ok(is_fresh) => is_fresh,
            Err(_) => return self.reject_order(&mut order, "Market price timestamp is invalid."),
        };
        if price.mint != order.asset_mint || !price_is_fresh {
            return self.wait_for_market_data(
                &mut order,
                "Market price is stale or for a different token; retrying without a fill.",
            );
        }

        if let Some(liquidity) = &price.liquidity_usd {
            let liquidity_age = price.liquidity_age_seconds(Some(now))?.unwrap_or(u64::MAX);
            let minimum = decimal_value(&policy.min_liquidity_usd, "Minimum USD liquidity", true)?;
            if liquidity_age > policy.max_liquidity_age_seconds || liquidity < &minimum {
                return self.wait_for_market_data(&mut order, "Liquidity is stale or below the configured USD minimum; retrying without a fill.");
            }
        } else if policy.require_liquidity {
            return self.wait_for_market_data(
                &mut order,
                "Liquidity data unavailable; retrying without a fill.",
            );
        }

        if policy.slippage_bps > 10_000 || policy.fee_bps > 10_000 {
            return self.reject_order(
                &mut order,
                "Slippage and trading-fee basis points must be between 0 and 10000.",
            );
        }
        let minimum_order_value_raw = match order.details.get("minimum_order_value_usd").cloned() {
            None | Some(Value::Null) => policy.min_order_value_usd.clone(),
            Some(Value::String(value)) => value,
            Some(_) => {
                return self.reject_order(&mut order, "Minimum order value is invalid.");
            }
        };
        let minimum_order_value =
            match decimal_value(&minimum_order_value_raw, "Minimum order value USD", false) {
                Ok(value) => value,
                Err(_) => return self.reject_order(&mut order, "Minimum order value is invalid."),
            };
        let minimum_order_quantity_raw = match order.details.get("minimum_order_quantity").cloned()
        {
            None | Some(Value::Null) => policy.min_order_quantity.clone(),
            Some(Value::String(value)) => value,
            Some(_) => {
                return self.reject_order(&mut order, "Minimum order quantity is invalid.");
            }
        };
        let minimum_order_quantity =
            match decimal_value(&minimum_order_quantity_raw, "Minimum order quantity", false) {
                Ok(value) => value,
                Err(_) => {
                    return self.reject_order(&mut order, "Minimum order quantity is invalid.")
                }
            };
        let quantity_step_raw = match order.details.get("asset_quantity_step").cloned() {
            None | Some(Value::Null) => policy.min_order_quantity.clone(),
            Some(Value::String(value)) => value,
            Some(_) => {
                return self.reject_order(&mut order, "Token quantity step is invalid.");
            }
        };
        let quantity_step = match decimal_value(&quantity_step_raw, "Token quantity step", false) {
            Ok(value) => value,
            Err(_) => return self.reject_order(&mut order, "Token quantity step is invalid."),
        };
        let reference_price = price.price.clone();
        let slip_factor = BigDecimal::from(10_000_i64 + i64::from(policy.slippage_bps))
            / BigDecimal::from(10_000_i64);
        let execution_price = if order.side == "BUY" {
            &reference_price * slip_factor
        } else {
            &reference_price
                * (BigDecimal::from(10_000_i64 - i64::from(policy.slippage_bps))
                    / BigDecimal::from(10_000_i64))
        };
        let fee_rate = BigDecimal::from(i64::from(policy.fee_bps)) / BigDecimal::from(10_000_i64);
        let fee_multiplier = BigDecimal::from(1) + &fee_rate;
        let base_network_fee =
            match decimal_value(&policy.network_cost, "Base network fee USD", true) {
                Ok(value) => value,
                Err(_) => {
                    return self.reject_order(&mut order, "Configured base network fee is invalid.")
                }
            };
        let priority_fee = match decimal_value(&policy.priority_fee_usd, "Priority fee USD", true) {
            Ok(value) => value,
            Err(_) => return self.reject_order(&mut order, "Configured priority fee is invalid."),
        };
        let network = &base_network_fee + &priority_fee;
        if network > 1_000 {
            return self.reject_order(
                &mut order,
                "Combined simulated network and priority fees exceed the $1,000 per-fill safety limit.",
            );
        }

        let target_budget = match order.details.get("target_budget_usd").cloned() {
            None | Some(Value::Null) => None,
            Some(Value::String(value)) => {
                match decimal_value(&value, "Target all-in USD BUY budget", false) {
                    Ok(value) => Some(value),
                    Err(_) => {
                        return self
                            .reject_order(&mut order, "Target all-in USD BUY budget is invalid.");
                    }
                }
            }
            Some(_) => {
                return self.reject_order(&mut order, "Target all-in USD BUY budget is invalid.");
            }
        };
        let mut quantity = match decimal_value(&order.quantity, "Order quantity", false) {
            Ok(value) => value,
            Err(_) => return self.reject_order(&mut order, "Order quantity is invalid."),
        };
        if let Some(budget) = target_budget {
            if order.side != "BUY" {
                return self.reject_order(
                    &mut order,
                    "An all-in USD BUY budget can only be used by a BUY order.",
                );
            }
            let quote_balance = self.available_balance_for_order(&order.quote_mint, order_id);
            if budget > quote_balance {
                return self.reject_order(
                    &mut order,
                    "Insufficient simulated quote balance for the configured BUY budget.",
                );
            }
            if budget <= network {
                return self.reject_order(
                    &mut order,
                    "The configured BUY budget does not cover simulated network cost.",
                );
            }
            let all_in_cost_per_token = &execution_price * &fee_multiplier;
            if all_in_cost_per_token <= BigDecimal::zero() {
                return self.reject_order(
                    &mut order,
                    "The simulated BUY execution price and fees are invalid.",
                );
            }
            quantity = (&budget - &network) / all_in_cost_per_token;
            if quantity <= BigDecimal::zero() {
                return self.reject_order(
                    &mut order,
                    "The configured BUY budget produces no positive token quantity.",
                );
            }
        } else if order
            .details
            .get("target_notional_usd")
            .is_some_and(|value| !value.is_null())
        {
            // Backward compatibility for pending orders persisted before the
            // all-in budget contract was introduced.
            let Some(target) = order
                .details
                .get("target_notional_usd")
                .and_then(Value::as_str)
            else {
                return self.reject_order(&mut order, "Target USD paper order size is invalid.");
            };
            let target_value = match decimal_value(target, "Target USD paper order size", false) {
                Ok(value) => value,
                Err(_) => {
                    return self.reject_order(&mut order, "Target USD paper order size is invalid.")
                }
            };
            quantity = target_value / &price.price;
        }

        // Token balances are integer atomic units. Round down to a whole
        // quantity step so paper fills never invent fractional base units or
        // exceed an all-in BUY budget because of decimal division precision.
        let whole_steps = (&quantity / &quantity_step).with_scale_round(0, RoundingMode::Down);
        quantity = whole_steps * &quantity_step;
        order.quantity = quantity.normalized().to_plain_string();

        if quantity < minimum_order_quantity {
            return self.reject_order(
                &mut order,
                "Order quantity is below the configured minimum token quantity.",
            );
        }

        let gross = &quantity * &execution_price;
        if gross < minimum_order_value {
            return self.reject_order(
                &mut order,
                "Order value is below the configured minimum USD order value.",
            );
        }
        let fee = &gross * &fee_rate;
        let total_cost_or_proceeds = if order.side == "BUY" {
            &gross + &fee + &network
        } else {
            &gross - &fee - &network
        };
        if total_cost_or_proceeds < BigDecimal::zero() {
            return self.reject_order(&mut order, "Costs exceed simulated sale proceeds.");
        }

        let mut balances = self.balances.clone();
        let asset_balance = balances
            .get(&order.asset_mint)
            .cloned()
            .unwrap_or_else(BigDecimal::zero);
        let quote_balance = balances
            .get(&order.quote_mint)
            .cloned()
            .unwrap_or_else(BigDecimal::zero);
        if order.side == "BUY" {
            let spendable_quote = self.available_balance_for_order(&order.quote_mint, order_id);
            if spendable_quote < total_cost_or_proceeds {
                return self.reject_order(&mut order, "Insufficient simulated quote balance.");
            }
            balances.insert(
                order.quote_mint.clone(),
                quote_balance - &total_cost_or_proceeds,
            );
            balances.insert(order.asset_mint.clone(), asset_balance + &quantity);
        } else {
            let spendable_asset = self.available_balance_for_order(&order.asset_mint, order_id);
            if spendable_asset < quantity {
                return self.reject_order(
                    &mut order,
                    "Insufficient simulated asset balance after pending-order reservations.",
                );
            }
            balances.insert(order.asset_mint.clone(), asset_balance - &quantity);
            balances.insert(
                order.quote_mint.clone(),
                quote_balance + &total_cost_or_proceeds,
            );
        }

        let fill = PaperFill {
            fill_id: Uuid::new_v4().to_string(),
            order_id: order.order_id.clone(),
            side: order.side.clone(),
            asset_mint: order.asset_mint.clone(),
            quantity: quantity.normalized().to_plain_string(),
            quote_mint: order.quote_mint.clone(),
            reference_price: reference_price.to_string(),
            execution_price: execution_price.to_string(),
            gross_quote: gross.to_string(),
            fee: fee.to_string(),
            base_network_fee: base_network_fee.normalized().to_plain_string(),
            priority_fee: priority_fee.normalized().to_plain_string(),
            network_cost: network.normalized().to_plain_string(),
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
        if order
            .details
            .get("target_budget_usd")
            .is_some_and(|value| !value.is_null())
        {
            set_detail(
                &mut order.details,
                "buy_budget_used_usd",
                json!(total_cost_or_proceeds.normalized().to_plain_string()),
            );
        }
        set_detail(
            &mut order.details,
            "minimum_order_value_usd",
            json!(minimum_order_value.normalized().to_plain_string()),
        );
        set_detail(
            &mut order.details,
            "minimum_order_quantity",
            json!(minimum_order_quantity.normalized().to_plain_string()),
        );
        set_detail(
            &mut order.details,
            "asset_quantity_step",
            json!(quantity_step.normalized().to_plain_string()),
        );
        set_detail(
            &mut order.details,
            "order_value_usd",
            json!(gross.normalized().to_plain_string()),
        );
        set_detail(&mut order.details, "execution_state", json!("FILLED"));
        set_detail(
            &mut order.details,
            "reference_price",
            json!(reference_price.to_string()),
        );
        set_detail(
            &mut order.details,
            "execution_price",
            json!(execution_price.to_string()),
        );
        set_detail(&mut order.details, "fee", json!(fee.to_string()));
        set_detail(&mut order.details, "trading_fee_bps", json!(policy.fee_bps));
        set_detail(
            &mut order.details,
            "base_network_fee_usd",
            json!(base_network_fee.normalized().to_plain_string()),
        );
        set_detail(
            &mut order.details,
            "priority_fee_usd",
            json!(priority_fee.normalized().to_plain_string()),
        );
        set_detail(
            &mut order.details,
            "network_cost",
            json!(network.normalized().to_plain_string()),
        );
        set_detail(&mut order.details, "fill_id", json!(fill.fill_id));
        if self.reservations.contains_key(order_id) {
            set_detail(&mut order.details, "reservation_state", json!("CONSUMED"));
        }

        // This single SQLite transaction commits the order, balance changes,
        // fill, and reservation release together.
        self.persist_releasing_reservation(&order, Some(&balances), Some(&fill))?;
        self.reservations.remove(order_id);
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

    fn wait_for_market_data(
        &mut self,
        order: &mut PaperOrder,
        reason: &str,
    ) -> Result<Value, PaperSimulatorError> {
        order.status = "CREATED".to_string();
        order.reason = Some(reason.to_string());
        set_detail(
            &mut order.details,
            "execution_state",
            json!("WAITING_FOR_MARKET_DATA"),
        );
        self.persist(order, None, None)?;
        self.orders.insert(order.order_id.clone(), order.clone());
        Ok(json!({
            "status":"WAITING_FOR_MARKET_DATA","reason":reason,
            "simulated":true,"real_trading":false,"transaction_broadcast":false
        }))
    }

    fn reject_order(
        &mut self,
        order: &mut PaperOrder,
        reason: &str,
    ) -> Result<Value, PaperSimulatorError> {
        order.status = "REJECTED".to_string();
        order.reason = Some(reason.to_string());
        set_detail(&mut order.details, "execution_state", json!("REJECTED"));
        if self.reservations.contains_key(&order.order_id) {
            set_detail(&mut order.details, "reservation_state", json!("RELEASED"));
        }
        self.persist_releasing_reservation(order, None, None)?;
        self.reservations.remove(&order.order_id);
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
                        &fill.asset_mint,
                        &fill.quantity,
                        &fill.execution_price,
                        &fill.fee,
                        false,
                        &fill.network_cost,
                        Some(&fill.created_at),
                    )?;
                }
                "SELL" => {
                    let tracked = decimal_value(
                        &ledger.snapshot(&fill.asset_mint).quantity,
                        "Tracked quantity",
                        true,
                    )?;
                    let sold = decimal_value(&fill.quantity, "Quantity", false)?;
                    if sold > tracked {
                        let missing = &sold - &tracked;
                        ledger.deposit(
                            &fill.asset_mint,
                            &missing.to_string(),
                            None,
                            Some(&fill.created_at),
                        )?;
                    }
                    let event = ledger.sell(
                        &fill.asset_mint,
                        &fill.quantity,
                        &fill.execution_price,
                        &fill.fee,
                        false,
                        &fill.network_cost,
                        Some(&fill.created_at),
                    )?;
                    let entry = realized
                        .entry(fill.asset_mint.clone())
                        .or_insert((BigDecimal::zero(), true));
                    match event.realized_pnl {
                        Some(value) => {
                            entry.0 += BigDecimal::from_str(&value).map_err(|_| {
                                PaperSimulatorError::InvalidInput("Invalid P&L result.".to_string())
                            })?
                        }
                        None => entry.1 = false,
                    }
                }
                _ => {
                    return Err(PaperSimulatorError::InvalidInput(
                        "Persisted fill has an invalid side.".to_string(),
                    ))
                }
            }
        }
        let realized_by_mint = realized
            .into_iter()
            .map(|(mint, (value, complete))| {
                (
                    mint,
                    json!({
                        "pnl": if complete { Some(value.to_string()) } else { None },
                        "status": if complete { "calculated" } else { "unknown_cost_basis" },
                        "currency":"USD"
                    }),
                )
            })
            .collect();

        Ok(PaperPnlSummary {
            cost_basis_method: "average_cost".to_string(),
            currency: "USD".to_string(),
            realized_by_mint,
            unrealized_status: "unavailable_no_verified_fresh_market_price".to_string(),
            unrealized_note: "No verified market-price provider is connected to the simulator."
                .to_string(),
        })
    }

    pub fn report(&self) -> Result<Value, PaperSimulatorError> {
        let balances = self
            .balances
            .iter()
            .map(|(mint, amount)| (mint.clone(), json!(amount.to_string())))
            .collect::<Map<_, _>>();
        let mut reserved_balances = Map::new();
        let mut available_balances = Map::new();
        for mint in self.balances.keys() {
            reserved_balances.insert(mint.clone(), json!(self.reserved_balance(mint).to_string()));
            available_balances.insert(
                mint.clone(),
                json!(self.available_balance(mint).to_string()),
            );
        }
        Ok(json!({
            "mode":"PAPER_SIMULATION",
            "simulated":true,
            "real_wallet_balances_included":false,
            "real_trading":false,
            "transaction_broadcast":false,
            "balances":balances,
            "reserved_balances":reserved_balances,
            "available_balances":available_balances,
            "pending_reservation_count":self.reservations.len(),
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

    fn now() -> &'static str {
        "2026-11-09T12:00:00Z"
    }

    fn virtual_cash() -> BTreeMap<String, String> {
        BTreeMap::from([("USD".to_string(), "100".to_string())])
    }

    fn price(mint: &str) -> MarketPrice {
        MarketPrice::new(
            mint,
            "10",
            "USD",
            "test-feed",
            now(),
            Some("50000"),
            Some("test-liquidity".to_string()),
            Some(now().to_string()),
        )
        .unwrap()
    }

    fn buy_order(sim: &mut PaperSimulator, signal: &str) -> String {
        sim.create_order(CreateOrderRequest {
            signal_id: signal,
            side: "BUY",
            asset_mint: "TOKEN",
            quantity: "1",
            quote_mint: "USD",
            created_at: now(),
            details: Some(json!({
                "target_notional_usd":"20","origin":"AUTOMATIC_WALLET_COPY"
            })),
        })
        .unwrap()["order_id"]
            .as_str()
            .unwrap()
            .to_string()
    }

    fn budget_buy_order(sim: &mut PaperSimulator, signal: &str, budget: &str) -> String {
        sim.create_order(CreateOrderRequest {
            signal_id: signal,
            side: "BUY",
            asset_mint: "TOKEN",
            quantity: "1",
            quote_mint: "USD",
            created_at: now(),
            details: Some(json!({
                "target_budget_usd":budget,
                "target_budget_semantics":"ALL_IN_USD_BUDGET_INCLUDING_SLIPPAGE_FEES_AND_NETWORK_COST",
                "origin":"AUTOMATIC_WALLET_COPY"
            })),
        })
        .unwrap()["order_id"]
            .as_str()
            .unwrap()
            .to_string()
    }

    #[test]
    fn configurable_trading_network_and_priority_fees_are_accounted_and_persisted() {
        let directory = tempdir().unwrap();
        let path = directory.path().join("configured-fees.db");
        let order_id;
        {
            let mut sim = PaperSimulator::open(&path, &virtual_cash()).unwrap();
            order_id = buy_order(&mut sim, "configured-fee-order");
            let policy = ExecutionPolicy {
                fee_bps: 50,
                network_cost: "0.25".to_string(),
                priority_fee_usd: "0.75".to_string(),
                ..ExecutionPolicy::default()
            };

            let result = sim
                .execute_order(&order_id, Some(&price("TOKEN")), now(), &policy, true)
                .unwrap();
            assert_eq!(result["status"], "FILLED");

            let fill = &sim.fills()[0];
            assert_eq!(
                BigDecimal::from_str(&fill.gross_quote).unwrap(),
                BigDecimal::from_str("20.1").unwrap()
            );
            assert_eq!(
                BigDecimal::from_str(&fill.fee).unwrap(),
                BigDecimal::from_str("0.1005").unwrap()
            );
            assert_eq!(
                BigDecimal::from_str(&fill.base_network_fee).unwrap(),
                BigDecimal::from_str("0.25").unwrap()
            );
            assert_eq!(
                BigDecimal::from_str(&fill.priority_fee).unwrap(),
                BigDecimal::from_str("0.75").unwrap()
            );
            assert_eq!(
                BigDecimal::from_str(&fill.network_cost).unwrap(),
                BigDecimal::from(1)
            );
            assert_eq!(
                BigDecimal::from_str(&fill.net_quote).unwrap(),
                BigDecimal::from_str("21.2005").unwrap()
            );
            assert_eq!(sim.orders()[&order_id].details["trading_fee_bps"], 50);
            assert_eq!(
                sim.orders()[&order_id].details["base_network_fee_usd"],
                "0.25"
            );
            assert_eq!(sim.orders()[&order_id].details["priority_fee_usd"], "0.75");
        }

        let recovered = PaperSimulator::open(&path, &BTreeMap::new()).unwrap();
        let fill = &recovered.fills()[0];
        assert_eq!(fill.order_id, order_id);
        assert_eq!(fill.base_network_fee, "0.25");
        assert_eq!(fill.priority_fee, "0.75");
        assert_eq!(fill.network_cost, "1");
        assert_eq!(recovered.orders()[&order_id].status, "FILLED");
    }

    #[test]
    fn invalid_priority_fee_rejects_order_and_releases_its_reservation() {
        let mut sim = PaperSimulator::in_memory(&virtual_cash()).unwrap();
        let id = budget_buy_order(&mut sim, "invalid-priority-fee", "20");
        assert_eq!(sim.reserved_balance("USD"), BigDecimal::from(20));

        let policy = ExecutionPolicy {
            priority_fee_usd: "-0.01".to_string(),
            ..ExecutionPolicy::default()
        };
        let result = sim
            .execute_order(&id, Some(&price("TOKEN")), now(), &policy, true)
            .unwrap();

        assert_eq!(result["status"], "REJECTED");
        assert!(result["reason"].as_str().unwrap().contains("priority fee"));
        assert_eq!(sim.reserved_balance("USD"), BigDecimal::zero());
        assert_eq!(sim.available_balance("USD"), BigDecimal::from(100));
        assert!(sim.fills().is_empty());
    }

    #[test]
    fn existing_simulated_fills_schema_is_migrated_with_fee_breakdown_columns() {
        let directory = tempdir().unwrap();
        let path = directory.path().join("legacy-fills.db");
        {
            let connection = Connection::open(&path).unwrap();
            connection
                .execute_batch(
                    "CREATE TABLE simulated_fills(
                        fill_id TEXT PRIMARY KEY,
                        order_id TEXT NOT NULL UNIQUE,
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
                    );",
                )
                .unwrap();
        }

        let sim = PaperSimulator::open(&path, &virtual_cash()).unwrap();
        assert!(sim.fills().is_empty());

        let connection = Connection::open(&path).unwrap();
        let base_fee_exists: bool = connection
            .query_row(
                "SELECT EXISTS(
                    SELECT 1 FROM pragma_table_info('simulated_fills')
                    WHERE name='base_network_fee'
                )",
                [],
                |row| row.get(0),
            )
            .unwrap();
        let priority_fee_exists: bool = connection
            .query_row(
                "SELECT EXISTS(
                    SELECT 1 FROM pragma_table_info('simulated_fills')
                    WHERE name='priority_fee'
                )",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert!(base_fee_exists);
        assert!(priority_fee_exists);
    }

    #[test]
    fn fills_a_paper_buy_with_decimal_accounting() {
        let mut sim = PaperSimulator::in_memory(&virtual_cash()).unwrap();
        let id = buy_order(&mut sim, "signal-1");
        let result = sim
            .execute_order(
                &id,
                Some(&price("TOKEN")),
                now(),
                &ExecutionPolicy::default(),
                true,
            )
            .unwrap();
        assert_eq!(result["status"], "FILLED");
        assert_eq!(
            sim.balances()["USD"],
            BigDecimal::from_str("79.8799").unwrap()
        );
        assert_eq!(sim.balances()["TOKEN"], BigDecimal::from(2));
        assert_eq!(sim.fills().len(), 1);
        assert_eq!(result["transaction_broadcast"], false);
    }

    #[test]
    fn all_in_buy_budget_covers_slippage_fees_and_network_cost_without_overspend() {
        let mut balances = virtual_cash();
        balances.insert("USD".to_string(), "20".to_string());
        let mut sim = PaperSimulator::in_memory(&balances).unwrap();
        let id = budget_buy_order(&mut sim, "all-in-budget", "20");
        let result = sim
            .execute_order(
                &id,
                Some(&price("TOKEN")),
                now(),
                &ExecutionPolicy::default(),
                true,
            )
            .unwrap();
        assert_eq!(result["status"], "FILLED");
        let fill = &sim.fills()[0];
        let spent = BigDecimal::from_str(&fill.gross_quote).unwrap()
            + BigDecimal::from_str(&fill.fee).unwrap()
            + BigDecimal::from_str(&fill.network_cost).unwrap();
        let budget = BigDecimal::from(20);
        let unspent = &budget - &spent;
        assert!(unspent >= BigDecimal::zero());
        assert!(unspent < BigDecimal::from_str("0.00000000000000002").unwrap());
        assert_eq!(sim.balances()["USD"], unspent);
        assert!(BigDecimal::from_str(&fill.quantity).unwrap() < 2);
        assert_eq!(
            sim.orders()[&id].details["asset_quantity_step"],
            "0.000000000000000001"
        );
        assert_eq!(
            BigDecimal::from_str(
                sim.orders()[&id].details["buy_budget_used_usd"]
                    .as_str()
                    .unwrap()
            )
            .unwrap(),
            spent
        );
    }

    #[test]
    fn all_in_buy_budget_rejects_invalid_or_unaffordable_budget_without_fill() {
        let mut sim = PaperSimulator::in_memory(&virtual_cash()).unwrap();
        let id = budget_buy_order(&mut sim, "oversized-budget", "101");
        let result = sim
            .execute_order(
                &id,
                Some(&price("TOKEN")),
                now(),
                &ExecutionPolicy::default(),
                true,
            )
            .unwrap();
        assert_eq!(result["status"], "REJECTED");
        assert!(sim.fills().is_empty());
        assert_eq!(sim.balances()["USD"], BigDecimal::from(100));

        let invalid = sim
            .create_order(CreateOrderRequest {
                signal_id: "invalid-budget",
                side: "BUY",
                asset_mint: "TOKEN",
                quantity: "1",
                quote_mint: "USD",
                created_at: now(),
                details: Some(json!({"target_budget_usd":true})),
            })
            .unwrap();
        let invalid_id = invalid["order_id"].as_str().unwrap();
        let rejected = sim
            .execute_order(
                invalid_id,
                Some(&price("TOKEN")),
                now(),
                &ExecutionPolicy::default(),
                true,
            )
            .unwrap();
        assert_eq!(rejected["status"], "REJECTED");
        assert!(sim.fills().is_empty());
    }

    #[test]
    fn all_in_buy_quantity_is_rounded_down_to_token_atomic_units() {
        let mut balances = virtual_cash();
        balances.insert("USD".to_string(), "20".to_string());
        let mut sim = PaperSimulator::in_memory(&balances).unwrap();
        let order = sim
            .create_order(CreateOrderRequest {
                signal_id: "atomic-unit-rounding",
                side: "BUY",
                asset_mint: "TOKEN",
                quantity: "1",
                quote_mint: "USD",
                created_at: now(),
                details: Some(json!({
                    "target_budget_usd":"20",
                    "asset_quantity_step":"0.1",
                    "minimum_order_quantity":"0.1"
                })),
            })
            .unwrap();
        let id = order["order_id"].as_str().unwrap();
        let result = sim
            .execute_order(
                id,
                Some(&price("TOKEN")),
                now(),
                &ExecutionPolicy::default(),
                true,
            )
            .unwrap();

        assert_eq!(result["status"], "FILLED");
        assert_eq!(sim.fills()[0].quantity, "1.9");
        let spent = BigDecimal::from_str(&sim.fills()[0].gross_quote).unwrap()
            + BigDecimal::from_str(&sim.fills()[0].fee).unwrap()
            + BigDecimal::from_str(&sim.fills()[0].network_cost).unwrap();
        assert!(spent <= 20);
        assert_eq!(sim.balances()["USD"], BigDecimal::from(20) - spent);
    }

    #[test]
    fn orders_below_minimum_usd_value_are_rejected_without_balance_changes() {
        let mut sim = PaperSimulator::in_memory(&virtual_cash()).unwrap();
        let order = sim
            .create_order(CreateOrderRequest {
                signal_id: "below-minimum-usd",
                side: "BUY",
                asset_mint: "TOKEN",
                quantity: "0.05",
                quote_mint: "USD",
                created_at: now(),
                details: None,
            })
            .unwrap();
        let id = order["order_id"].as_str().unwrap();
        let result = sim
            .execute_order(
                id,
                Some(&price("TOKEN")),
                now(),
                &ExecutionPolicy::default(),
                true,
            )
            .unwrap();

        assert_eq!(result["status"], "REJECTED");
        assert!(result["reason"].as_str().unwrap().contains("minimum USD"));
        assert_eq!(sim.orders()[id].status, "REJECTED");
        assert_eq!(sim.balances()["USD"], BigDecimal::from(100));
        assert!(sim.fills().is_empty());
    }

    #[test]
    fn orders_below_minimum_token_quantity_are_rejected_before_fill() {
        let mut sim = PaperSimulator::in_memory(&virtual_cash()).unwrap();
        let order = sim
            .create_order(CreateOrderRequest {
                signal_id: "below-minimum-quantity",
                side: "BUY",
                asset_mint: "TOKEN",
                quantity: "1",
                quote_mint: "USD",
                created_at: now(),
                details: Some(json!({
                    "target_budget_usd":"2",
                    "minimum_order_quantity":"0.2"
                })),
            })
            .unwrap();
        let id = order["order_id"].as_str().unwrap();
        let result = sim
            .execute_order(
                id,
                Some(&price("TOKEN")),
                now(),
                &ExecutionPolicy::default(),
                true,
            )
            .unwrap();

        assert_eq!(result["status"], "REJECTED");
        assert!(result["reason"]
            .as_str()
            .unwrap()
            .contains("minimum token quantity"));
        assert_eq!(sim.orders()[id].status, "REJECTED");
        assert_eq!(sim.balances()["USD"], BigDecimal::from(100));
        assert!(sim.fills().is_empty());
    }

    #[test]
    fn malformed_order_budget_and_minimum_constraints_are_persistently_rejected() {
        let mut sim = PaperSimulator::in_memory(&virtual_cash()).unwrap();
        for (signal, details) in [
            (
                "malformed-budget",
                json!({"target_budget_usd":"not-a-number"}),
            ),
            (
                "malformed-minimum-value",
                json!({"minimum_order_value_usd":true}),
            ),
            (
                "malformed-minimum-quantity",
                json!({"minimum_order_quantity":"-0.1"}),
            ),
        ] {
            let order = sim
                .create_order(CreateOrderRequest {
                    signal_id: signal,
                    side: "BUY",
                    asset_mint: "TOKEN",
                    quantity: "1",
                    quote_mint: "USD",
                    created_at: now(),
                    details: Some(details),
                })
                .unwrap();
            let id = order["order_id"].as_str().unwrap();
            let result = sim
                .execute_order(
                    id,
                    Some(&price("TOKEN")),
                    now(),
                    &ExecutionPolicy::default(),
                    true,
                )
                .unwrap();
            assert_eq!(result["status"], "REJECTED", "{signal}");
            assert_eq!(sim.orders()[id].status, "REJECTED", "{signal}");
        }
        assert_eq!(sim.balances()["USD"], BigDecimal::from(100));
        assert!(sim.fills().is_empty());
    }

    #[test]
    fn invalid_execution_policy_minimum_is_rejected_without_fill() {
        let mut sim = PaperSimulator::in_memory(&virtual_cash()).unwrap();
        let id = buy_order(&mut sim, "invalid-minimum-policy");
        let policy = ExecutionPolicy {
            min_order_value_usd: "NaN".to_string(),
            ..ExecutionPolicy::default()
        };
        let result = sim
            .execute_order(&id, Some(&price("TOKEN")), now(), &policy, true)
            .unwrap();

        assert_eq!(result["status"], "REJECTED");
        assert!(sim.fills().is_empty());
        assert_eq!(sim.balances()["USD"], BigDecimal::from(100));
    }

    #[test]
    fn duplicate_signal_cannot_create_a_second_order() {
        let mut sim = PaperSimulator::in_memory(&virtual_cash()).unwrap();
        let first = buy_order(&mut sim, "same-signal");
        let second = sim
            .create_order(CreateOrderRequest {
                signal_id: "same-signal",
                side: "BUY",
                asset_mint: "TOKEN",
                quantity: "1",
                quote_mint: "USD",
                created_at: now(),
                details: None,
            })
            .unwrap();
        assert_eq!(second["status"], "DUPLICATE");
        assert_eq!(sim.orders().len(), 1);
        assert!(!first.is_empty());
    }

    #[test]
    fn stale_price_waits_without_fabricating_a_fill() {
        let mut sim = PaperSimulator::in_memory(&virtual_cash()).unwrap();
        let id = buy_order(&mut sim, "stale-price");
        let stale = MarketPrice::new(
            "TOKEN",
            "10",
            "USD",
            "test-feed",
            "2026-11-09T11:58:00Z",
            Some("50000"),
            Some("test-liquidity".to_string()),
            Some(now().to_string()),
        )
        .unwrap();
        let outcome = sim
            .execute_order(&id, Some(&stale), now(), &ExecutionPolicy::default(), true)
            .unwrap();
        assert_eq!(outcome["status"], "WAITING_FOR_MARKET_DATA");
        assert!(sim.fills().is_empty());
    }

    #[test]
    fn sell_cannot_exceed_virtual_token_balance() {
        let mut balances = virtual_cash();
        balances.insert("TOKEN".to_string(), "1".to_string());
        let mut sim = PaperSimulator::in_memory(&balances).unwrap();
        let order = sim
            .create_order(CreateOrderRequest {
                signal_id: "sell-too-much",
                side: "SELL",
                asset_mint: "TOKEN",
                quantity: "2",
                quote_mint: "USD",
                created_at: now(),
                details: None,
            })
            .unwrap();
        let id = order["order_id"].as_str().unwrap();
        let result = sim
            .execute_order(
                id,
                Some(&price("TOKEN")),
                now(),
                &ExecutionPolicy::default(),
                true,
            )
            .unwrap();
        assert_eq!(result["status"], "REJECTED");
        assert_eq!(sim.balances()["TOKEN"].to_string(), "1");
        assert!(sim.fills().is_empty());
    }

    #[test]
    fn pending_buy_reservations_prevent_double_spending_available_usd() {
        let mut sim = PaperSimulator::in_memory(&virtual_cash()).unwrap();
        let first = budget_buy_order(&mut sim, "reserve-buy-60", "60");

        assert_eq!(sim.balances()["USD"], BigDecimal::from(100));
        assert_eq!(sim.reserved_balance("USD"), BigDecimal::from(60));
        assert_eq!(sim.available_balance("USD"), BigDecimal::from(40));
        assert_eq!(sim.orders()[&first].status, "CREATED");

        let second = budget_buy_order(&mut sim, "reserve-buy-50", "50");
        assert_eq!(sim.orders()[&second].status, "REJECTED");
        assert!(sim.orders()[&second]
            .reason
            .as_deref()
            .unwrap()
            .contains("Insufficient available virtual balance"));
        assert_eq!(sim.reserved_balance("USD"), BigDecimal::from(60));
        assert_eq!(sim.available_balance("USD"), BigDecimal::from(40));
        assert_eq!(sim.balances()["USD"], BigDecimal::from(100));

        let final_order = budget_buy_order(&mut sim, "reserve-buy-40", "40");
        assert_eq!(sim.orders()[&final_order].status, "CREATED");
        assert_eq!(sim.reserved_balance("USD"), BigDecimal::from(100));
        assert_eq!(sim.available_balance("USD"), BigDecimal::zero());
    }

    #[test]
    fn pending_sell_reservations_prevent_double_spending_token_balance() {
        let mut balances = virtual_cash();
        balances.insert("TOKEN".to_string(), "10".to_string());
        let mut sim = PaperSimulator::in_memory(&balances).unwrap();

        let first = sim
            .create_order(CreateOrderRequest {
                signal_id: "reserve-sell-7",
                side: "SELL",
                asset_mint: "TOKEN",
                quantity: "7",
                quote_mint: "USD",
                created_at: now(),
                details: None,
            })
            .unwrap();
        let first_id = first["order_id"].as_str().unwrap();

        assert_eq!(sim.reserved_balance("TOKEN"), BigDecimal::from(7));
        assert_eq!(sim.available_balance("TOKEN"), BigDecimal::from(3));

        let second = sim
            .create_order(CreateOrderRequest {
                signal_id: "reserve-sell-4",
                side: "SELL",
                asset_mint: "TOKEN",
                quantity: "4",
                quote_mint: "USD",
                created_at: now(),
                details: None,
            })
            .unwrap();
        let second_id = second["order_id"].as_str().unwrap();

        assert_eq!(sim.orders()[first_id].status, "CREATED");
        assert_eq!(sim.orders()[second_id].status, "REJECTED");
        assert_eq!(sim.balances()["TOKEN"], BigDecimal::from(10));
        assert_eq!(sim.reserved_balance("TOKEN"), BigDecimal::from(7));
        assert_eq!(sim.available_balance("TOKEN"), BigDecimal::from(3));
    }

    #[test]
    fn cancellation_and_rejection_release_pending_order_reservations() {
        let mut sim = PaperSimulator::in_memory(&virtual_cash()).unwrap();
        let cancelled_id = budget_buy_order(&mut sim, "cancel-reservation", "40");
        assert_eq!(sim.available_balance("USD"), BigDecimal::from(60));

        let cancelled = sim.cancel_order(&cancelled_id).unwrap();
        assert_eq!(cancelled["status"], "CANCELLED");
        assert_eq!(sim.reserved_balance("USD"), BigDecimal::zero());
        assert_eq!(sim.available_balance("USD"), BigDecimal::from(100));
        assert_eq!(
            sim.orders()[&cancelled_id].details["reservation_state"],
            "RELEASED"
        );

        let rejected_id = budget_buy_order(&mut sim, "reject-reservation", "40");
        let invalid_policy = ExecutionPolicy {
            min_order_value_usd: "NaN".to_string(),
            ..ExecutionPolicy::default()
        };
        let rejected = sim
            .execute_order(
                &rejected_id,
                Some(&price("TOKEN")),
                now(),
                &invalid_policy,
                true,
            )
            .unwrap();
        assert_eq!(rejected["status"], "REJECTED");
        assert_eq!(sim.reserved_balance("USD"), BigDecimal::zero());
        assert_eq!(sim.available_balance("USD"), BigDecimal::from(100));
        assert_eq!(
            sim.orders()[&rejected_id].details["reservation_state"],
            "RELEASED"
        );
    }

    #[test]
    fn reservations_survive_restart_and_are_consumed_atomically_with_fill() {
        let directory = tempdir().unwrap();
        let path = directory.path().join("reservations.db");
        let order_id;
        {
            let mut sim = PaperSimulator::open(&path, &virtual_cash()).unwrap();
            order_id = budget_buy_order(&mut sim, "restart-reservation", "50");
            assert_eq!(sim.reserved_balance("USD"), BigDecimal::from(50));
            assert_eq!(sim.available_balance("USD"), BigDecimal::from(50));
        }

        {
            let mut sim = PaperSimulator::open(&path, &BTreeMap::new()).unwrap();
            assert_eq!(sim.reserved_balance("USD"), BigDecimal::from(50));
            assert_eq!(sim.available_balance("USD"), BigDecimal::from(50));

            let result = sim
                .execute_order(
                    &order_id,
                    Some(&price("TOKEN")),
                    now(),
                    &ExecutionPolicy::default(),
                    true,
                )
                .unwrap();
            assert_eq!(result["status"], "FILLED");
            assert_eq!(sim.reserved_balance("USD"), BigDecimal::zero());
            assert_eq!(sim.available_balance("USD"), sim.balances()["USD"]);
            assert_eq!(sim.fills().len(), 1);
        }

        let recovered = PaperSimulator::open(&path, &BTreeMap::new()).unwrap();
        assert_eq!(recovered.reserved_balance("USD"), BigDecimal::zero());
        assert_eq!(recovered.orders()[&order_id].status, "FILLED");
        assert_eq!(recovered.fills().len(), 1);
    }

    #[test]
    fn separate_simulator_instances_cannot_reserve_the_same_virtual_cash() {
        let directory = tempdir().unwrap();
        let path = directory.path().join("concurrent-reservations.db");
        let mut first = PaperSimulator::open(&path, &virtual_cash()).unwrap();
        let mut second = PaperSimulator::open(&path, &BTreeMap::new()).unwrap();

        let first_id = budget_buy_order(&mut first, "instance-one-70", "70");
        assert_eq!(first.orders()[&first_id].status, "CREATED");

        let second_order = second
            .create_order(CreateOrderRequest {
                signal_id: "instance-two-50",
                side: "BUY",
                asset_mint: "TOKEN",
                quantity: "1",
                quote_mint: "USD",
                created_at: now(),
                details: Some(json!({"target_budget_usd":"50"})),
            })
            .unwrap();
        let second_id = second_order["order_id"].as_str().unwrap();

        assert_eq!(second.orders()[second_id].status, "REJECTED");
        assert_eq!(first.reserved_balance("USD"), BigDecimal::from(70));
        assert_eq!(first.available_balance("USD"), BigDecimal::from(30));
        assert_eq!(second.reserved_balance("USD"), BigDecimal::from(70));
        assert_eq!(second.available_balance("USD"), BigDecimal::from(30));
        assert_eq!(first.balances()["USD"], BigDecimal::from(100));
    }

    #[test]
    fn virtual_balance_cannot_be_set_below_pending_reservations() {
        let mut sim = PaperSimulator::in_memory(&virtual_cash()).unwrap();
        let _id = budget_buy_order(&mut sim, "protect-virtual-balance", "60");
        assert!(sim.set_virtual_balance("50", "USD").is_err());
        assert_eq!(sim.balances()["USD"], BigDecimal::from(100));
        assert_eq!(sim.available_balance("USD"), BigDecimal::from(40));
    }

    #[test]
    fn restart_recovers_order_balances_and_fill() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("paper.db");
        let id;
        {
            let mut sim = PaperSimulator::open(&path, &virtual_cash()).unwrap();
            id = buy_order(&mut sim, "restart-signal");
            sim.execute_order(
                &id,
                Some(&price("TOKEN")),
                now(),
                &ExecutionPolicy::default(),
                true,
            )
            .unwrap();
        }
        let sim = PaperSimulator::open(&path, &BTreeMap::new()).unwrap();
        assert_eq!(sim.orders()[&id].status, "FILLED");
        assert_eq!(sim.fills().len(), 1);
        assert_eq!(sim.balances()["TOKEN"], BigDecimal::from(2));
    }

    #[test]
    fn failed_provider_does_not_create_a_fill() {
        let mut sim = PaperSimulator::in_memory(&virtual_cash()).unwrap();
        let id = buy_order(&mut sim, "rpc-fail");
        let result = sim
            .execute_order(
                &id,
                Some(&price("TOKEN")),
                now(),
                &ExecutionPolicy::default(),
                false,
            )
            .unwrap();
        assert_eq!(result["status"], "WAITING_FOR_MARKET_DATA");
        assert_eq!(sim.balances()["USD"].to_string(), "100");
    }
}
