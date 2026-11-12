//! Maps to engine/profile_store.py — paper profiles only, no keys.

use crate::error::{EngenError, Result};
use rusqlite::{params, Connection};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use uuid::Uuid;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Profile {
    pub profile_id: String,
    pub name: String,
    pub source_wallets: Vec<String>,
    pub active: bool,
}

#[derive(Debug)]
pub struct ProfileStore {
    path: PathBuf,
}

impl ProfileStore {
    pub fn open(path: impl AsRef<Path>) -> Result<Self> {
        let path = path.as_ref().to_path_buf();
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(|e| EngenError::Other(e.to_string()))?;
        }
        let store = Self { path };
        store.init()?;
        Ok(store)
    }

    fn conn(&self) -> Result<Connection> {
        Ok(Connection::open(&self.path)?)
    }

    fn init(&self) -> Result<()> {
        self.conn()?.execute_batch(
            r#"
            CREATE TABLE IF NOT EXISTS profiles (
                profile_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                source_wallets TEXT NOT NULL,
                active INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS watched_wallets (
                address TEXT PRIMARY KEY
            );
            "#,
        )?;
        Ok(())
    }

    pub fn upsert(
        &self,
        name: &str,
        source_wallets: &[String],
        active: bool,
        profile_id: Option<&str>,
    ) -> Result<Profile> {
        let name = name.trim();
        if name.is_empty() {
            return Err(EngenError::InvalidInput("profile name required".into()));
        }
        let id = profile_id
            .map(|s| s.to_string())
            .unwrap_or_else(|| Uuid::new_v4().to_string());
        let wallets_json = serde_json::to_string(source_wallets)
            .map_err(|e| EngenError::Other(e.to_string()))?;
        self.conn()?.execute(
            "INSERT INTO profiles(profile_id, name, source_wallets, active)
             VALUES(?1, ?2, ?3, ?4)
             ON CONFLICT(profile_id) DO UPDATE SET
               name=excluded.name,
               source_wallets=excluded.source_wallets,
               active=excluded.active",
            params![id, name, wallets_json, if active { 1 } else { 0 }],
        )?;
        Ok(Profile {
            profile_id: id,
            name: name.to_string(),
            source_wallets: source_wallets.to_vec(),
            active,
        })
    }

    pub fn list(&self) -> Result<Vec<Profile>> {
        let conn = self.conn()?;
        let mut stmt = conn.prepare(
            "SELECT profile_id, name, source_wallets, active FROM profiles ORDER BY name",
        )?;
        let rows = stmt.query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, i64>(3)?,
            ))
        })?;
        let mut out = Vec::new();
        for row in rows {
            let (profile_id, name, wallets, active) = row?;
            let source_wallets: Vec<String> =
                serde_json::from_str(&wallets).unwrap_or_default();
            out.push(Profile {
                profile_id,
                name,
                source_wallets,
                active: active != 0,
            });
        }
        Ok(out)
    }

    pub fn wallets(&self) -> Result<Vec<String>> {
        let conn = self.conn()?;
        let mut stmt = conn.prepare("SELECT address FROM watched_wallets ORDER BY address")?;
        let rows = stmt.query_map([], |row| row.get::<_, String>(0))?;
        let mut out = Vec::new();
        for row in rows {
            out.push(row?);
        }
        Ok(out)
    }

    pub fn add_wallet(&self, address: &str) -> Result<Vec<String>> {
        if !is_pubkey_like(address) {
            return Err(EngenError::InvalidInput("Invalid-looking Solana address".into()));
        }
        self.conn()?.execute(
            "INSERT INTO watched_wallets(address) VALUES(?1) ON CONFLICT(address) DO NOTHING",
            params![address],
        )?;
        self.wallets()
    }

    pub fn remove_wallet(&self, address: &str) -> Result<Vec<String>> {
        self.conn()?
            .execute("DELETE FROM watched_wallets WHERE address=?1", params![address])?;
        self.wallets()
    }
}

fn is_pubkey_like(value: &str) -> bool {
    let n = value.len();
    (32..=44).contains(&n)
        && value
            .chars()
            .all(|c| matches!(c, '1'..='9' | 'A'..='H' | 'J'..='N' | 'P'..='Z' | 'a'..='k' | 'm'..='z'))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn profile_survives_reopen() {
        let dir = std::env::temp_dir().join(format!("engen-prof-{}", Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        let db = dir.join("profiles.db");
        let id = {
            let store = ProfileStore::open(&db).unwrap();
            let p = store
                .upsert("alpha", &["Wallet111".into()], true, None)
                .unwrap();
            p.profile_id
        };
        let store = ProfileStore::open(&db).unwrap();
        let list = store.list().unwrap();
        assert_eq!(list.len(), 1);
        assert_eq!(list[0].profile_id, id);
        assert_eq!(list[0].source_wallets, vec!["Wallet111".to_string()]);
        let _ = std::fs::remove_dir_all(dir);
    }
}
