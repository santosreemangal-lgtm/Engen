//! Compatibility port of the legacy persistent PaperTrader API.
//!
//! This ledger is kept separate from the dashboard's price-verified
//! PaperSimulator. It preserves the old transaction-result -> virtual balance
//! API so the legacy paper-monitor CLI can be replaced without changing its
//! accounting contract. It never signs or broadcasts transactions.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::str::FromStr;

use bigdecimal::BigDecimal;
use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use thiserror::Error;

#[derive(Debug, Error)]
pub enum PaperTraderError {
    #[error("Paper trader database operation failed.")]
    Database(#[from] rusqlite::Error),
    #[error("Paper trader filesystem operation failed.")]
    Io(#[from] std::io::Error),
    #[error("Invalid paper trader configuration: {0}")]
    InvalidConfiguration(String),
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PaperTrade {
    pub signature: String,
    pub side: String,
    pub asset_mint: String,
    pub asset_amount: String,
    pub quote_mint: String,
    pub quote_amount: String,
    pub status: String,
}

#[derive(Debug)]
pub struct PaperTrader {
    balances: BTreeMap<String, BigDecimal>,
    processed_signatures: BTreeSet<String>,
    trades: Vec<PaperTrade>,
    db_path: Option<PathBuf>,
    max_position_pct: Option<BigDecimal>,
    max_open_positions: Option<usize>,
}

impl PaperTrader {
    /// Create a deterministic in-memory compatibility ledger.
    pub fn in_memory(
        initial_balances: &BTreeMap<String, String>,
        max_position_pct: Option<&str>,
        max_open_positions: Option<usize>,
    ) -> Result<Self, PaperTraderError> {
        Self::build(initial_balances, None, max_position_pct, max_open_positions)
    }

    /// Open the legacy SQLite paper-ledger schema and restore its saved state.
    pub fn open(
        initial_balances: &BTreeMap<String, String>,
        db_path: impl AsRef<Path>,
        max_position_pct: Option<&str>,
        max_open_positions: Option<usize>,
    ) -> Result<Self, PaperTraderError> {
        Self::build(
            initial_balances,
            Some(db_path.as_ref().to_path_buf()),
            max_position_pct,
            max_open_positions,
        )
    }

    fn build(
        initial_balances: &BTreeMap<String, String>,
        db_path: Option<PathBuf>,
        max_position_pct: Option<&str>,
        max_open_positions: Option<usize>,
    ) -> Result<Self, PaperTraderError> {
        let mut balances = BTreeMap::new();
        for (mint, raw_amount) in initial_balances {
            if mint.trim().is_empty() {
                return Err(PaperTraderError::InvalidConfiguration(
                    "Each balance needs a valid token mint.".to_string(),
                ));
            }
            let amount = BigDecimal::from_str(raw_amount.trim()).map_err(|_| {
                PaperTraderError::InvalidConfiguration("Invalid initial balance.".to_string())
            })?;
            if amount < BigDecimal::from(0) {
                return Err(PaperTraderError::InvalidConfiguration(
                    "Initial balances must be finite and non-negative.".to_string(),
                ));
            }
            balances.insert(mint.clone(), amount);
        }

        let max_position_pct = max_position_pct
            .map(|raw| {
                let limit = BigDecimal::from_str(raw.trim()).map_err(|_| {
                    PaperTraderError::InvalidConfiguration(
                        "Maximum position percentage must be numeric.".to_string(),
                    )
                })?;
                if limit <= BigDecimal::from(0) || limit > BigDecimal::from(100) {
                    return Err(PaperTraderError::InvalidConfiguration(
                        "Maximum position percentage must be greater than 0 and at most 100."
                            .to_string(),
                    ));
                }
                Ok(limit)
            })
            .transpose()?;

        if max_open_positions.is_some_and(|count| !(1..=100).contains(&count)) {
            return Err(PaperTraderError::InvalidConfiguration(
                "Maximum open positions must be an integer from 1 to 100.".to_string(),
            ));
        }

        let mut trader = Self {
            balances,
            processed_signatures: BTreeSet::new(),
            trades: Vec::new(),
            db_path,
            max_position_pct,
            max_open_positions,
        };
        if trader.db_path.is_some() {
            trader.initialize_storage()?;
        }
        Ok(trader)
    }

    fn connect(&self) -> Result<Connection, PaperTraderError> {
        match &self.db_path {
            Some(path) => Ok(Connection::open(path)?),
            None => unreachable!("in-memory ledgers do not open a database"),
        }
    }

    fn initialize_storage(&mut self) -> Result<(), PaperTraderError> {
        let path = self.db_path.as_ref().expect("checked by caller");
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
        }
        let mut connection = Connection::open(path)?;
        connection.execute_batch(
            "CREATE TABLE IF NOT EXISTS paper_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS paper_balances (
                mint TEXT PRIMARY KEY,
                amount TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signature TEXT NOT NULL UNIQUE,
                side TEXT NOT NULL,
                asset_mint TEXT NOT NULL,
                asset_amount TEXT NOT NULL,
                quote_mint TEXT NOT NULL,
                quote_amount TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );",
        )?;

        let initialized: Option<String> = connection
            .query_row(
                "SELECT value FROM paper_meta WHERE key = 'initialized'",
                [],
                |row| row.get(0),
            )
            .optional()?;

        if initialized.is_none() {
            let transaction = connection.transaction()?;
            for (mint, amount) in &self.balances {
                transaction.execute(
                    "INSERT INTO paper_balances (mint, amount) VALUES (?1, ?2)",
                    params![mint, amount.to_string()],
                )?;
            }
            transaction.execute(
                "INSERT INTO paper_meta (key, value) VALUES ('initialized', '1')",
                [],
            )?;
            transaction.commit()?;
        } else {
            let mut statement =
                connection.prepare("SELECT mint, amount FROM paper_balances ORDER BY mint")?;
            let rows = statement.query_map([], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
            })?;
            let mut restored = BTreeMap::new();
            for row in rows {
                let (mint, raw_amount) = row?;
                let amount = BigDecimal::from_str(&raw_amount).map_err(|_| {
                    rusqlite::Error::FromSqlConversionFailure(
                        raw_amount.len(),
                        rusqlite::types::Type::Text,
                        Box::new(std::io::Error::new(
                            std::io::ErrorKind::InvalidData,
                            "invalid paper balance decimal",
                        )),
                    )
                })?;
                restored.insert(mint, amount);
            }
            self.balances = restored;
        }

        let mut statement = connection.prepare(
            "SELECT signature, side, asset_mint, asset_amount, quote_mint, quote_amount, status
             FROM paper_trades ORDER BY id",
        )?;
        let rows = statement.query_map([], |row| {
            Ok(PaperTrade {
                signature: row.get(0)?,
                side: row.get(1)?,
                asset_mint: row.get(2)?,
                asset_amount: row.get(3)?,
                quote_mint: row.get(4)?,
                quote_amount: row.get(5)?,
                status: row.get(6)?,
            })
        })?;
        self.trades.clear();
        self.processed_signatures.clear();
        for row in rows {
            let trade = row?;
            self.processed_signatures
                .insert(trade.signature.clone());
            self.trades.push(trade);
        }
        Ok(())
    }

