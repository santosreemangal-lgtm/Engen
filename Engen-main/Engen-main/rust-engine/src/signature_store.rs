//! Durable SQLite store for public wallet signatures and bounded processing retries.
//!
//! The schema intentionally matches the existing Python SignatureStore so it can
//! be migrated and cross-checked without rewriting user databases.

use std::path::{Path, PathBuf};

use rusqlite::{params, Connection, OptionalExtension, Transaction};
use serde_json::Value;
use thiserror::Error;

use crate::solana_validation::{require_solana_address, require_transaction_signature};

#[derive(Debug, Error)]
pub enum SignatureStoreError {
    #[error("Database operation failed.")]
    Database(#[from] rusqlite::Error),
    #[error("Invalid input: {0}")]
    InvalidInput(String),
    #[error("Stored JSON data is invalid.")]
    InvalidJson(#[from] serde_json::Error),
    #[error("The requested record does not exist.")]
    NotFound,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FailureState {
    pub status: String,
    pub attempts: u32,
}

#[derive(Debug, Clone, PartialEq)]
pub struct TransactionRecord {
    pub raw_transaction: Option<Value>,
    pub parsed_evidence: Option<Value>,
    pub classification: Option<Value>,
    pub status: String,
    pub updated_at: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ErrorRecord {
    pub status: String,
    pub attempts: u32,
    pub last_checked_at: Option<String>,
    pub error_stage: Option<String>,
    pub error_code: Option<String>,
    pub error_message: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WalletPosition {
    pub slot: Option<u64>,
    pub signature: Option<String>,
    pub updated_at: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProcessingStage {
    Discovery,
    RpcFetch,
    Interpretation,
    PaperSimulation,
    Persistence,
}

impl ProcessingStage {
    fn as_str(self) -> &'static str {
        match self {
            Self::Discovery => "discovery",
            Self::RpcFetch => "rpc_fetch",
            Self::Interpretation => "interpretation",
            Self::PaperSimulation => "paper_simulation",
            Self::Persistence => "persistence",
        }
    }
}

pub struct SignatureStore {
    path: PathBuf,
}

impl SignatureStore {
    pub fn open(path: impl AsRef<Path>) -> Result<Self, SignatureStoreError> {
        let path = path.as_ref().to_path_buf();
        if let Some(parent) = path.parent() {
            if !parent.as_os_str().is_empty() {
                std::fs::create_dir_all(parent).map_err(|_| {
                    SignatureStoreError::InvalidInput(
                        "Unable to create database directory.".to_string(),
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

    fn connect(&self) -> Result<Connection, SignatureStoreError> {
        let connection = Connection::open(&self.path)?;
        connection.busy_timeout(std::time::Duration::from_secs(10))?;
        connection.execute_batch("PRAGMA foreign_keys=ON;")?;
        Ok(connection)
    }

    fn initialize(connection: &mut Connection) -> Result<(), SignatureStoreError> {
        let transaction = connection.transaction()?;
        transaction.execute_batch(
            "
            CREATE TABLE IF NOT EXISTS seen_signatures (
                wallet_address TEXT NOT NULL,
                signature TEXT NOT NULL,
                first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (wallet_address, signature)
            );
            CREATE TABLE IF NOT EXISTS wallet_baselines (
                wallet_address TEXT PRIMARY KEY,
                initialized_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS wallet_history_backfills (
                wallet_address TEXT PRIMARY KEY,
                cursor_signature TEXT,
                signatures_count INTEGER NOT NULL DEFAULT 0,
                completed INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS wallet_positions (
                wallet_address TEXT PRIMARY KEY,
                slot INTEGER,
                signature TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS transaction_records (
                wallet_address TEXT NOT NULL,
                signature TEXT NOT NULL,
                raw_transaction_json TEXT,
                parsed_evidence_json TEXT,
                classification_json TEXT,
                processing_status TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (wallet_address, signature)
            );
            ",
        )?;

        let mut transaction_record_statement =
            transaction.prepare("PRAGMA table_info(transaction_records)")?;
        let transaction_record_columns = transaction_record_statement
            .query_map([], |row| row.get::<_, String>(1))?
            .collect::<Result<std::collections::HashSet<_>, _>>()?;
        drop(transaction_record_statement);

        for (column, definition) in [
            ("parsed_evidence_json", "TEXT"),
            ("classification_json", "TEXT"),
        ] {
            if !transaction_record_columns.contains(column) {
                transaction.execute_batch(&format!(
                    "ALTER TABLE transaction_records ADD COLUMN {column} {definition};"
                ))?;
            }
        }

        let mut statement = transaction.prepare("PRAGMA table_info(seen_signatures)")?;
        let columns = statement
            .query_map([], |row| row.get::<_, String>(1))?
            .collect::<Result<std::collections::HashSet<_>, _>>()?;
        drop(statement);

        let migrations = [
            ("status", "TEXT NOT NULL DEFAULT 'processed'"),
            ("attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("slot", "INTEGER"),
            ("last_checked_at", "TEXT"),
            ("error_stage", "TEXT"),
            ("error_code", "TEXT"),
            ("error_message", "TEXT"),
            ("claim_owner", "TEXT"),
            ("claim_expires_at", "INTEGER"),
            ("queue_lane", "TEXT NOT NULL DEFAULT 'history'"),
            ("signature_observation_json", "TEXT"),
        ];
        for (column, definition) in migrations {
            if !columns.contains(column) {
                transaction.execute_batch(&format!(
                    "ALTER TABLE seen_signatures ADD COLUMN {column} {definition};"
                ))?;
            }
        }

        transaction.commit()?;
        Ok(())
    }

    fn validate_wallet(wallet: &str) -> Result<(), SignatureStoreError> {
        require_solana_address(wallet)
            .map(|_| ())
            .map_err(|error| SignatureStoreError::InvalidInput(error.to_string()))
    }

    fn validate_signature(signature: &str) -> Result<(), SignatureStoreError> {
        require_transaction_signature(signature)
            .map(|_| ())
            .map_err(|error| SignatureStoreError::InvalidInput(error.to_string()))
    }

    fn transaction<'a>(
        connection: &'a mut Connection,
    ) -> Result<Transaction<'a>, SignatureStoreError> {
        Ok(connection.transaction()?)
    }

    pub fn has_baseline(&self, wallet: &str) -> Result<bool, SignatureStoreError> {
        Self::validate_wallet(wallet)?;
        let connection = self.connect()?;
        let found = connection
            .query_row(
                "SELECT 1 FROM wallet_baselines WHERE wallet_address=?1 LIMIT 1",
                [wallet],
                |_| Ok(()),
            )
            .optional()?
            .is_some();
        Ok(found)
    }

    pub fn mark_baseline(&self, wallet: &str) -> Result<bool, SignatureStoreError> {
        Self::validate_wallet(wallet)?;
        let mut connection = self.connect()?;
        let transaction = Self::transaction(&mut connection)?;
        let changed = transaction.execute(
            "INSERT OR IGNORE INTO wallet_baselines(wallet_address) VALUES(?1)",
            [wallet],
        )?;
        transaction.commit()?;
        Ok(changed == 1)
    }

    pub fn has_seen(&self, wallet: &str, signature: &str) -> Result<bool, SignatureStoreError> {
        Self::validate_wallet(wallet)?;
        Self::validate_signature(signature)?;
        let connection = self.connect()?;
        let found = connection
            .query_row(
                "SELECT 1 FROM seen_signatures WHERE wallet_address=?1 AND signature=?2 LIMIT 1",
                params![wallet, signature],
                |_| Ok(()),
            )
            .optional()?
            .is_some();
        Ok(found)
    }

    pub fn get_status(
        &self,
        wallet: &str,
        signature: &str,
    ) -> Result<Option<String>, SignatureStoreError> {
        Self::validate_wallet(wallet)?;
        Self::validate_signature(signature)?;
        let connection = self.connect()?;
        Ok(connection.query_row(
            "SELECT status FROM seen_signatures WHERE wallet_address=?1 AND signature=?2 LIMIT 1",
            params![wallet, signature],
            |row| row.get(0),
        ).optional()?)
    }

    pub fn save(
        &self,
        wallet: &str,
        signature: &str,
        slot: Option<u64>,
    ) -> Result<bool, SignatureStoreError> {
        self.save_in_lane(wallet, signature, slot, "fresh", None)
    }

    /// Save a first-seen signature together with its source queue and RPC observation.
    /// Existing lane assignments are never downgraded from Fresh to History.
    pub fn save_in_lane(
        &self,
        wallet: &str,
        signature: &str,
        slot: Option<u64>,
        lane: &str,
        observation: Option<&Value>,
    ) -> Result<bool, SignatureStoreError> {
        Self::validate_wallet(wallet)?;
        Self::validate_signature(signature)?;
        if !matches!(lane, "fresh" | "history") {
            return Err(SignatureStoreError::InvalidInput(
                "Signature queue lane must be fresh or history.".to_string(),
            ));
        }
        let slot = match slot {
            Some(value) if value > i64::MAX as u64 => {
                return Err(SignatureStoreError::InvalidInput(
                    "Slot is out of range.".to_string(),
                ));
            }
            Some(value) => Some(value as i64),
            None => None,
        };
        let observation_json = observation.map(serde_json::to_string).transpose()?;
        let mut connection = self.connect()?;
        let transaction = Self::transaction(&mut connection)?;
        let inserted = transaction.execute(
            "INSERT OR IGNORE INTO seen_signatures(
                wallet_address,signature,status,slot,queue_lane,signature_observation_json
             ) VALUES(?1,?2,'pending',?3,?4,?5)",
            params![wallet, signature, slot, lane, observation_json],
        )?;
        if inserted == 0 {
            transaction.execute(
                "UPDATE seen_signatures
                 SET signature_observation_json=COALESCE(?3,signature_observation_json),
                     slot=COALESCE(?4,slot)
                 WHERE wallet_address=?1 AND signature=?2",
                params![wallet, signature, observation_json, slot],
            )?;
        }
        transaction.commit()?;
        Ok(inserted == 1)
    }

    pub fn save_many<I, S>(&self, wallet: &str, signatures: I) -> Result<usize, SignatureStoreError>
    where
        I: IntoIterator<Item = S>,
        S: AsRef<str>,
    {
        Self::validate_wallet(wallet)?;
        let mut connection = self.connect()?;
        let transaction = Self::transaction(&mut connection)?;
        let mut inserted = 0;
        for signature in signatures {
            let signature = signature.as_ref();
            Self::validate_signature(signature)?;
            inserted += transaction.execute(
                "INSERT OR IGNORE INTO seen_signatures(wallet_address,signature,status,queue_lane) VALUES(?1,?2,'pending','fresh')",
                params![wallet, signature],
            )?;
        }
        transaction.commit()?;
        Ok(inserted)
    }

    /// Retain a stale source signature as an auditable, non-retryable paper-copy skip.
    pub fn mark_stale(&self, wallet: &str, signature: &str) -> Result<bool, SignatureStoreError> {
        Self::validate_wallet(wallet)?;
        Self::validate_signature(signature)?;
        let mut connection = self.connect()?;
        let transaction = Self::transaction(&mut connection)?;
        transaction.execute(
            "INSERT OR IGNORE INTO seen_signatures(wallet_address,signature,status) VALUES(?1,?2,'stale')",
            params![wallet, signature],
        )?;
        let changed = transaction.execute(
            "UPDATE seen_signatures
             SET status='stale',claim_owner=NULL,claim_expires_at=NULL,
                 last_checked_at=CURRENT_TIMESTAMP,
                 error_stage='freshness',error_code='source_stale',
                 error_message='Source transaction is outside the 60-second paper-copy freshness window.'
             WHERE wallet_address=?1 AND signature=?2",
            params![wallet, signature],
        )?;
        transaction.execute(
            "INSERT INTO transaction_records(wallet_address,signature,raw_transaction_json,processing_status,updated_at)
             VALUES(?1,?2,NULL,'stale',CURRENT_TIMESTAMP)
             ON CONFLICT(wallet_address,signature) DO UPDATE SET
                 processing_status='stale',updated_at=CURRENT_TIMESTAMP",
            params![wallet, signature],
        )?;
        transaction.commit()?;
        Ok(changed == 1)
    }

    /// Atomically claim one pending signature. Expired leases are reclaimable after a crash.
    /// The injected timestamp makes lease expiry deterministic in tests.
    pub fn claim_signature(
        &self,
        wallet: &str,
        signature: &str,
        owner: &str,
        now_epoch: i64,
        lease_seconds: u64,
    ) -> Result<bool, SignatureStoreError> {
        Self::validate_wallet(wallet)?;
        Self::validate_signature(signature)?;
        if owner.trim().is_empty() || owner.len() > 128 || now_epoch < 0 {
            return Err(SignatureStoreError::InvalidInput(
                "Claim owner and timestamp are invalid.".to_string(),
            ));
        }
        let lease_seconds = i64::try_from(lease_seconds).map_err(|_| {
            SignatureStoreError::InvalidInput("Claim lease is out of range.".to_string())
        })?;
        if !(1..=3_600).contains(&lease_seconds) {
            return Err(SignatureStoreError::InvalidInput(
                "Claim lease must be between 1 and 3600 seconds.".to_string(),
            ));
        }
        let expires_at = now_epoch.checked_add(lease_seconds).ok_or_else(|| {
            SignatureStoreError::InvalidInput("Claim expiry is out of range.".to_string())
        })?;
        let mut connection = self.connect()?;
        let transaction = Self::transaction(&mut connection)?;
        let claimed = transaction.execute(
            "UPDATE seen_signatures
             SET status='processing',claim_owner=?3,claim_expires_at=?4,
                 last_checked_at=CURRENT_TIMESTAMP
             WHERE wallet_address=?1 AND signature=?2
               AND (status='pending'
                    OR (status='processing'
                        AND (claim_expires_at IS NULL OR claim_expires_at<=?5)))",
            params![wallet, signature, owner, expires_at, now_epoch],
        )?;
        transaction.commit()?;
        Ok(claimed == 1)
    }

    /// Release only the lease owned by this worker; failure states remain untouched.
    pub fn release_signature_claim(
        &self,
        wallet: &str,
        signature: &str,
        owner: &str,
    ) -> Result<bool, SignatureStoreError> {
        Self::validate_wallet(wallet)?;
        Self::validate_signature(signature)?;
        let mut connection = self.connect()?;
        let transaction = Self::transaction(&mut connection)?;
        let released = transaction.execute(
            "UPDATE seen_signatures
             SET status='pending',claim_owner=NULL,claim_expires_at=NULL,
                 last_checked_at=CURRENT_TIMESTAMP
             WHERE wallet_address=?1 AND signature=?2
               AND status='processing' AND claim_owner=?3",
            params![wallet, signature, owner],
        )?;
        transaction.commit()?;
        Ok(released == 1)
    }

    /// Complete a signature only while the caller still owns its atomic lease.
    pub fn mark_claimed_processed(
        &self,
        wallet: &str,
        signature: &str,
        owner: &str,
    ) -> Result<bool, SignatureStoreError> {
        Self::validate_wallet(wallet)?;
        Self::validate_signature(signature)?;
        let mut connection = self.connect()?;
        let transaction = Self::transaction(&mut connection)?;
        let changed = transaction.execute(
            "UPDATE seen_signatures
             SET status='processed',claim_owner=NULL,claim_expires_at=NULL,
                 last_checked_at=CURRENT_TIMESTAMP,error_stage=NULL,
                 error_code=NULL,error_message=NULL
             WHERE wallet_address=?1 AND signature=?2
               AND status='processing' AND claim_owner=?3",
            params![wallet, signature, owner],
        )?;
        if changed == 1 {
            transaction.execute(
                "UPDATE transaction_records
                 SET processing_status='processed',updated_at=CURRENT_TIMESTAMP
                 WHERE wallet_address=?1 AND signature=?2
                   AND processing_status NOT IN ('stale','failed','incomplete','needs_review')",
                params![wallet, signature],
            )?;
            let slot: Option<i64> = transaction.query_row(
                "SELECT slot FROM seen_signatures WHERE wallet_address=?1 AND signature=?2",
                params![wallet, signature],
                |row| row.get(0),
            )?;
            if let Some(slot) = slot {
                transaction.execute(
                    "INSERT INTO wallet_positions(wallet_address,slot,signature)
                     VALUES(?1,?2,?3)
                     ON CONFLICT(wallet_address) DO UPDATE SET
                       slot=CASE WHEN wallet_positions.slot IS NULL OR excluded.slot>=wallet_positions.slot THEN excluded.slot ELSE wallet_positions.slot END,
                       signature=CASE WHEN wallet_positions.slot IS NULL OR excluded.slot>=wallet_positions.slot THEN excluded.signature ELSE wallet_positions.signature END,
                       updated_at=CURRENT_TIMESTAMP",
                    params![wallet, slot, signature],
                )?;
            }
        }
        transaction.commit()?;
        Ok(changed == 1)
    }

    pub fn mark_processed(
        &self,
        wallet: &str,
        signature: &str,
    ) -> Result<bool, SignatureStoreError> {
        Self::validate_wallet(wallet)?;
        Self::validate_signature(signature)?;
        let mut connection = self.connect()?;
        let transaction = Self::transaction(&mut connection)?;
        let changed = transaction.execute(
            "UPDATE seen_signatures SET status='processed', claim_owner=NULL, claim_expires_at=NULL, last_checked_at=CURRENT_TIMESTAMP, error_stage=NULL, error_code=NULL, error_message=NULL WHERE wallet_address=?1 AND signature=?2 AND status!='stale'",
            params![wallet, signature],
        )?;
        transaction.execute(
            "UPDATE transaction_records SET processing_status='processed', updated_at=CURRENT_TIMESTAMP WHERE wallet_address=?1 AND signature=?2 AND processing_status NOT IN ('stale','failed','incomplete','needs_review')",
            params![wallet, signature],
        )?;
        let slot: Option<i64> = transaction
            .query_row(
                "SELECT slot FROM seen_signatures WHERE wallet_address=?1 AND signature=?2",
                params![wallet, signature],
                |row| row.get(0),
            )
            .optional()?
            .flatten();
        if let Some(slot) = slot {
            transaction.execute(
                "INSERT INTO wallet_positions(wallet_address,slot,signature) VALUES(?1,?2,?3)
                 ON CONFLICT(wallet_address) DO UPDATE SET
                   slot=CASE WHEN wallet_positions.slot IS NULL OR excluded.slot>=wallet_positions.slot THEN excluded.slot ELSE wallet_positions.slot END,
                   signature=CASE WHEN wallet_positions.slot IS NULL OR excluded.slot>=wallet_positions.slot THEN excluded.signature ELSE wallet_positions.signature END,
                   updated_at=CURRENT_TIMESTAMP",
                params![wallet, slot, signature],
            )?;
        }
        transaction.commit()?;
        Ok(changed == 1)
    }

    pub fn record_failure(
        &self,
        wallet: &str,
        signature: &str,
        stage: ProcessingStage,
        code: Option<&str>,
        message: Option<&str>,
        max_attempts: u32,
    ) -> Result<FailureState, SignatureStoreError> {
        Self::validate_wallet(wallet)?;
        Self::validate_signature(signature)?;
        if max_attempts == 0 {
            return Err(SignatureStoreError::InvalidInput(
                "max_attempts must be a positive bounded integer.".to_string(),
            ));
        }

        let safe_code = code
            .filter(|value| !value.is_empty())
            .unwrap_or("processing_error");
        let safe_message = message.unwrap_or("Processing failed.");
        let safe_message = safe_message.chars().take(300).collect::<String>();

        let mut connection = self.connect()?;
        let transaction = Self::transaction(&mut connection)?;
        transaction.execute(
            "INSERT OR IGNORE INTO seen_signatures(wallet_address,signature,status,attempts) VALUES(?1,?2,'pending',0)",
            params![wallet, signature],
        )?;
        let attempts: u32 = transaction.query_row(
            "SELECT attempts FROM seen_signatures WHERE wallet_address=?1 AND signature=?2",
            params![wallet, signature],
            |row| row.get(0),
        )?;
        let attempts = attempts.saturating_add(1);
        let status = if attempts >= max_attempts {
            "needs_review"
        } else {
            "pending"
        };
        transaction.execute(
            "UPDATE seen_signatures SET status=?1,attempts=?2,last_checked_at=CURRENT_TIMESTAMP,error_stage=?3,error_code=?4,error_message=?5,claim_owner=NULL,claim_expires_at=NULL WHERE wallet_address=?6 AND signature=?7",
            params![status, attempts, stage.as_str(), safe_code, safe_message, wallet, signature],
        )?;
        transaction.commit()?;

        Ok(FailureState {
            status: status.to_string(),
            attempts,
        })
    }

    pub fn save_transaction_record(
        &self,
        wallet: &str,
        signature: &str,
        raw_transaction: Option<&Value>,
        processing_status: &str,
    ) -> Result<(), SignatureStoreError> {
        Self::validate_wallet(wallet)?;
        Self::validate_signature(signature)?;
        const ALLOWED: &[&str] = &[
            "observed",
            "incomplete",
            "failed",
            "processed",
            "needs_review",
        ];
        if !ALLOWED.contains(&processing_status) {
            return Err(SignatureStoreError::InvalidInput(
                "Invalid transaction processing status.".to_string(),
            ));
        }
        self.save_transaction_evidence(
            wallet,
            signature,
            raw_transaction,
            None,
            None,
            processing_status,
        )
    }

    /// Persist raw payload and normalized parsed/classification evidence atomically.
    pub fn save_transaction_evidence(
        &self,
        wallet: &str,
        signature: &str,
        raw_transaction: Option<&Value>,
        parsed_evidence: Option<&Value>,
        classification: Option<&Value>,
        processing_status: &str,
    ) -> Result<(), SignatureStoreError> {
        Self::validate_wallet(wallet)?;
        Self::validate_signature(signature)?;
        const ALLOWED: &[&str] = &[
            "observed",
            "incomplete",
            "failed",
            "processed",
            "needs_review",
            "stale",
        ];
        if !ALLOWED.contains(&processing_status) {
            return Err(SignatureStoreError::InvalidInput(
                "Invalid transaction processing status.".to_string(),
            ));
        }
        let raw_json = raw_transaction.map(serde_json::to_string).transpose()?;
        let parsed_json = parsed_evidence.map(serde_json::to_string).transpose()?;
        let classification_json = classification.map(serde_json::to_string).transpose()?;

        let connection = self.connect()?;
        connection.execute(
            "INSERT INTO transaction_records(
                wallet_address,signature,raw_transaction_json,parsed_evidence_json,
                classification_json,processing_status
             ) VALUES(?1,?2,?3,?4,?5,?6)
             ON CONFLICT(wallet_address,signature) DO UPDATE SET
               raw_transaction_json=COALESCE(excluded.raw_transaction_json,transaction_records.raw_transaction_json),
               parsed_evidence_json=COALESCE(excluded.parsed_evidence_json,transaction_records.parsed_evidence_json),
               classification_json=COALESCE(excluded.classification_json,transaction_records.classification_json),
               processing_status=CASE
                 WHEN transaction_records.processing_status IN ('stale','failed','incomplete','needs_review')
                 THEN transaction_records.processing_status
                 ELSE excluded.processing_status
               END,
               updated_at=CURRENT_TIMESTAMP",
            params![
                wallet,
                signature,
                raw_json,
                parsed_json,
                classification_json,
                processing_status
            ],
        )?;
        Ok(())
    }

    pub fn get_transaction_record(
        &self,
        wallet: &str,
        signature: &str,
    ) -> Result<Option<TransactionRecord>, SignatureStoreError> {
        Self::validate_wallet(wallet)?;
        Self::validate_signature(signature)?;
        let connection = self.connect()?;
        let row = connection.query_row(
            "SELECT raw_transaction_json,parsed_evidence_json,classification_json,processing_status,updated_at FROM transaction_records WHERE wallet_address=?1 AND signature=?2",
            params![wallet, signature],
            |row| {
                Ok((
                    row.get::<_, Option<String>>(0)?,
                    row.get::<_, Option<String>>(1)?,
                    row.get::<_, Option<String>>(2)?,
                    row.get::<_, String>(3)?,
                    row.get::<_, String>(4)?,
                ))
            },
        ).optional()?;
        row.map(|(raw, parsed, classification, status, updated)| {
            Ok(TransactionRecord {
                raw_transaction: raw.map(|value| serde_json::from_str(&value)).transpose()?,
                parsed_evidence: parsed
                    .map(|value| serde_json::from_str(&value))
                    .transpose()?,
                classification: classification
                    .map(|value| serde_json::from_str(&value))
                    .transpose()?,
                status,
                updated_at: updated,
            })
        })
        .transpose()
    }

    pub fn get_error_record(
        &self,
        wallet: &str,
        signature: &str,
    ) -> Result<Option<ErrorRecord>, SignatureStoreError> {
        Self::validate_wallet(wallet)?;
        Self::validate_signature(signature)?;
        let connection = self.connect()?;
        Ok(connection.query_row(
            "SELECT status,attempts,last_checked_at,error_stage,error_code,error_message FROM seen_signatures WHERE wallet_address=?1 AND signature=?2",
            params![wallet, signature],
            |row| Ok(ErrorRecord {
                status: row.get(0)?,
                attempts: row.get(1)?,
                last_checked_at: row.get(2)?,
                error_stage: row.get(3)?,
                error_code: row.get(4)?,
                error_message: row.get(5)?,
            }),
        ).optional()?)
    }

    pub fn pending_count_for_lane(
        &self,
        wallet: &str,
        lane: &str,
    ) -> Result<usize, SignatureStoreError> {
        Self::validate_wallet(wallet)?;
        if !matches!(lane, "fresh" | "history") {
            return Err(SignatureStoreError::InvalidInput(
                "Signature queue lane must be fresh or history.".to_string(),
            ));
        }
        let connection = self.connect()?;
        let count: i64 = connection.query_row(
            "SELECT COUNT(*) FROM seen_signatures
             WHERE wallet_address=?1 AND queue_lane=?2
               AND status IN ('pending','processing')",
            params![wallet, lane],
            |row| row.get(0),
        )?;
        Ok(usize::try_from(count).unwrap_or(0))
    }

    pub fn pending_signatures_for_lane(
        &self,
        wallet: &str,
        lane: &str,
        limit: usize,
    ) -> Result<Vec<String>, SignatureStoreError> {
        Self::validate_wallet(wallet)?;
        if !matches!(lane, "fresh" | "history") || limit == 0 || limit > 10_000 {
            return Err(SignatureStoreError::InvalidInput(
                "Invalid queue lane or pending-signature limit.".to_string(),
            ));
        }
        let connection = self.connect()?;
        let mut statement = connection.prepare(
            "SELECT signature FROM seen_signatures
             WHERE wallet_address=?1 AND queue_lane=?2
               AND (status='pending'
                    OR (status='processing' AND
                        (claim_expires_at IS NULL OR
                         claim_expires_at<=CAST(strftime('%s','now') AS INTEGER))))
             ORDER BY first_seen_at ASC LIMIT ?3",
        )?;
        let rows = statement.query_map(params![wallet, lane, limit as i64], |row| row.get(0))?;
        Ok(rows.collect::<Result<Vec<String>, _>>()?)
    }

    /// Restore signature-list metadata after a process restart so timestamp/finality
    /// decisions are identical to the original discovery pass.
    pub fn pending_signature_observations(
        &self,
        wallet: &str,
        lane: &str,
        limit: usize,
    ) -> Result<Vec<Value>, SignatureStoreError> {
        Self::validate_wallet(wallet)?;
        if !matches!(lane, "fresh" | "history") || limit == 0 || limit > 10_000 {
            return Err(SignatureStoreError::InvalidInput(
                "Invalid queue lane or pending-signature limit.".to_string(),
            ));
        }
        let connection = self.connect()?;
        let mut statement = connection.prepare(
            "SELECT signature,slot,signature_observation_json
             FROM seen_signatures
             WHERE wallet_address=?1 AND queue_lane=?2
               AND (status='pending'
                    OR (status='processing' AND
                        (claim_expires_at IS NULL OR
                         claim_expires_at<=CAST(strftime('%s','now') AS INTEGER))))
             ORDER BY first_seen_at ASC LIMIT ?3",
        )?;
        let rows = statement.query_map(params![wallet, lane, limit as i64], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, Option<i64>>(1)?,
                row.get::<_, Option<String>>(2)?,
            ))
        })?;
        let mut observations = Vec::new();
        for row in rows {
            let (signature, slot, encoded) = row?;
            let mut observation = match encoded {
                Some(encoded) => serde_json::from_str::<Value>(&encoded)?,
                None => serde_json::json!({}),
            };
            if !observation.is_object() {
                observation = serde_json::json!({});
            }
            observation["signature"] = Value::String(signature);
            if observation.get("slot").is_none_or(Value::is_null) {
                if let Some(slot) = slot {
                    observation["slot"] = Value::from(slot);
                }
            }
            observations.push(observation);
        }
        Ok(observations)
    }

    pub fn pending_signatures(
        &self,
        wallet: &str,
        limit: usize,
    ) -> Result<Vec<String>, SignatureStoreError> {
        Self::validate_wallet(wallet)?;
        if limit == 0 || limit > 10_000 {
            return Err(SignatureStoreError::InvalidInput(
                "Pending-signature limit must be between 1 and 10000.".to_string(),
            ));
        }
        let connection = self.connect()?;
        let mut statement = connection.prepare(
            "SELECT signature FROM seen_signatures
             WHERE wallet_address=?1
               AND (status='pending'
                    OR (status='processing' AND
                        (claim_expires_at IS NULL OR claim_expires_at<=CAST(strftime('%s','now') AS INTEGER))))
             ORDER BY first_seen_at ASC LIMIT ?2"
        )?;
        let rows = statement.query_map(params![wallet, limit as i64], |row| row.get(0))?;
        Ok(rows.collect::<Result<Vec<String>, _>>()?)
    }

    pub fn history_backfill_state(
        &self,
        wallet: &str,
    ) -> Result<Option<(Option<String>, usize, bool)>, SignatureStoreError> {
        Self::validate_wallet(wallet)?;
        let connection = self.connect()?;
        let row = connection
            .query_row(
                "SELECT cursor_signature,signatures_count,completed
                 FROM wallet_history_backfills WHERE wallet_address=?1",
                [wallet],
                |row| {
                    Ok((
                        row.get::<_, Option<String>>(0)?,
                        row.get::<_, i64>(1)?,
                        row.get::<_, i64>(2)?,
                    ))
                },
            )
            .optional()?;
        Ok(row.map(|(cursor, count, completed)| {
            (cursor, usize::try_from(count).unwrap_or(0), completed != 0)
        }))
    }

    /// Initialize the one-time bounded History backfill. Existing state is never reset.
    pub fn start_history_backfill(
        &self,
        wallet: &str,
        cursor_signature: Option<&str>,
    ) -> Result<bool, SignatureStoreError> {
        Self::validate_wallet(wallet)?;
        if let Some(signature) = cursor_signature {
            Self::validate_signature(signature)?;
        }
        let connection = self.connect()?;
        let changed = connection.execute(
            "INSERT OR IGNORE INTO wallet_history_backfills(
                wallet_address,cursor_signature,signatures_count,completed
             ) VALUES(?1,?2,0,?3)",
            params![
                wallet,
                cursor_signature,
                if cursor_signature.is_none() {
                    1_i64
                } else {
                    0_i64
                }
            ],
        )?;
        Ok(changed == 1)
    }

    /// Persist progress after each page so a provider outage or process restart resumes
    /// from the next signature instead of re-reading an unbounded historical range.
    pub fn advance_history_backfill(
        &self,
        wallet: &str,
        cursor_signature: Option<&str>,
        scanned_count: usize,
        completed: bool,
    ) -> Result<(), SignatureStoreError> {
        Self::validate_wallet(wallet)?;
        if let Some(signature) = cursor_signature {
            Self::validate_signature(signature)?;
        }
        let scanned_count = i64::try_from(scanned_count).map_err(|_| {
            SignatureStoreError::InvalidInput("History scan count is out of range.".to_string())
        })?;
        if !(0..=1_000).contains(&scanned_count) {
            return Err(SignatureStoreError::InvalidInput(
                "History scan count must be between 0 and 1000.".to_string(),
            ));
        }
        let connection = self.connect()?;
        let changed = connection.execute(
            "UPDATE wallet_history_backfills
             SET cursor_signature=?2,
                 signatures_count=MIN(1000,signatures_count+?3),
                 completed=CASE
                   WHEN ?4=1 OR signatures_count+?3>=1000 THEN 1
                   ELSE 0
                 END,
                 updated_at=CURRENT_TIMESTAMP
             WHERE wallet_address=?1",
            params![
                wallet,
                cursor_signature,
                scanned_count,
                if completed { 1_i64 } else { 0_i64 }
            ],
        )?;
        if changed != 1 {
            return Err(SignatureStoreError::NotFound);
        }
        Ok(())
    }

    pub fn get_position(
        &self,
        wallet: &str,
    ) -> Result<Option<WalletPosition>, SignatureStoreError> {
        Self::validate_wallet(wallet)?;
        let connection = self.connect()?;
        Ok(connection
            .query_row(
                "SELECT slot,signature,updated_at FROM wallet_positions WHERE wallet_address=?1",
                [wallet],
                |row| {
                    let slot: Option<i64> = row.get(0)?;
                    Ok(WalletPosition {
                        slot: slot.and_then(|value| u64::try_from(value).ok()),
                        signature: row.get(1)?,
                        updated_at: row.get(2)?,
                    })
                },
            )
            .optional()?)
    }

    pub fn seen_signatures(
        &self,
        wallet: &str,
        limit: usize,
    ) -> Result<Vec<String>, SignatureStoreError> {
        Self::validate_wallet(wallet)?;
        if limit == 0 || limit > 10_000 {
            return Err(SignatureStoreError::InvalidInput(
                "Seen-signature limit must be between 1 and 10000.".to_string(),
            ));
        }
        let connection = self.connect()?;
        let mut statement = connection.prepare(
            "SELECT signature FROM seen_signatures WHERE wallet_address=?1 ORDER BY first_seen_at DESC LIMIT ?2"
        )?;
        let rows = statement.query_map(params![wallet, limit as i64], |row| row.get(0))?;
        Ok(rows.collect::<Result<Vec<String>, _>>()?)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, Barrier};
    use std::thread;
    use tempfile::tempdir;

    const WALLET: &str = "11111111111111111111111111111111";
    const SIGNATURE: &str = "1111111111111111111111111111111111111111111111111111111111111111";

    #[test]
    fn atomic_claim_prevents_duplicate_workers_and_recovers_expired_lease_after_restart() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("claims.db");
        let store = SignatureStore::open(&path).unwrap();
        store.save(WALLET, SIGNATURE, Some(22)).unwrap();
        assert!(store
            .claim_signature(WALLET, SIGNATURE, "worker-a", 1_000, 30)
            .unwrap());
        assert!(!store
            .claim_signature(WALLET, SIGNATURE, "worker-b", 1_001, 30)
            .unwrap());
        assert!(store
            .release_signature_claim(WALLET, SIGNATURE, "worker-a")
            .unwrap());
        assert!(store
            .claim_signature(WALLET, SIGNATURE, "worker-b", 1_002, 30)
            .unwrap());
        assert!(!store
            .mark_claimed_processed(WALLET, SIGNATURE, "worker-a")
            .unwrap());
        assert!(store
            .mark_claimed_processed(WALLET, SIGNATURE, "worker-b")
            .unwrap());
        assert_eq!(
            store.get_status(WALLET, SIGNATURE).unwrap().as_deref(),
            Some("processed")
        );

        let recovery_signature = "1111111111111111111111111111111111111111111111111111111111111112";
        store.save(WALLET, recovery_signature, Some(23)).unwrap();
        assert!(store
            .claim_signature(WALLET, recovery_signature, "crashed-worker", 1_000, 10)
            .unwrap());
        let restarted = SignatureStore::open(&path).unwrap();
        assert_eq!(
            restarted.pending_signatures(WALLET, 10).unwrap(),
            vec![recovery_signature.to_string()]
        );
        assert!(restarted
            .claim_signature(WALLET, recovery_signature, "recovery-worker", 2_000, 30)
            .unwrap());
    }

    #[test]
    fn concurrent_connections_claim_one_signature_exactly_once() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("concurrent-claims.db");
        SignatureStore::open(&path)
            .unwrap()
            .save(WALLET, SIGNATURE, Some(33))
            .unwrap();
        let barrier = Arc::new(Barrier::new(3));
        let mut workers = Vec::new();
        for owner in ["worker-a", "worker-b"] {
            let path = path.clone();
            let barrier = Arc::clone(&barrier);
            workers.push(thread::spawn(move || {
                let store = SignatureStore::open(path).unwrap();
                barrier.wait();
                store
                    .claim_signature(WALLET, SIGNATURE, owner, 5_000, 30)
                    .unwrap()
            }));
        }
        barrier.wait();
        let mut claimed = 0;
        for worker in workers {
            if worker.join().unwrap() {
                claimed += 1;
            }
        }
        assert_eq!(claimed, 1);
    }

    #[test]
    fn fresh_and_history_rows_are_separate_and_restart_keeps_signature_observation() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("lanes.db");
        let store = SignatureStore::open(&path).unwrap();
        let fresh = SIGNATURE;
        let history = "1111111111111111111111111111111111111111111111111111111111111112";
        let observation = serde_json::json!({
            "signature": fresh,
            "slot": 90,
            "blockTime": 1_760_000_000,
            "confirmationStatus": "finalized",
            "err": null
        });
        assert!(store
            .save_in_lane(WALLET, fresh, Some(90), "fresh", Some(&observation))
            .unwrap());
        assert!(store
            .save_in_lane(WALLET, history, Some(80), "history", None)
            .unwrap());
        assert_eq!(
            store
                .pending_signatures_for_lane(WALLET, "fresh", 10)
                .unwrap(),
            vec![fresh.to_string()]
        );
        assert_eq!(
            store
                .pending_signatures_for_lane(WALLET, "history", 10)
                .unwrap(),
            vec![history.to_string()]
        );
        assert_eq!(store.pending_count_for_lane(WALLET, "fresh").unwrap(), 1);
        drop(store);

        let restarted = SignatureStore::open(&path).unwrap();
        let restored = restarted
            .pending_signature_observations(WALLET, "fresh", 10)
            .unwrap();
        assert_eq!(restored[0]["blockTime"], 1_760_000_000);
        assert_eq!(restored[0]["confirmationStatus"], "finalized");
        assert_eq!(restored[0]["slot"], 90);
    }

    #[test]
    fn existing_fresh_assignment_is_not_demoted_by_history_discovery() {
        let dir = tempdir().unwrap();
        let store = SignatureStore::open(dir.path().join("lane-promotion.db")).unwrap();
        assert!(store
            .save_in_lane(WALLET, SIGNATURE, Some(10), "fresh", None)
            .unwrap());
        assert!(!store
            .save_in_lane(WALLET, SIGNATURE, Some(10), "history", None)
            .unwrap());
        assert_eq!(
            store
                .pending_signatures_for_lane(WALLET, "fresh", 10)
                .unwrap(),
            vec![SIGNATURE.to_string()]
        );
        assert!(store
            .pending_signatures_for_lane(WALLET, "history", 10)
            .unwrap()
            .is_empty());
    }

    #[test]
    fn history_backfill_progress_survives_restart_and_stops_at_one_thousand() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("history-backfill.db");
        let cursor_a = SIGNATURE;
        let cursor_b = "1111111111111111111111111111111111111111111111111111111111111112";
        {
            let store = SignatureStore::open(&path).unwrap();
            assert!(store
                .start_history_backfill(WALLET, Some(cursor_a))
                .unwrap());
            assert!(!store
                .start_history_backfill(WALLET, Some(cursor_b))
                .unwrap());
            store
                .advance_history_backfill(WALLET, Some(cursor_b), 600, false)
                .unwrap();
        }
        let restarted = SignatureStore::open(&path).unwrap();
        assert_eq!(
            restarted.history_backfill_state(WALLET).unwrap(),
            Some((Some(cursor_b.to_string()), 600, false))
        );
        restarted
            .advance_history_backfill(WALLET, Some(cursor_b), 500, false)
            .unwrap();
        assert_eq!(
            restarted.history_backfill_state(WALLET).unwrap(),
            Some((Some(cursor_b.to_string()), 1_000, true))
        );
        assert!(!restarted
            .start_history_backfill(WALLET, Some(cursor_a))
            .unwrap());
    }

    #[test]
    fn empty_history_backfill_starts_completed() {
        let dir = tempdir().unwrap();
        let store = SignatureStore::open(dir.path().join("empty-backfill.db")).unwrap();
        assert!(store.start_history_backfill(WALLET, None).unwrap());
        assert_eq!(
            store.history_backfill_state(WALLET).unwrap(),
            Some((None, 0, true))
        );
    }

    #[test]
    fn stale_signatures_are_auditable_and_cannot_be_overwritten_as_processed() {
        let dir = tempdir().unwrap();
        let store = SignatureStore::open(dir.path().join("signatures.db")).unwrap();
        store.save(WALLET, SIGNATURE, Some(10)).unwrap();
        assert!(store.mark_stale(WALLET, SIGNATURE).unwrap());
        assert_eq!(
            store.get_status(WALLET, SIGNATURE).unwrap().as_deref(),
            Some("stale")
        );
        assert!(!store.mark_processed(WALLET, SIGNATURE).unwrap());
        assert_eq!(
            store.get_status(WALLET, SIGNATURE).unwrap().as_deref(),
            Some("stale")
        );
        let record = store
            .get_transaction_record(WALLET, SIGNATURE)
            .unwrap()
            .unwrap();
        assert_eq!(record.status, "stale");
    }

    #[test]
    fn mark_processed_preserves_failed_transaction_evidence_status() {
        let dir = tempdir().unwrap();
        let store = SignatureStore::open(dir.path().join("signatures.db")).unwrap();
        store.save(WALLET, SIGNATURE, Some(10)).unwrap();
        let raw = serde_json::json!({"meta":{"err":{"InstructionError":[0,"Custom"]}}});
        store
            .save_transaction_record(WALLET, SIGNATURE, Some(&raw), "failed")
            .unwrap();
        assert!(store.mark_processed(WALLET, SIGNATURE).unwrap());
        let record = store
            .get_transaction_record(WALLET, SIGNATURE)
            .unwrap()
            .unwrap();
        assert_eq!(record.status, "failed");
        assert_eq!(record.raw_transaction, Some(raw));
    }

    #[test]
    fn persists_and_deduplicates_signatures() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("signatures.db");
        let store = SignatureStore::open(&path).unwrap();
        assert!(store.save(WALLET, SIGNATURE, Some(10)).unwrap());
        assert!(!store.save(WALLET, SIGNATURE, Some(11)).unwrap());
        assert!(store.has_seen(WALLET, SIGNATURE).unwrap());
        assert_eq!(
            store.get_status(WALLET, SIGNATURE).unwrap().as_deref(),
            Some("pending")
        );
    }

    #[test]
    fn legacy_transaction_records_schema_migrates_without_losing_raw_evidence() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("legacy.db");
        let raw = serde_json::json!({"slot":88,"meta":{"err":null}});
        {
            let connection = Connection::open(&path).unwrap();
            connection
                .execute_batch(
                    "CREATE TABLE transaction_records (
                    wallet_address TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    raw_transaction_json TEXT,
                    processing_status TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (wallet_address, signature)
                );",
                )
                .unwrap();
            connection
                .execute(
                    "INSERT INTO transaction_records(
                    wallet_address,signature,raw_transaction_json,processing_status
                 ) VALUES(?1,?2,?3,'observed')",
                    params![WALLET, SIGNATURE, serde_json::to_string(&raw).unwrap()],
                )
                .unwrap();
        }

