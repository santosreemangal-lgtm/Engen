//! Deduped signature ledger. Maps to engine/signature_store.py (subset).

use crate::{EngenError, Result};
use rusqlite::{params, Connection};
use std::path::{Path, PathBuf};

#[derive(Debug)]
pub struct SignatureStore {
    path: PathBuf,
}

impl SignatureStore {
    pub fn open(path: impl AsRef<Path>) -> Result<Self> {
        let path = path.as_ref().to_path_buf();
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(|e| EngenError::Other(e.to_string()))?;
        }
        let store = Self { path };
        store.conn()?.execute_batch(
            "CREATE TABLE IF NOT EXISTS signatures (
                wallet TEXT NOT NULL,
                signature TEXT NOT NULL,
                processed INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (wallet, signature)
            );",
        )?;
        Ok(store)
    }

    fn conn(&self) -> Result<Connection> {
        Ok(Connection::open(&self.path)?)
    }

    pub fn seen(&self, wallet: &str, signature: &str) -> Result<bool> {
        let conn = self.conn()?;
        let n: i64 = conn.query_row(
            "SELECT COUNT(*) FROM signatures WHERE wallet=?1 AND signature=?2",
            params![wallet, signature],
            |row| row.get(0),
        )?;
        Ok(n > 0)
    }

    pub fn remember(&self, wallet: &str, signature: &str) -> Result<()> {
        self.conn()?.execute(
            "INSERT INTO signatures(wallet, signature, processed) VALUES(?1, ?2, 0)
             ON CONFLICT(wallet, signature) DO NOTHING",
            params![wallet, signature],
        )?;
        Ok(())
    }

    pub fn mark_processed(&self, wallet: &str, signature: &str) -> Result<()> {
        self.conn()?.execute(
            "UPDATE signatures SET processed=1 WHERE wallet=?1 AND signature=?2",
            params![wallet, signature],
        )?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn remember_is_idempotent() {
        let path = std::env::temp_dir().join(format!("engen-sig-{}.db", uuid::Uuid::new_v4()));
        let store = SignatureStore::open(&path).unwrap();
        store.remember("w", "s").unwrap();
        store.remember("w", "s").unwrap();
        assert!(store.seen("w", "s").unwrap());
        store.mark_processed("w", "s").unwrap();
        let _ = std::fs::remove_file(path);
    }
}
