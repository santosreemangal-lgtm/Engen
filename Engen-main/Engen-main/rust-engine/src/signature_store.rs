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
                processing_status TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (wallet_address, signature)
            );
            ",
        )?;

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
        Self::validate_wallet(wallet)?;
        Self::validate_signature(signature)?;
        let slot = match slot {
            Some(value) if value > i64::MAX as u64 => {
                return Err(SignatureStoreError::InvalidInput(
                    "Slot is out of range.".to_string(),
                ));
            }
            Some(value) => Some(value as i64),
            None => None,
        };
        let mut connection = self.connect()?;
        let transaction = Self::transaction(&mut connection)?;
        let inserted = transaction.execute(
            "INSERT OR IGNORE INTO seen_signatures(wallet_address,signature,status,slot) VALUES(?1,?2,'pending',?3)",
            params![wallet, signature, slot],
        )?;
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
                "INSERT OR IGNORE INTO seen_signatures(wallet_address,signature,status) VALUES(?1,?2,'pending')",
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
             SET status='stale',last_checked_at=CURRENT_TIMESTAMP,
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
            "UPDATE seen_signatures SET status='processed', last_checked_at=CURRENT_TIMESTAMP, error_stage=NULL, error_code=NULL, error_message=NULL WHERE wallet_address=?1 AND signature=?2 AND status!='stale'",
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
            "UPDATE seen_signatures SET status=?1,attempts=?2,last_checked_at=CURRENT_TIMESTAMP,error_stage=?3,error_code=?4,error_message=?5 WHERE wallet_address=?6 AND signature=?7",
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
        let raw_json = raw_transaction.map(serde_json::to_string).transpose()?;

        let connection = self.connect()?;
        connection.execute(
            "INSERT INTO transaction_records(wallet_address,signature,raw_transaction_json,processing_status)
             VALUES(?1,?2,?3,?4)
             ON CONFLICT(wallet_address,signature) DO UPDATE SET
               raw_transaction_json=COALESCE(excluded.raw_transaction_json,transaction_records.raw_transaction_json),
               processing_status=excluded.processing_status,updated_at=CURRENT_TIMESTAMP",
            params![wallet, signature, raw_json, processing_status],
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
            "SELECT raw_transaction_json,processing_status,updated_at FROM transaction_records WHERE wallet_address=?1 AND signature=?2",
            params![wallet, signature],
            |row| {
                Ok((
                    row.get::<_, Option<String>>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                ))
            },
        ).optional()?;
        row.map(|(raw, status, updated)| {
            Ok(TransactionRecord {
                raw_transaction: raw.map(|value| serde_json::from_str(&value)).transpose()?,
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
            "SELECT signature FROM seen_signatures WHERE wallet_address=?1 AND status='pending' ORDER BY first_seen_at ASC LIMIT ?2"
        )?;
        let rows = statement.query_map(params![wallet, limit as i64], |row| row.get(0))?;
        Ok(rows.collect::<Result<Vec<String>, _>>()?)
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
        let evidence = serde_json::json!({
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
        {
            let store = SignatureStore::open(&path).unwrap();
            store.save(WALLET, SIGNATURE, Some(42)).unwrap();
            store
                .save_transaction_record(WALLET, SIGNATURE, Some(&evidence), "observed")
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
        assert_eq!(record.raw_transaction, Some(evidence));
        assert_eq!(record.status, "processed");
    }
}