    fn delta_for_mint(changes: &[Value], mint: &str) -> Option<BigDecimal> {
        let mut decimals = None;
        let mut raw_total = BigDecimal::from(0);
        let mut count = 0usize;

        for item in changes {
            if item.get("mint").and_then(Value::as_str) != Some(mint) {
                continue;
            }
            let raw = item.get("delta_amount")?.as_str()?;
            let digits = raw.strip_prefix('-').unwrap_or(raw);
            if digits.is_empty() || !digits.bytes().all(|byte| byte.is_ascii_digit()) {
                return None;
            }
            let current_decimals = item.get("decimals")?.as_u64()?;
            if decimals.is_some_and(|previous| previous != current_decimals) {
                return None;
            }
            decimals = Some(current_decimals);
            raw_total += BigDecimal::from_str(raw).ok()?;
            count += 1;
        }

        if count == 0 {
            return None;
        }
        let places = decimals?;
        let factor = BigDecimal::from_str(&format!("1{}", "0".repeat(places as usize))).ok()?;
        Some(raw_total / factor)
    }

    fn persist_trade(
        &self,
        trade: &PaperTrade,
        balances: &BTreeMap<String, BigDecimal>,
    ) -> Result<(), PaperTraderError> {
        let Some(path) = &self.db_path else {
            return Ok(());
        };
        let mut connection = Connection::open(path)?;
        let transaction = connection.transaction()?;
        for (mint, amount) in balances {
            transaction.execute(
                "INSERT INTO paper_balances (mint, amount) VALUES (?1, ?2)
                 ON CONFLICT(mint) DO UPDATE SET amount = excluded.amount",
                params![mint, amount.to_string()],
            )?;
        }
        transaction.execute(
            "INSERT INTO paper_trades (
                signature, side, asset_mint, asset_amount,
                quote_mint, quote_amount, status
             ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
            params![
                trade.signature,
                trade.side,
                trade.asset_mint,
                trade.asset_amount,
                trade.quote_mint,
                trade.quote_amount,
                trade.status
            ],
        )?;
        transaction.commit()?;
        Ok(())
    }