        let store = SignatureStore::open(&path).unwrap();
        let before = store
            .get_transaction_record(WALLET, SIGNATURE)
            .unwrap()
            .unwrap();
        assert_eq!(before.raw_transaction, Some(raw.clone()));
        assert_eq!(before.parsed_evidence, None);
        assert_eq!(before.classification, None);

        let parsed = serde_json::json!({"instructions":[{"source":"inner"}]});
        let classification = serde_json::json!({
            "transaction_type":"TRANSFER",
            "trade_signal":"NOT_COPYABLE"
        });
        store
            .save_transaction_evidence(
                WALLET,
                SIGNATURE,
                None,
                Some(&parsed),
                Some(&classification),
                "observed",
            )
            .unwrap();
        let after = store
            .get_transaction_record(WALLET, SIGNATURE)
            .unwrap()
            .unwrap();
        assert_eq!(after.raw_transaction, Some(raw));
        assert_eq!(after.parsed_evidence, Some(parsed));
        assert_eq!(after.classification, Some(classification));
    }

    #[test]
    fn save_many_deduplicates_repeated_signatures_in_one_batch() {
        let dir = tempdir().unwrap();
        let store = SignatureStore::open(dir.path().join("signatures.db")).unwrap();
        assert_eq!(
            store
                .save_many(WALLET, [SIGNATURE, SIGNATURE, SIGNATURE])
                .unwrap(),
            1
        );
        assert_eq!(
            store.pending_signatures(WALLET, 10).unwrap(),
            vec![SIGNATURE]
        );
    }

    #[test]
    fn same_signature_is_independent_for_two_wallets() {
        let dir = tempdir().unwrap();
        let store = SignatureStore::open(dir.path().join("signatures.db")).unwrap();
        const OTHER_WALLET: &str = "11111111111111111111111111111112";
        assert!(store.save(WALLET, SIGNATURE, Some(10)).unwrap());
        assert!(store.save(OTHER_WALLET, SIGNATURE, Some(10)).unwrap());
        assert!(store.has_seen(WALLET, SIGNATURE).unwrap());
        assert!(store.has_seen(OTHER_WALLET, SIGNATURE).unwrap());
    }

    #[test]
    fn concurrent_duplicate_saves_commit_exactly_one_signature_row() {
        let dir = tempdir().unwrap();
        let store = Arc::new(SignatureStore::open(dir.path().join("signatures.db")).unwrap());
        let barrier = Arc::new(Barrier::new(8));
        let handles = (0..8)
            .map(|_| {
                let store = Arc::clone(&store);
                let barrier = Arc::clone(&barrier);
                thread::spawn(move || {
                    barrier.wait();
                    store.save(WALLET, SIGNATURE, Some(10)).unwrap()
                })
            })
            .collect::<Vec<_>>();
        let inserted = handles
            .into_iter()
            .map(|handle| usize::from(handle.join().unwrap()))
            .sum::<usize>();
        assert_eq!(inserted, 1);
        assert_eq!(store.pending_signatures(WALLET, 10).unwrap().len(), 1);
    }

    #[test]
    fn records_bounded_failures_and_review_state() {
        let dir = tempdir().unwrap();
        let store = SignatureStore::open(dir.path().join("signatures.db")).unwrap();
        for attempt in 1..=5 {
            let result = store
                .record_failure(
                    WALLET,
                    SIGNATURE,
                    ProcessingStage::RpcFetch,
                    Some("rpc_error"),
                    Some("provider unavailable"),
                    5,
                )
                .unwrap();
            assert_eq!(result.attempts, attempt);
            assert_eq!(
                result.status,
                if attempt == 5 {
                    "needs_review"
                } else {
                    "pending"
                }
            );
        }
    }

    #[test]
    fn transaction_record_is_preserved_when_later_update_has_no_payload() {
        let dir = tempdir().unwrap();
        let store = SignatureStore::open(dir.path().join("signatures.db")).unwrap();
        let raw = serde_json::json!({"slot":10,"meta":{"err":null}});
        store
            .save_transaction_record(WALLET, SIGNATURE, Some(&raw), "observed")
            .unwrap();
        store
            .save_transaction_record(WALLET, SIGNATURE, None, "processed")
            .unwrap();
        let record = store
            .get_transaction_record(WALLET, SIGNATURE)
            .unwrap()
            .unwrap();
        assert_eq!(record.raw_transaction, Some(raw));
        assert_eq!(record.status, "processed");
    }

    #[test]
    fn restart_preserves_position_processed_status_and_instruction_evidence() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("signatures.db");
        let raw_evidence = serde_json::json!({
            "transaction": {"message": {"instructions": [{
                "program": "system",
                "parsed": {"type": "transfer"}
            }]}},
            "meta": {"innerInstructions": [{
                "index": 0,
                "instructions": [{
                    "program": "spl-token-2022",
                    "parsed": {"type": "transferChecked"}
                }]
            }]}
        });
        let parsed_evidence = serde_json::json!({
            "instructions": [
                {"source":"message","parsed_type":"transfer"},
                {"source":"inner","parsed_type":"transferChecked"}
            ]
        });
        let classification = serde_json::json!({
            "transaction_type":"TRANSFER",
            "trade_signal":"NOT_COPYABLE"
        });
        {
            let store = SignatureStore::open(&path).unwrap();
            store.save(WALLET, SIGNATURE, Some(42)).unwrap();
            store
                .save_transaction_evidence(
                    WALLET,
                    SIGNATURE,
                    Some(&raw_evidence),
                    Some(&parsed_evidence),
                    Some(&classification),
                    "observed",
                )
                .unwrap();
            assert!(store.mark_processed(WALLET, SIGNATURE).unwrap());
        }
        let store = SignatureStore::open(&path).unwrap();
        assert_eq!(
            store.get_status(WALLET, SIGNATURE).unwrap().as_deref(),
            Some("processed")
        );
        assert_eq!(store.get_position(WALLET).unwrap().unwrap().slot, Some(42));
        let record = store
            .get_transaction_record(WALLET, SIGNATURE)
            .unwrap()
            .unwrap();
        assert_eq!(record.raw_transaction, Some(raw_evidence));
        assert_eq!(record.parsed_evidence, Some(parsed_evidence));
        assert_eq!(record.classification, Some(classification));
        assert_eq!(record.status, "processed");
    }
}
