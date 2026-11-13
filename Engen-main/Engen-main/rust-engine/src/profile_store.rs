//! Durable SQLite profile, source-wallet, transaction-link and balance-snapshot store.
//!
//! The schema is intentionally compatible with the Python ProfileStore schema (user_version=1).
//! Opening this store is an explicit runtime action; migration code and tests must use temporary
//! paths and must never open, rewrite, or delete a user's data directory.

use std::collections::{BTreeMap, BTreeSet, HashSet};
use std::path::{Path, PathBuf};
use std::str::FromStr;
use std::time::Duration;

use bigdecimal::BigDecimal;
use chrono::{DateTime, SecondsFormat, Utc};
use num_traits::Zero;
use rusqlite::{backup::Backup, params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use thiserror::Error;
use uuid::Uuid;

use crate::solana_validation::require_solana_address;

const LABELS: &[&str] = &[
    "BUY_CANDIDATE",
    "SELL_CANDIDATE",
    "TRANSFER",
    "UNKNOWN",
    "FAILED_TRANSACTION",
    "RENT",
    "LIQUIDITY_OPERATION",
];
const STATUSES: &[&str] = &["observed", "confirmed", "failed", "pending", "unknown"];
const PROFILE_STATUSES: &[&str] = &["disabled", "active", "archived"];
const FORBIDDEN_SETTINGS_KEYS: &[&str] = &[
    "private_key",
    "secret_key",
    "seed_phrase",
    "recovery_phrase",
    "mnemonic",
];

#[derive(Debug, Error)]
pub enum ProfileStoreError {
    #[error("SQLite operation failed.")]
    Database(#[from] rusqlite::Error),
    #[error("JSON serialization or parsing failed.")]
    Json(#[from] serde_json::Error),
    #[error("Invalid profile-store input: {0}")]
    InvalidInput(String),
    #[error("Timestamp must be a timezone-aware ISO-8601 value.")]
    InvalidTimestamp,
    #[error("Exact amount is invalid.")]
    InvalidAmount,
    #[error("Backup operation failed.")]
    Backup,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Profile {
    pub profile_id: String,
    pub name: String,
    pub destination_public_address: Option<String>,
    pub status: String,
    pub created_at: String,
    pub updated_at: String,
    pub deleted_at: Option<String>,
    pub settings: Value,
    pub source_wallets: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ProfileTransaction {
    pub signature: String,
    pub source_wallet_address: String,
    pub slot: Option<i64>,
    pub block_time: Option<i64>,
    pub observed_at: String,
    pub status: String,
    pub classification: String,
    pub evidence: Value,
    pub raw_amount: Option<String>,
    pub token_mint: Option<String>,
    pub token_decimals: Option<i64>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct BalanceSnapshot {
    pub snapshot_id: String,
    pub profile_id: String,
    pub wallet_address: String,
    pub token_mint: String,
    pub raw_amount: String,
    pub token_decimals: i64,
    pub observed_at: String,
    pub source: String,
    pub freshness_seconds: Option<i64>,
    pub is_current: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct PortfolioHolding {
    pub token_mint: String,
    pub token_decimals: i64,
    pub raw_amount: String,
    pub amount: String,
    pub wallet_count: usize,
    pub oldest_observed_at: String,
    pub newest_observed_at: String,
    pub metadata: Option<Value>,
    pub provenance: Vec<String>,
}

#[derive(Debug, Clone)]
pub struct SaveTransaction<'a> {
    pub signature: &'a str,
    pub source_wallet_address: &'a str,
    pub classification: &'a str,
    pub status: &'a str,
    pub evidence: Option<Value>,
    pub slot: Option<i64>,
    pub block_time: Option<i64>,
    pub raw_amount: Option<&'a str>,
    pub token_mint: Option<&'a str>,
    pub token_decimals: Option<i64>,
    pub profile_ids: &'a [String],
    pub observed_at: Option<&'a str>,
}

#[derive(Debug, Clone)]
pub struct SaveBalanceSnapshot<'a> {
    pub profile_id: &'a str,
    pub wallet_address: &'a str,
    pub token_mint: &'a str,
    pub raw_amount: &'a str,
    pub token_decimals: i64,
    pub source: &'a str,
    pub observed_at: Option<&'a str>,
    pub freshness_seconds: Option<i64>,
    pub is_current: bool,
}

#[derive(Debug, Clone)]
struct StoredTransaction {
    source_wallet_address: String,
    status: String,
    classification: String,
    evidence: Value,
    raw_amount: Option<String>,
    token_mint: Option<String>,
    token_decimals: Option<i64>,
}

#[derive(Debug, Default)]
struct HoldingAccumulator {
    token_decimals: Option<i64>,
    raw_amount: BigDecimal,
    amount: BigDecimal,
    wallet_count: usize,
    oldest_observed_at: Option<String>,
    newest_observed_at: Option<String>,
    provenance: BTreeSet<String>,
}

pub struct ProfileStore {
    path: PathBuf,
}

impl ProfileStore {
    pub fn open(path: impl AsRef<Path>) -> Result<Self, ProfileStoreError> {
        let path = path.as_ref().to_path_buf();
        if let Some(parent) = path.parent() {
            if !parent.as_os_str().is_empty() {
                std::fs::create_dir_all(parent).map_err(|_| {
                    ProfileStoreError::InvalidInput(
                        "Unable to create profile database directory.".to_string(),
                    )
                })?;
            }
        }
        let store = Self { path };
        let mut connection = store.connect()?;
        Self::initialize(&mut connection)?;
        Ok(store)
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    fn connect(&self) -> Result<Connection, ProfileStoreError> {
        let connection = Connection::open(&self.path)?;
        connection.busy_timeout(Duration::from_secs(10))?;
        connection.execute_batch("PRAGMA foreign_keys=ON; PRAGMA busy_timeout=10000;")?;
        Ok(connection)
    }

    fn initialize(connection: &mut Connection) -> Result<(), ProfileStoreError> {
        connection.execute_batch("PRAGMA journal_mode=WAL;")?;
        let version: i64 = connection.query_row("PRAGMA user_version", [], |row| row.get(0))?;
        if version > 1 {
            return Err(ProfileStoreError::InvalidInput(
                "Database schema is newer than this application.".to_string(),
            ));
        }
        if version == 0 {
            let transaction = connection.transaction()?;
            transaction.execute_batch(
                "
                CREATE TABLE profiles(
                  profile_id TEXT PRIMARY KEY, name TEXT NOT NULL,
                  destination_public_address TEXT,
                  status TEXT NOT NULL DEFAULT 'disabled'
                    CHECK(status IN ('disabled','active','archived')),
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, deleted_at TEXT);
                CREATE TABLE profile_settings(
                  profile_id TEXT PRIMARY KEY REFERENCES profiles(profile_id),
                  settings_json TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE source_wallets(
                  wallet_address TEXT PRIMARY KEY, created_at TEXT NOT NULL);
                CREATE TABLE profile_source_wallets(
                  profile_id TEXT NOT NULL REFERENCES profiles(profile_id),
                  wallet_address TEXT NOT NULL REFERENCES source_wallets(wallet_address),
                  associated_at TEXT NOT NULL, PRIMARY KEY(profile_id,wallet_address));
                CREATE TABLE transactions(
                  signature TEXT PRIMARY KEY, source_wallet_address TEXT NOT NULL,
                  slot INTEGER, block_time INTEGER, observed_at TEXT NOT NULL,
                  status TEXT NOT NULL CHECK(status IN
                    ('observed','confirmed','failed','pending','unknown')),
                  classification TEXT NOT NULL CHECK(classification IN
                    ('BUY_CANDIDATE','SELL_CANDIDATE','TRANSFER','UNKNOWN',
                     'FAILED_TRANSACTION','RENT','LIQUIDITY_OPERATION')),
                  evidence_json TEXT NOT NULL, raw_amount TEXT, token_mint TEXT,
                  token_decimals INTEGER,
                  CHECK(slot IS NULL OR slot>=0),
                  CHECK(token_decimals IS NULL OR token_decimals BETWEEN 0 AND 30));
                CREATE TABLE profile_transactions(
                  profile_id TEXT NOT NULL REFERENCES profiles(profile_id),
                  signature TEXT NOT NULL REFERENCES transactions(signature),
                  linked_at TEXT NOT NULL, PRIMARY KEY(profile_id,signature));
                CREATE TABLE balance_snapshots(
                  snapshot_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL REFERENCES profiles(profile_id),
                  wallet_address TEXT NOT NULL REFERENCES source_wallets(wallet_address), token_mint TEXT NOT NULL,
                  raw_amount TEXT NOT NULL, token_decimals INTEGER NOT NULL,
                  observed_at TEXT NOT NULL, source TEXT NOT NULL,
                  freshness_seconds INTEGER, is_current INTEGER NOT NULL DEFAULT 0
                    CHECK(is_current IN (0,1)));
                CREATE INDEX idx_balance_history ON balance_snapshots(profile_id,token_mint,observed_at);
                CREATE INDEX idx_transactions_observed ON transactions(observed_at);
                PRAGMA user_version=1;
                ",
            )?;
            transaction.commit()?;
        }
        Ok(())
    }

    pub fn create_profile(
        &self,
        name: &str,
        destination_public_address: Option<&str>,
        settings: Option<Value>,
    ) -> Result<String, ProfileStoreError> {
        let name = validate_profile_name(name)?;
        if let Some(address) = destination_public_address {
            validate_address(address, "destination public address")?;
        }
        let settings = settings.unwrap_or_else(|| Value::Object(Map::new()));
        let settings_json = serialize_settings(&settings)?;
        let profile_id = Uuid::new_v4().to_string();
        let now = utc_now();
        let mut connection = self.connect()?;
        let transaction = connection.transaction()?;
        let active_count: i64 = transaction.query_row(
            "SELECT COUNT(*) FROM profiles WHERE deleted_at IS NULL AND status != 'archived'",
            [],
            |row| row.get(0),
        )?;
        if active_count >= 3 {
            return Err(ProfileStoreError::InvalidInput(
                "A maximum of 3 destination wallet profiles is allowed. Archive or delete one before creating another."
                    .to_string(),
            ));
        }
        transaction.execute(
            "INSERT INTO profiles VALUES(?,?,?,'disabled',?,?,NULL)",
            params![profile_id, name, destination_public_address, now, now],
        )?;
        transaction.execute(
            "INSERT INTO profile_settings VALUES(?,?,?)",
            params![profile_id, settings_json, now],
        )?;
        transaction.commit()?;
        Ok(profile_id)
    }

    pub fn get_profile(&self, profile_id: &str) -> Result<Option<Profile>, ProfileStoreError> {
        let connection = self.connect()?;
        let row = connection
            .query_row(
                "SELECT profile_id,name,destination_public_address,status,created_at,updated_at,deleted_at
                 FROM profiles WHERE profile_id=? AND deleted_at IS NULL",
                [profile_id],
                |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, Option<String>>(2)?,
                        row.get::<_, String>(3)?,
                        row.get::<_, String>(4)?,
                        row.get::<_, String>(5)?,
                        row.get::<_, Option<String>>(6)?,
                    ))
                },
            )
            .optional()?;
        let Some((
            profile_id,
            name,
            destination_public_address,
            status,
            created_at,
            updated_at,
            deleted_at,
        )) = row
        else {
            return Ok(None);
        };
        let settings_json: Option<String> = connection
            .query_row(
                "SELECT settings_json FROM profile_settings WHERE profile_id=?",
                [&profile_id],
                |row| row.get(0),
            )
            .optional()?;
        let settings = match settings_json {
            Some(value) => serde_json::from_str(&value)?,
            None => Value::Object(Map::new()),
        };
        let mut statement = connection.prepare(
            "SELECT wallet_address FROM profile_source_wallets WHERE profile_id=? ORDER BY wallet_address",
        )?;
        let source_wallets = statement
            .query_map([&profile_id], |row| row.get(0))?
            .collect::<Result<Vec<String>, _>>()?;
        Ok(Some(Profile {
            profile_id,
            name,
            destination_public_address,
            status,
            created_at,
            updated_at,
            deleted_at,
            settings,
            source_wallets,
        }))
    }

    pub fn list_profiles(&self, include_archived: bool) -> Result<Vec<Profile>, ProfileStoreError> {
        let connection = self.connect()?;
        let sql = if include_archived {
            "SELECT profile_id FROM profiles WHERE deleted_at IS NULL ORDER BY created_at,profile_id"
        } else {
            "SELECT profile_id FROM profiles WHERE deleted_at IS NULL AND status!='archived' ORDER BY created_at,profile_id"
        };
        let mut statement = connection.prepare(sql)?;
        let ids = statement
            .query_map([], |row| row.get::<_, String>(0))?
            .collect::<Result<Vec<_>, _>>()?;
        drop(statement);
        drop(connection);
        ids.iter()
            .filter_map(|id| match self.get_profile(id) {
                Ok(Some(profile)) => Some(Ok(profile)),
                Ok(None) => None,
                Err(error) => Some(Err(error)),
            })
            .collect()
    }

    pub fn update_profile(
        &self,
        profile_id: &str,
        name: Option<&str>,
        destination_public_address: Option<&str>,
        status: Option<&str>,
    ) -> Result<Option<Profile>, ProfileStoreError> {
        let name = name.map(validate_profile_name).transpose()?;
        if let Some(address) = destination_public_address {
            validate_address(address, "destination public address")?;
        }
        if let Some(value) = status {
            if !PROFILE_STATUSES.contains(&value) {
                return Err(ProfileStoreError::InvalidInput(
                    "Invalid profile status.".to_string(),
                ));
            }
        }
        if name.is_none() && destination_public_address.is_none() && status.is_none() {
            return self.get_profile(profile_id);
        }
        let connection = self.connect()?;
        let changed = connection.execute(
            "UPDATE profiles SET name=COALESCE(?1,name),
                destination_public_address=COALESCE(?2,destination_public_address),
                status=COALESCE(?3,status),updated_at=?4
             WHERE profile_id=?5 AND deleted_at IS NULL",
            params![
                name,
                destination_public_address,
                status,
                utc_now(),
                profile_id
            ],
        )?;
        if changed != 1 {
            return Err(ProfileStoreError::InvalidInput(
                "Profile not found.".to_string(),
            ));
        }
        drop(connection);
        self.get_profile(profile_id)
    }

    /// Validate settings without changing persistent profile data.
    pub fn validate_settings(settings: &Value) -> Result<(), ProfileStoreError> {
        serialize_settings(settings).map(|_| ())
    }

    pub fn set_settings(
        &self,
        profile_id: &str,
        settings: &Value,
    ) -> Result<(), ProfileStoreError> {
        let payload = serialize_settings(settings)?;
        let now = utc_now();
        let mut connection = self.connect()?;
        let transaction = connection.transaction()?;
        let exists: bool = transaction.query_row(
            "SELECT EXISTS(SELECT 1 FROM profiles WHERE profile_id=? AND deleted_at IS NULL)",
            [profile_id],
            |row| row.get(0),
        )?;
        if !exists {
            return Err(ProfileStoreError::InvalidInput(
                "Profile not found.".to_string(),
            ));
        }
        transaction.execute(
            "UPDATE profile_settings SET settings_json=?,updated_at=? WHERE profile_id=?",
            params![payload, now, profile_id],
        )?;
        transaction.execute(
            "UPDATE profiles SET updated_at=? WHERE profile_id=?",
            params![now, profile_id],
        )?;
        transaction.commit()?;
        Ok(())
    }

    pub fn associate_source_wallet(
        &self,
        profile_id: &str,
        wallet_address: &str,
    ) -> Result<(), ProfileStoreError> {
        validate_address(wallet_address, "wallet address")?;
        let now = utc_now();
        let mut connection = self.connect()?;
        let transaction = connection.transaction()?;
        let exists: bool = transaction.query_row(
            "SELECT EXISTS(SELECT 1 FROM profiles WHERE profile_id=? AND deleted_at IS NULL)",
            [profile_id],
            |row| row.get(0),
        )?;
        if !exists {
            return Err(ProfileStoreError::InvalidInput(
                "Profile not found.".to_string(),
            ));
        }
        transaction.execute(
            "INSERT OR IGNORE INTO source_wallets VALUES(?,?)",
            params![wallet_address, now],
        )?;
        transaction.execute(
            "INSERT OR IGNORE INTO profile_source_wallets VALUES(?,?,?)",
            params![profile_id, wallet_address, now],
        )?;
        transaction.commit()?;
        Ok(())
    }

    pub fn disassociate_source_wallet(
        &self,
        profile_id: &str,
        wallet_address: &str,
    ) -> Result<(), ProfileStoreError> {
        validate_address(wallet_address, "wallet address")?;
        let connection = self.connect()?;
        let exists: bool = connection.query_row(
            "SELECT EXISTS(SELECT 1 FROM profiles WHERE profile_id=? AND deleted_at IS NULL)",
            [profile_id],
            |row| row.get(0),
        )?;
        if !exists {
            return Err(ProfileStoreError::InvalidInput(
                "Profile not found.".to_string(),
            ));
        }
        connection.execute(
            "DELETE FROM profile_source_wallets WHERE profile_id=? AND wallet_address=?",
            params![profile_id, wallet_address],
        )?;
        Ok(())
    }

    pub fn save_transaction(
        &self,
        input: SaveTransaction<'_>,
    ) -> Result<String, ProfileStoreError> {
        if input.signature.trim().is_empty() {
            return Err(ProfileStoreError::InvalidInput(
                "Transaction signature is required.".to_string(),
            ));
        }
        validate_address(input.source_wallet_address, "wallet address")?;
        if !LABELS.contains(&input.classification) || !STATUSES.contains(&input.status) {
            return Err(ProfileStoreError::InvalidInput(
                "Invalid transaction label or status.".to_string(),
            ));
        }
        if input.slot.is_some_and(|value| value < 0)
            || input.block_time.is_some_and(|value| value < 0)
            || input
                .token_decimals
                .is_some_and(|value| !(0..=30).contains(&value))
        {
            return Err(ProfileStoreError::InvalidInput(
                "Invalid slot, block time, or token decimals.".to_string(),
            ));
        }
        let raw_amount = input.raw_amount.map(exact_amount).transpose()?;
        if let Some(mint) = input.token_mint {
            validate_address(mint, "token mint")?;
        }
        let evidence = input.evidence.unwrap_or_else(|| Value::Object(Map::new()));
        let evidence_json = serialize_object(&evidence, "Transaction evidence must be an object.")?;
        let observed_at = validate_timestamp(input.observed_at.unwrap_or(&utc_now()))?;
        let now = utc_now();

        let mut connection = self.connect()?;
        let transaction = connection.transaction()?;
        transaction.execute(
            "INSERT OR IGNORE INTO source_wallets VALUES(?,?)",
            params![input.source_wallet_address, now],
        )?;
        transaction.execute(
            "INSERT OR IGNORE INTO transactions
             VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            params![
                input.signature,
                input.source_wallet_address,
                input.slot,
                input.block_time,
                observed_at,
                input.status,
                input.classification,
                evidence_json,
                raw_amount,
                input.token_mint,
                input.token_decimals
            ],
        )?;
        let stored = transaction.query_row(
            "SELECT source_wallet_address,status,classification,evidence_json,raw_amount,token_mint,token_decimals
             FROM transactions WHERE signature=?",
            [input.signature],
            |row| {
                let raw_evidence: String = row.get(3)?;
                Ok(StoredTransaction {
                    source_wallet_address: row.get(0)?,
                    status: row.get(1)?,
                    classification: row.get(2)?,
                    evidence: serde_json::from_str(&raw_evidence).unwrap_or(Value::Object(Map::new())),
                    raw_amount: row.get(4)?,
                    token_mint: row.get(5)?,
                    token_decimals: row.get(6)?,
                })
            },
        )?;
        if stored.source_wallet_address != input.source_wallet_address {
            return Err(ProfileStoreError::InvalidInput(
                "Signature already stored for a different source wallet.".to_string(),
            ));
        }

        let effective_status = if stored.status == "confirmed"
            || stored.status == "failed"
            || input.status == "unknown"
        {
            stored.status.clone()
        } else {
            input.status.to_string()
        };
        let effective_classification = if input.classification == "UNKNOWN" {
            stored.classification.clone()
        } else {
            input.classification.to_string()
        };
        let mut merged_evidence = stored.evidence.as_object().cloned().ok_or_else(|| {
            ProfileStoreError::InvalidInput(
                "Stored transaction evidence must be a JSON object.".to_string(),
            )
        })?;
        let incoming_evidence = evidence.as_object().ok_or_else(|| {
            ProfileStoreError::InvalidInput("Transaction evidence must be an object.".to_string())
        })?;
        for (key, value) in incoming_evidence {
            merged_evidence.insert(key.clone(), value.clone());
        }
        let merged_evidence_json = serde_json::to_string(&Value::Object(merged_evidence))?;
        let token_changed = input.token_mint.is_some()
            && stored
                .token_mint
                .as_deref()
                .is_some_and(|old| old != input.token_mint.unwrap());
        let effective_amount = if raw_amount.is_some() || token_changed {
            raw_amount
        } else {
            stored.raw_amount
        };
        let effective_mint = input.token_mint.map(str::to_string).or(stored.token_mint);
        let effective_decimals = input.token_decimals.or(stored.token_decimals);
        transaction.execute(
            "UPDATE transactions SET
                slot=COALESCE(?1,slot),block_time=COALESCE(?2,block_time),
                status=?3,classification=?4,evidence_json=?5,raw_amount=?6,
                token_mint=?7,token_decimals=?8 WHERE signature=?9",
            params![
                input.slot,
                input.block_time,
                effective_status,
                effective_classification,
                merged_evidence_json,
                effective_amount,
                effective_mint,
                effective_decimals,
                input.signature
            ],
        )?;

        let mut seen = HashSet::new();
        for profile_id in input.profile_ids {
            if !seen.insert(profile_id) {
                continue;
            }
            let profile_exists: bool = transaction.query_row(
                "SELECT EXISTS(SELECT 1 FROM profiles WHERE profile_id=? AND deleted_at IS NULL)",
                [profile_id],
                |row| row.get(0),
            )?;
            if !profile_exists {
                return Err(ProfileStoreError::InvalidInput(
                    "Profile not found.".to_string(),
                ));
            }
            let associated: bool = transaction.query_row(
                "SELECT EXISTS(SELECT 1 FROM profile_source_wallets WHERE profile_id=? AND wallet_address=?)",
                params![profile_id, input.source_wallet_address],
                |row| row.get(0),
            )?;
            if !associated {
                return Err(ProfileStoreError::InvalidInput(
                    "Associate source wallet with profile before linking transaction.".to_string(),
                ));
            }
            transaction.execute(
                "INSERT OR IGNORE INTO profile_transactions VALUES(?,?,?)",
                params![profile_id, input.signature, utc_now()],
            )?;
        }
        transaction.commit()?;
        Ok(input.signature.to_string())
    }

    pub fn get_transaction(
        &self,
        signature: &str,
        profile_id: Option<&str>,
    ) -> Result<Option<ProfileTransaction>, ProfileStoreError> {
        let connection = self.connect()?;
        let sql = if profile_id.is_some() {
            "SELECT t.signature,t.source_wallet_address,t.slot,t.block_time,t.observed_at,t.status,
                    t.classification,t.evidence_json,t.raw_amount,t.token_mint,t.token_decimals
             FROM transactions t JOIN profile_transactions p ON p.signature=t.signature
             WHERE t.signature=?1 AND p.profile_id=?2"
        } else {
            "SELECT signature,source_wallet_address,slot,block_time,observed_at,status,
                    classification,evidence_json,raw_amount,token_mint,token_decimals
             FROM transactions WHERE signature=?1"
        };
        let row = if let Some(pid) = profile_id {
            connection
                .query_row(sql, params![signature, pid], transaction_from_row)
                .optional()?
        } else {
            connection
                .query_row(sql, [signature], transaction_from_row)
                .optional()?
        };
        Ok(row)
    }

    pub fn list_transactions(
        &self,
        profile_id: &str,
    ) -> Result<Vec<ProfileTransaction>, ProfileStoreError> {
        let connection = self.connect()?;
        let mut statement = connection.prepare(
            "SELECT t.signature,t.source_wallet_address,t.slot,t.block_time,t.observed_at,t.status,
                    t.classification,t.evidence_json,t.raw_amount,t.token_mint,t.token_decimals
             FROM transactions t JOIN profile_transactions p ON p.signature=t.signature
             WHERE p.profile_id=? ORDER BY t.observed_at DESC,t.signature",
        )?;
        let rows = statement
            .query_map([profile_id], transaction_from_row)?
            .collect::<Result<Vec<_>, _>>()?;
        Ok(rows)
    }

    pub fn save_balance_snapshot(
        &self,
        input: SaveBalanceSnapshot<'_>,
    ) -> Result<String, ProfileStoreError> {
        let raw_amount = exact_amount(input.raw_amount)?;
        validate_address(input.wallet_address, "wallet address")?;
        validate_address(input.token_mint, "token mint")?;
        if !(0..=30).contains(&input.token_decimals) {
            return Err(ProfileStoreError::InvalidInput(
                "Invalid token decimals.".to_string(),
            ));
        }
        if input.source.trim().is_empty() {
            return Err(ProfileStoreError::InvalidInput(
                "Balance source/provenance is required.".to_string(),
            ));
        }
        if input.freshness_seconds.is_some_and(|value| value < 0) {
            return Err(ProfileStoreError::InvalidInput(
                "Invalid freshness.".to_string(),
            ));
        }
        let snapshot_id = Uuid::new_v4().to_string();
        let observed_at = validate_timestamp(input.observed_at.unwrap_or(&utc_now()))?;
        let mut connection = self.connect()?;
        let transaction = connection.transaction()?;
        let profile_exists: bool = transaction.query_row(
            "SELECT EXISTS(SELECT 1 FROM profiles WHERE profile_id=? AND deleted_at IS NULL)",
            [input.profile_id],
            |row| row.get(0),
        )?;
        if !profile_exists {
            return Err(ProfileStoreError::InvalidInput(
                "Profile not found.".to_string(),
            ));
        }
        let associated: bool = transaction.query_row(
            "SELECT EXISTS(SELECT 1 FROM profile_source_wallets WHERE profile_id=? AND wallet_address=?)",
            params![input.profile_id, input.wallet_address],
            |row| row.get(0),
        )?;
        if !associated {
            return Err(ProfileStoreError::InvalidInput(
                "Wallet is not associated with profile.".to_string(),
            ));
        }
        if input.is_current {
            transaction.execute(
                "UPDATE balance_snapshots SET is_current=0
                 WHERE profile_id=? AND wallet_address=? AND token_mint=?",
                params![input.profile_id, input.wallet_address, input.token_mint],
            )?;
        }
        transaction.execute(
            "INSERT INTO balance_snapshots VALUES(?,?,?,?,?,?,?,?,?,?)",
            params![
                snapshot_id,
                input.profile_id,
                input.wallet_address,
                input.token_mint,
                raw_amount,
                input.token_decimals,
                observed_at,
                input.source.trim(),
                input.freshness_seconds,
                i64::from(input.is_current)
            ],
        )?;
        transaction.commit()?;
        Ok(snapshot_id)
    }

    pub fn current_balances(
        &self,
        profile_id: &str,
    ) -> Result<Vec<BalanceSnapshot>, ProfileStoreError> {
        let connection = self.connect()?;
        let mut statement = connection.prepare(
            "SELECT snapshot_id,profile_id,wallet_address,token_mint,raw_amount,token_decimals,
                    observed_at,source,freshness_seconds,is_current
             FROM balance_snapshots WHERE profile_id=? AND is_current=1 ORDER BY token_mint",
        )?;
        let rows = statement
            .query_map([profile_id], |row| {
                Ok(BalanceSnapshot {
                    snapshot_id: row.get(0)?,
                    profile_id: row.get(1)?,
                    wallet_address: row.get(2)?,
                    token_mint: row.get(3)?,
                    raw_amount: row.get(4)?,
                    token_decimals: row.get(5)?,
                    observed_at: row.get(6)?,
                    source: row.get(7)?,
                    freshness_seconds: row.get(8)?,
                    is_current: row.get::<_, i64>(9)? == 1,
                })
            })?
            .collect::<Result<Vec<_>, _>>()?;
        Ok(rows)
    }

    pub fn portfolio_holdings(
        &self,
        profile_id: &str,
        max_age_seconds: Option<i64>,
    ) -> Result<Vec<PortfolioHolding>, ProfileStoreError> {
        if max_age_seconds.is_some_and(|age| age < 0) {
            return Err(ProfileStoreError::InvalidInput(
                "max_age_seconds must be a non-negative integer.".to_string(),
            ));
        }
        let rows = self.current_balances(profile_id)?;
        let now = Utc::now();
        let mut totals: BTreeMap<String, HoldingAccumulator> = BTreeMap::new();
        for row in rows {
            let Ok(observed) = DateTime::parse_from_rfc3339(&row.observed_at) else {
                continue;
            };
            let age = now
                .signed_duration_since(observed.with_timezone(&Utc))
                .num_seconds()
                .max(0);
            if max_age_seconds.is_some_and(|limit| age > limit) {
                continue;
            }
            let raw = BigDecimal::from_str(&row.raw_amount)
                .map_err(|_| ProfileStoreError::InvalidAmount)?;
            let decimal_factor = BigDecimal::from_str(&format!("1e-{}", row.token_decimals))
                .map_err(|_| ProfileStoreError::InvalidAmount)?;
            let amount = &raw * decimal_factor;
            let entry = totals.entry(row.token_mint.clone()).or_default();
            if let Some(previous) = entry.token_decimals {
                if previous != row.token_decimals {
                    return Err(ProfileStoreError::InvalidInput(
                        "Conflicting decimals for the same token mint.".to_string(),
                    ));
                }
            } else {
                entry.token_decimals = Some(row.token_decimals);
            }
            entry.raw_amount += raw;
            entry.amount += amount;
            entry.wallet_count += 1;
            entry.oldest_observed_at = Some(
                entry
                    .oldest_observed_at
                    .take()
                    .map(|value| value.min(row.observed_at.clone()))
                    .unwrap_or_else(|| row.observed_at.clone()),
            );
            entry.newest_observed_at = Some(
                entry
                    .newest_observed_at
                    .take()
                    .map(|value| value.max(row.observed_at.clone()))
                    .unwrap_or_else(|| row.observed_at.clone()),
            );
            entry.provenance.insert(row.source);
        }
        totals
            .into_iter()
            .map(|(token_mint, entry)| {
                let token_decimals = entry.token_decimals.unwrap_or(0);
                Ok(PortfolioHolding {
                    token_mint,
                    token_decimals,
                    raw_amount: decimal_to_plain(&entry.raw_amount),
                    amount: decimal_to_plain(&entry.amount),
                    wallet_count: entry.wallet_count,
                    oldest_observed_at: entry.oldest_observed_at.unwrap_or_default(),
                    newest_observed_at: entry.newest_observed_at.unwrap_or_default(),
                    metadata: None,
                    provenance: entry.provenance.into_iter().collect(),
                })
            })
            .collect()
    }

    pub fn backup_to(
        &self,
        destination_path: impl AsRef<Path>,
    ) -> Result<String, ProfileStoreError> {
        let destination = destination_path.as_ref();
        if let Some(parent) = destination.parent() {
            if !parent.as_os_str().is_empty() {
                std::fs::create_dir_all(parent).map_err(|_| ProfileStoreError::Backup)?;
            }
        }
        let source = self.connect()?;
        let mut target = Connection::open(destination)?;
        let backup = Backup::new(&source, &mut target).map_err(|_| ProfileStoreError::Backup)?;
        backup
            .run_to_completion(5, Duration::from_millis(250), None)
            .map_err(|_| ProfileStoreError::Backup)?;
        Ok(destination.to_string_lossy().to_string())
    }

    pub fn delete_profile(&self, profile_id: &str) -> Result<bool, ProfileStoreError> {
        let now = utc_now();
        let connection = self.connect()?;
        let changed = connection.execute(
            "UPDATE profiles SET deleted_at=?,updated_at=?,status='archived'
             WHERE profile_id=? AND deleted_at IS NULL",
            params![now, now, profile_id],
        )?;
        if changed != 1 {
            return Err(ProfileStoreError::InvalidInput(
                "Profile not found or already deleted.".to_string(),
            ));
        }
        Ok(true)
    }
}

fn transaction_from_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<ProfileTransaction> {
    let evidence_json: String = row.get(7)?;
    Ok(ProfileTransaction {
        signature: row.get(0)?,
        source_wallet_address: row.get(1)?,
        slot: row.get(2)?,
        block_time: row.get(3)?,
        observed_at: row.get(4)?,
        status: row.get(5)?,
        classification: row.get(6)?,
        evidence: serde_json::from_str(&evidence_json).unwrap_or(Value::Object(Map::new())),
        raw_amount: row.get(8)?,
        token_mint: row.get(9)?,
        token_decimals: row.get(10)?,
    })
}

fn validate_profile_name(value: &str) -> Result<String, ProfileStoreError> {
    let name = value.trim();
    if name.is_empty() || name.chars().count() > 80 {
        return Err(ProfileStoreError::InvalidInput(
            "Profile name must contain 1–80 characters.".to_string(),
        ));
    }
    Ok(name.to_string())
}

fn validate_address(value: &str, field: &str) -> Result<(), ProfileStoreError> {
    require_solana_address(value)
        .map_err(|_| ProfileStoreError::InvalidInput(format!("Invalid Solana {field}.")))?;
    Ok(())
}

fn serialize_settings(settings: &Value) -> Result<String, ProfileStoreError> {
    fn inspect(value: &Value) -> Result<(), ProfileStoreError> {
        match value {
            Value::Object(map) => {
                for (key, child) in map {
                    let normalized = key.trim().to_ascii_lowercase().replace(['-', ' '], "_");
                    if FORBIDDEN_SETTINGS_KEYS.contains(&normalized.as_str()) {
                        return Err(ProfileStoreError::InvalidInput(
                            "Secret authorization fields cannot be stored in profile settings."
                                .to_string(),
                        ));
                    }
                    inspect(child)?;
                }
            }
            Value::Array(items) => {
                for child in items {
                    inspect(child)?;
                }
            }
            _ => {}
        }
        Ok(())
    }
    if !settings.is_object() {
        return Err(ProfileStoreError::InvalidInput(
            "Settings must be an object.".to_string(),
        ));
    }
    inspect(settings)?;
    Ok(serde_json::to_string(settings)?)
}

fn serialize_object(value: &Value, error: &str) -> Result<String, ProfileStoreError> {
    if !value.is_object() {
        return Err(ProfileStoreError::InvalidInput(error.to_string()));
    }
    Ok(serde_json::to_string(value)?)
}

fn exact_amount(value: &str) -> Result<String, ProfileStoreError> {
    let text = value.trim();
    if text.is_empty() {
        return Err(ProfileStoreError::InvalidAmount);
    }
    let parsed = BigDecimal::from_str(text).map_err(|_| ProfileStoreError::InvalidAmount)?;
    if parsed.with_scale(0) != parsed {
        return Err(ProfileStoreError::InvalidAmount);
    }
    Ok(text.to_string())
}

fn validate_timestamp(value: &str) -> Result<String, ProfileStoreError> {
    if DateTime::parse_from_rfc3339(value.trim()).is_err() {
        return Err(ProfileStoreError::InvalidTimestamp);
    }
    Ok(value.to_string())
}

fn utc_now() -> String {
    Utc::now().to_rfc3339_opts(SecondsFormat::Secs, true)
}

fn decimal_to_plain(value: &BigDecimal) -> String {
    if value.is_zero() {
        "0".to_string()
    } else {
        value.normalized().to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    const WALLET_A: &str = "11111111111111111111111111111111";
    const WALLET_B: &str = "So11111111111111111111111111111111111111112";

    fn store() -> (tempfile::TempDir, ProfileStore) {
        let directory = tempdir().expect("temporary directory");
        let path = directory.path().join("profiles.db");
        let store = ProfileStore::open(&path).expect("profile store");
        (directory, store)
    }

    #[test]
    fn creates_profiles_with_isolated_settings_and_enforces_limit() {
        let (_directory, store) = store();
        let a = store
            .create_profile(" Alpha ", None, Some(serde_json::json!({"risk":"low"})))
            .expect("profile A");
        let b = store
            .create_profile("Beta", None, Some(serde_json::json!({"risk":"medium"})))
            .expect("profile B");
        store
            .set_settings(&a, &serde_json::json!({"risk":"high"}))
            .expect("update settings");
        assert_eq!(
            store.get_profile(&a).unwrap().unwrap().settings["risk"],
            "high"
        );
        assert_eq!(
            store.get_profile(&b).unwrap().unwrap().settings["risk"],
            "medium"
        );
        assert_eq!(store.get_profile(&a).unwrap().unwrap().status, "disabled");
        store.create_profile("Three", None, None).unwrap();
        assert!(store.create_profile("Four", None, None).is_err());
    }

    #[test]
    fn rejects_secret_fields_and_invalid_addresses() {
        let (_directory, store) = store();
        assert!(store
            .create_profile(
                "Unsafe",
                None,
                Some(serde_json::json!({"wallet":{"seed_phrase":"not-stored"}}))
            )
            .is_err());
        assert!(store
            .create_profile("Invalid", Some("not-a-wallet"), None)
            .is_err());
    }

    #[test]
    fn settings_can_be_validated_without_writing_and_reject_secret_fields() {
        assert!(ProfileStore::validate_settings(&serde_json::json!({"risk":"low"})).is_ok());
        assert!(ProfileStore::validate_settings(
            &serde_json::json!({"credentials":{"seed_phrase":"must-not-be-stored"}})
        ).is_err());
    }

    #[test]
    fn shared_wallet_association_and_soft_delete_preserve_history() {
        let (_directory, store) = store();
        let a = store.create_profile("A", None, None).unwrap();
        let b = store.create_profile("B", None, None).unwrap();
        store.associate_source_wallet(&a, WALLET_A).unwrap();
        store.associate_source_wallet(&b, WALLET_A).unwrap();
        store.disassociate_source_wallet(&a, WALLET_A).unwrap();
        assert!(store
            .get_profile(&a)
            .unwrap()
            .unwrap()
            .source_wallets
            .is_empty());
        assert_eq!(
            store.get_profile(&b).unwrap().unwrap().source_wallets,
            vec![WALLET_A]
        );
        store.associate_source_wallet(&a, WALLET_A).unwrap();
        store
            .save_transaction(SaveTransaction {
                signature: "example-signature",
                source_wallet_address: WALLET_A,
                classification: "BUY_CANDIDATE",
                status: "confirmed",
                evidence: Some(serde_json::json!({"source":"unit-test"})),
                slot: Some(12),
                block_time: Some(1_700_000_000),
                raw_amount: Some("000123"),
                token_mint: Some(WALLET_A),
                token_decimals: Some(6),
                profile_ids: std::slice::from_ref(&a),
                observed_at: Some("2026-01-02T03:04:05Z"),
            })
            .unwrap();
        store.delete_profile(&a).unwrap();
        assert!(store.get_profile(&a).unwrap().is_none());
        assert_eq!(
            store
                .get_transaction("example-signature", None)
                .unwrap()
                .unwrap()
                .raw_amount
                .as_deref(),
            Some("000123")
        );
        assert_eq!(store.list_transactions(&a).unwrap().len(), 1);
    }

    #[test]
    fn transaction_upsert_is_idempotent_and_preserves_terminal_status() {
        let (_directory, store) = store();
        let profile = store.create_profile("Profile", None, None).unwrap();
        store.associate_source_wallet(&profile, WALLET_A).unwrap();
        let input = SaveTransaction {
            signature: "stable-signature",
            source_wallet_address: WALLET_A,
            classification: "BUY_CANDIDATE",
            status: "confirmed",
            evidence: Some(serde_json::json!({"first":true})),
            slot: None,
            block_time: None,
            raw_amount: Some("000123"),
            token_mint: Some(WALLET_A),
            token_decimals: Some(6),
            profile_ids: std::slice::from_ref(&profile),
            observed_at: Some("2026-01-02T03:04:05Z"),
        };
        store.save_transaction(input.clone()).unwrap();
        let mut later = input;
        later.status = "pending";
        later.classification = "UNKNOWN";
        later.evidence = Some(serde_json::json!({"second":true}));
        store.save_transaction(later).unwrap();
        let record = store
            .get_transaction("stable-signature", Some(&profile))
            .unwrap()
            .unwrap();
        assert_eq!(record.status, "confirmed");
        assert_eq!(record.classification, "BUY_CANDIDATE");
        assert_eq!(record.raw_amount.as_deref(), Some("000123"));
        assert_eq!(record.evidence["first"], true);
        assert_eq!(record.evidence["second"], true);
        assert_eq!(store.list_transactions(&profile).unwrap().len(), 1);
    }

    #[test]
    fn exact_balance_snapshots_aggregate_and_keep_zero_balances() {
        let (_directory, store) = store();
        let profile = store.create_profile("Portfolio", None, None).unwrap();
        store.associate_source_wallet(&profile, WALLET_A).unwrap();
        store.associate_source_wallet(&profile, WALLET_B).unwrap();
        store
            .save_balance_snapshot(SaveBalanceSnapshot {
                profile_id: &profile,
                wallet_address: WALLET_A,
                token_mint: WALLET_A,
                raw_amount: "1000000",
                token_decimals: 6,
                source: "rpc-a",
                observed_at: Some("2026-10-25T00:00:00Z"),
                freshness_seconds: None,
                is_current: true,
            })
            .unwrap();
        store
            .save_balance_snapshot(SaveBalanceSnapshot {
                profile_id: &profile,
                wallet_address: WALLET_B,
                token_mint: WALLET_A,
                raw_amount: "250000",
                token_decimals: 6,
                source: "rpc-b",
                observed_at: Some("2026-10-25T00:01:00Z"),
                freshness_seconds: Some(5),
                is_current: true,
            })
            .unwrap();
        let holdings = store.portfolio_holdings(&profile, None).unwrap();
        assert_eq!(holdings[0].raw_amount, "1250000");
        assert_eq!(holdings[0].amount, "1.25");
        assert_eq!(holdings[0].wallet_count, 2);
        assert_eq!(holdings[0].provenance, vec!["rpc-a", "rpc-b"]);
    }

    #[test]
    fn latest_current_snapshot_replaces_only_matching_wallet_and_mint() {
        let (_directory, store) = store();
        let profile = store.create_profile("Balances", None, None).unwrap();
        store.associate_source_wallet(&profile, WALLET_A).unwrap();
        for amount in ["10", "0"] {
            store
                .save_balance_snapshot(SaveBalanceSnapshot {
                    profile_id: &profile,
                    wallet_address: WALLET_A,
                    token_mint: WALLET_A,
                    raw_amount: amount,
                    token_decimals: 0,
                    source: "rpc",
                    observed_at: Some(if amount == "10" {
                        "2026-01-01T00:00:00Z"
                    } else {
                        "2026-01-02T00:00:00Z"
                    }),
                    freshness_seconds: None,
                    is_current: true,
                })
                .unwrap();
        }
        let rows = store.current_balances(&profile).unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].raw_amount, "0");
    }

    #[test]
    fn backup_restores_profile_and_balance_data() {
        let (directory, store) = store();
        let profile = store.create_profile("Backup", None, None).unwrap();
        store.associate_source_wallet(&profile, WALLET_A).unwrap();
        store
            .save_balance_snapshot(SaveBalanceSnapshot {
                profile_id: &profile,
                wallet_address: WALLET_A,
                token_mint: WALLET_A,
                raw_amount: "0",
                token_decimals: 6,
                source: "rpc",
                observed_at: Some("2026-01-02T03:04:05Z"),
                freshness_seconds: None,
                is_current: true,
            })
            .unwrap();
        let backup = directory.path().join("backup.db");
        store.backup_to(&backup).unwrap();
        let restored = ProfileStore::open(backup).unwrap();
        assert_eq!(
            restored.get_profile(&profile).unwrap().unwrap().name,
            "Backup"
        );
        assert_eq!(
            restored.current_balances(&profile).unwrap()[0].raw_amount,
            "0"
        );
    }
}