    /// Apply one legacy monitor result to the virtual ledger.
    ///
    /// Invalid/unavailable/unsupported results are returned as SKIPPED, repeated
    /// signatures as DUPLICATE, risk/balance violations as REJECTED, and a
    /// successful atomic virtual-ledger update as PAPER_FILLED.
    pub fn process(&mut self, result: &Value) -> Result<Value, PaperTraderError> {
        if !result.is_object() {
            return Ok(json!({"status":"SKIPPED","reason":"Invalid transaction result."}));
        }

        let Some(signature) = result.get("signature").and_then(Value::as_str) else {
            return Ok(json!({"status":"SKIPPED","reason":"Missing transaction signature."}));
        };
        let signature = signature.trim();
        if signature.is_empty() {
            return Ok(json!({"status":"SKIPPED","reason":"Missing transaction signature."}));
        }
        if self.processed_signatures.contains(signature) {
            return Ok(json!({"status":"DUPLICATE","signature":signature}));
        }
        if result.get("available") != Some(&Value::Bool(true))
            || result.get("status").and_then(Value::as_str) != Some("success")
        {
            return Ok(json!({
                "status":"SKIPPED",
                "reason":"Transaction is unavailable or unsuccessful."
            }));
        }

        let Some(signal) = result.get("trade_signal").filter(|value| value.is_object()) else {
            return Ok(json!({"status":"SKIPPED","reason":"No trade signal."}));
        };
        let action = signal.get("classification").and_then(Value::as_str).unwrap_or("");
        if !matches!(action, "BUY_CANDIDATE" | "SELL_CANDIDATE") {
            return Ok(json!({
                "status":"SKIPPED",
                "reason":"Signal is not a supported buy or sell."
            }));
        }

        let assets = signal.get("asset_mints").and_then(Value::as_array);
        let quotes = signal.get("quote_mints").and_then(Value::as_array);
        let changes = result.get("token_balance_changes").and_then(Value::as_array);
        let (Some(assets), Some(quotes), Some(changes)) = (assets, quotes, changes) else {
            return Ok(json!({
                "status":"SKIPPED",
                "reason":"Only one asset and one quote token are supported."
            }));
        };
        if assets.len() != 1 || quotes.len() != 1 {
            return Ok(json!({
                "status":"SKIPPED",
                "reason":"Only one asset and one quote token are supported."
            }));
        }
        let (Some(asset_mint), Some(quote_mint)) = (assets[0].as_str(), quotes[0].as_str())
        else {
            return Ok(json!({"status":"SKIPPED","reason":"Invalid token mints."}));
        };
        if asset_mint.trim().is_empty()
            || quote_mint.trim().is_empty()
            || asset_mint == quote_mint
        {
            return Ok(json!({"status":"SKIPPED","reason":"Invalid token mints."}));
        }

        let (Some(asset_delta), Some(quote_delta)) = (
            Self::delta_for_mint(changes, asset_mint),
            Self::delta_for_mint(changes, quote_mint),
        ) else {
            return Ok(json!({
                "status":"SKIPPED",
                "reason":"Token amounts are unavailable or invalid."
            }));
        };

        let zero = BigDecimal::from(0);
        let asset_balance = self
            .balances
            .get(asset_mint)
            .cloned()
            .unwrap_or_else(|| zero.clone());
        let quote_balance = self
            .balances
            .get(quote_mint)
            .cloned()
            .unwrap_or_else(|| zero.clone());

        let (side, asset_amount, quote_amount, new_asset_balance, new_quote_balance) =
            if action == "BUY_CANDIDATE" {
                if asset_delta <= zero || quote_delta >= zero {
                    return Ok(json!({"status":"SKIPPED","reason":"Buy direction is inconsistent."}));
                }
                let asset_amount = asset_delta;
                let quote_amount = -quote_delta;
                if let Some(limit) = &self.max_position_pct {
                    let maximum = &quote_balance * limit / BigDecimal::from(100);
                    if quote_amount > maximum {
                        return Ok(json!({
                            "status":"REJECTED",
                            "reason":"Buy exceeds this profile's maximum position percentage."
                        }));
                    }
                }
                if self.max_open_positions.is_some() && asset_balance <= zero {
                    let open = self
                        .balances
                        .iter()
                        .filter(|(mint, amount)| mint.as_str() != quote_mint && **amount > zero)
                        .count();
                    if open >= self.max_open_positions.unwrap_or(0) {
                        return Ok(json!({
                            "status":"REJECTED",
                            "reason":"This profile's maximum open paper positions has been reached."
                        }));
                    }
                }
                if quote_balance < quote_amount {
                    return Ok(json!({
                        "status":"REJECTED",
                        "reason":"Insufficient paper quote balance."
                    }));
                }
                (
                    "BUY",
                    asset_amount.clone(),
                    quote_amount.clone(),
                    asset_balance + asset_amount,
                    quote_balance - quote_amount,
                )
            } else {
                if asset_delta >= zero || quote_delta <= zero {
                    return Ok(json!({"status":"SKIPPED","reason":"Sell direction is inconsistent."}));
                }
                let asset_amount = -asset_delta;
                let quote_amount = quote_delta;
                if asset_balance < asset_amount {
                    return Ok(json!({
                        "status":"REJECTED",
                        "reason":"Insufficient paper asset balance."
                    }));
                }
                (
                    "SELL",
                    asset_amount.clone(),
                    quote_amount.clone(),
                    asset_balance - asset_amount,
                    quote_balance + quote_amount,
                )
            };

        let trade = PaperTrade {
            signature: signature.to_string(),
            side: side.to_string(),
            asset_mint: asset_mint.to_string(),
            asset_amount: asset_amount.to_string(),
            quote_mint: quote_mint.to_string(),
            quote_amount: quote_amount.to_string(),
            status: "PAPER_FILLED".to_string(),
        };
        let mut new_balances = self.balances.clone();
        new_balances.insert(asset_mint.to_string(), new_asset_balance);
        new_balances.insert(quote_mint.to_string(), new_quote_balance);

        // Persist both changed balances and the unique signature record atomically.
        self.persist_trade(&trade, &new_balances)?;
        self.balances = new_balances;
        self.processed_signatures.insert(signature.to_string());
        self.trades.push(trade.clone());
        Ok(serde_json::to_value(trade).unwrap_or_else(|_| json!({"status":"ERROR"})))
    }

    pub fn get_balances(&self) -> Value {
        Value::Object(
            self.balances
                .iter()
                .map(|(mint, amount)| (mint.clone(), Value::String(amount.to_string())))
                .collect(),
        )
    }

    pub fn trades(&self) -> &[PaperTrade] {
        &self.trades
    }

    pub fn processed_signatures(&self) -> &BTreeSet<String> {
        &self.processed_signatures
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    const QUOTE: &str = "USDC";
    const ASSET: &str = "TOKEN_A";

    fn balances(amount: &str) -> BTreeMap<String, String> {
        BTreeMap::from([(QUOTE.to_string(), amount.to_string())])
    }

    fn buy_result(signature: &str) -> Value {
        json!({
            "signature":signature,
            "available":true,
            "status":"success",
            "trade_signal":{
                "classification":"BUY_CANDIDATE",
                "asset_mints":[ASSET],
                "quote_mints":[QUOTE]
            },
            "token_balance_changes":[
                {"mint":ASSET,"delta_amount":"500","decimals":2},
                {"mint":QUOTE,"delta_amount":"-1000000","decimals":6}
            ]
        })
    }

    #[test]
    fn buy_updates_virtual_balances_and_duplicate_is_rejected() {
        let mut trader = PaperTrader::in_memory(&balances("10"), None, None).unwrap();
        let result = trader.process(&buy_result("demo-signature")).unwrap();
        assert_eq!(result["status"], "PAPER_FILLED");
        assert_eq!(trader.get_balances()["USDC"], "9");
        assert_eq!(trader.get_balances()["TOKEN_A"], "5");

        let duplicate = trader.process(&buy_result("demo-signature")).unwrap();
        assert_eq!(duplicate["status"], "DUPLICATE");
        assert_eq!(trader.trades().len(), 1);
    }

    #[test]
    fn rejects_overspend_and_skips_unknown_signal_without_mutation() {
        let mut poor = PaperTrader::in_memory(&balances("0.25"), None, None).unwrap();
        let result = poor.process(&buy_result("too-expensive")).unwrap();
        assert_eq!(result["status"], "REJECTED");
        assert_eq!(poor.get_balances()["USDC"], "0.25");

        let mut unknown = buy_result("unknown-signal");
        unknown["trade_signal"]["classification"] = json!("UNKNOWN");
        let skipped = poor.process(&unknown).unwrap();
        assert_eq!(skipped["status"], "SKIPPED");
        assert_eq!(poor.trades().len(), 0);
    }

    #[test]
    fn sqlite_state_survives_restart_and_duplicate_detection() {
        let directory = tempdir().unwrap();
        let path = directory.path().join("paper.db");
        {
            let mut trader =
                PaperTrader::open(&balances("10"), &path, None, None).unwrap();
            assert_eq!(
                trader.process(&buy_result("restart-signature")).unwrap()["status"],
                "PAPER_FILLED"
            );
        }
        let mut restarted = PaperTrader::open(&balances("999"), &path, None, None).unwrap();
        assert_eq!(restarted.get_balances()["USDC"], "9");
        assert_eq!(restarted.get_balances()["TOKEN_A"], "5");
        assert_eq!(restarted.trades().len(), 1);
        assert_eq!(
            restarted.process(&buy_result("restart-signature")).unwrap()["status"],
            "DUPLICATE"
        );
        assert_eq!(restarted.trades().len(), 1);
    }

    #[test]
    fn profile_risk_limits_and_sell_direction_match_legacy_api() {
        let mut limited = PaperTrader::in_memory(&balances("10"), Some("5"), None).unwrap();
        let rejected = limited.process(&buy_result("risk-limit")).unwrap();
        assert_eq!(rejected["status"], "REJECTED");

        let mut trader = PaperTrader::in_memory(&balances("0"), None, None).unwrap();
        let mut initial = trader.balances.clone();
        initial.insert(ASSET.to_string(), BigDecimal::from(5));
        trader.balances = initial;
        let sell = json!({
            "signature":"sell-signature",
            "available":true,
            "status":"success",
            "trade_signal":{"classification":"SELL_CANDIDATE","asset_mints":[ASSET],"quote_mints":[QUOTE]},
            "token_balance_changes":[
                {"mint":ASSET,"delta_amount":"-250","decimals":2},
                {"mint":QUOTE,"delta_amount":"500000","decimals":6}
            ]
        });
        assert_eq!(trader.process(&sell).unwrap()["status"], "PAPER_FILLED");
        assert_eq!(trader.get_balances()["TOKEN_A"], "2.5");
        assert_eq!(trader.get_balances()["USDC"], "0.5");
    }

    #[test]
    fn max_open_positions_rejects_a_new_token_when_cap_is_reached() {
        let initial = BTreeMap::from([
            (QUOTE.to_string(), "10".to_string()),
            ("TOKEN_B".to_string(), "1".to_string()),
        ]);
        let mut trader = PaperTrader::in_memory(&initial, None, Some(1)).unwrap();
        let rejected = trader.process(&buy_result("position-cap")).unwrap();
        assert_eq!(rejected["status"], "REJECTED");
        assert_eq!(trader.trades().len(), 0);
    }

    #[test]
    fn invalid_risk_limit_configuration_is_rejected() {
        for value in ["0", "-1", "100.1", "NaN", "invalid"] {
            assert!(PaperTrader::in_memory(&balances("10"), Some(value), None).is_err());
        }
        for value in [0, 101] {
            assert!(PaperTrader::in_memory(&balances("10"), None, Some(value)).is_err());
        }
    }

    #[test]
    fn persist_native_sol_quote_and_restore_it() {
        let directory = tempdir().unwrap();
        let path = directory.path().join("native-sol-paper.db");
        let native_sol = crate::transaction_analysis::WRAPPED_SOL_MINT;
        let initial = BTreeMap::from([(native_sol.to_string(), "1".to_string())]);
        let mut trader = PaperTrader::open(&initial, &path, None, None).unwrap();
        let transaction = json!({
            "signature":"native-sol-buy-signature",
            "available":true,
            "status":"success",
            "trade_signal":{
                "classification":"BUY_CANDIDATE",
                "asset_mints":[ASSET],
                "quote_mints":[native_sol]
            },
            "token_balance_changes":[
                {"mint":ASSET,"delta_amount":"12851247","decimals":6,"owner":"WALLET_A"},
                {"mint":native_sol,"delta_amount":"-200000000","decimals":9,"owner":"WALLET_A","source":"native_sol","fee_adjusted":true}
            ]
        });
        assert_eq!(trader.process(&transaction).unwrap()["status"], "PAPER_FILLED");
        drop(trader);
        let restored = PaperTrader::open(&initial, &path, None, None).unwrap();
        assert_eq!(restored.get_balances()[native_sol], "0.8");
        assert_eq!(restored.get_balances()[ASSET], "12.851247");
        assert_eq!(restored.trades().len(), 1);
    }
}

