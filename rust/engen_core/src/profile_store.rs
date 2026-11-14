//! Maps to engine/profile_store.py — paper profiles only, no keys.

use crate::error::{EngenError, Result};
use rusqlite::{params, Connection};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use uuid::Uuid;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Profile {
    pub profile_id: String,
    pub name: String,
    pub source_wallets: Vec<String>,
    pub active: bool,
    #[serde(default = "default_status")]
    pub status: String,
    #[serde(default)]
    pub destination_public_address: Option<String>,
    #[serde(default)]
    pub settings: serde_json::Value,
}

fn default_status() -> String {
    "draft".into()
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
            CREATE TABLE IF NOT EXISTS observed_swaps (
                signature TEXT PRIMARY KEY,
                wallet TEXT NOT NULL,
                classification TEXT NOT NULL,
                asset_mint TEXT,
                quote_mint TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS profile_monitor (
                profile_id TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL
            );
            "#,
        )?;
        for sql in [
            "ALTER TABLE profiles ADD COLUMN status TEXT NOT NULL DEFAULT 'draft'",
            "ALTER TABLE profiles ADD COLUMN destination TEXT",
            "ALTER TABLE profiles ADD COLUMN settings TEXT NOT NULL DEFAULT '{}'",
        ] {
            let _ = self.conn()?.execute(sql, []);
        }
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
        let status = if active { "active" } else { "draft" };
        self.conn()?.execute(
            "INSERT INTO profiles(profile_id, name, source_wallets, active, status, destination, settings)
             VALUES(?1, ?2, ?3, ?4, ?5, NULL, '{}')
             ON CONFLICT(profile_id) DO UPDATE SET
               name=excluded.name,
               source_wallets=excluded.source_wallets,
               active=excluded.active,
               status=excluded.status",
            params![id, name, wallets_json, if active { 1 } else { 0 }, status],
        )?;
        self.get(&id)?.ok_or_else(|| EngenError::NotFound(id))
    }

    pub fn list(&self) -> Result<Vec<Profile>> {
        let conn = self.conn()?;
        let mut stmt = conn.prepare(
            "SELECT profile_id, name, source_wallets, active, status, destination, settings FROM profiles ORDER BY name",
        )?;
        let rows = stmt.query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, i64>(3)?,
                row.get::<_, String>(4)?,
                row.get::<_, Option<String>>(5)?,
                row.get::<_, String>(6)?,
            ))
        })?;
        let mut out = Vec::new();
        for row in rows {
            let (profile_id, name, wallets, active, status, destination, settings) = row?;
            let source_wallets: Vec<String> =
                serde_json::from_str(&wallets).unwrap_or_default();
            let settings: serde_json::Value =
                serde_json::from_str(&settings).unwrap_or(serde_json::json!({}));
            out.push(Profile {
                profile_id,
                name,
                source_wallets,
                active: active != 0 || status == "active",
                status,
                destination_public_address: destination,
                settings,
            });
        }
        Ok(out)
    }

    pub fn get(&self, profile_id: &str) -> Result<Option<Profile>> {
        Ok(self.list()?.into_iter().find(|p| p.profile_id == profile_id))
    }

    pub fn create_profile(&self, name: &str) -> Result<Profile> {
        if self.list()?.len() >= 3 {
            return Err(EngenError::InvalidInput(
                "Maximum of 3 paper profiles reached.".into(),
            ));
        }
        self.upsert(name, &[], false, None)
    }

    pub fn set_status(&self, profile_id: &str, status: &str) -> Result<Profile> {
        let status = status.trim().to_ascii_lowercase();
        if status != "active" && status != "draft" {
            return Err(EngenError::InvalidInput("status must be active or draft".into()));
        }
        let changed = self.conn()?.execute(
            "UPDATE profiles SET status=?1, active=?2 WHERE profile_id=?3",
            params![status, if status == "active" { 1 } else { 0 }, profile_id],
        )?;
        if changed == 0 {
            return Err(EngenError::NotFound("Profile not found.".into()));
        }
        self.get(profile_id)?.ok_or_else(|| EngenError::NotFound("Profile not found.".into()))
    }

    pub fn set_settings(&self, profile_id: &str, settings: &serde_json::Value) -> Result<Profile> {
        if self.get(profile_id)?.is_none() {
            return Err(EngenError::NotFound("Profile not found.".into()));
        }
        self.conn()?.execute(
            "UPDATE profiles SET settings=?1 WHERE profile_id=?2",
            params![settings.to_string(), profile_id],
        )?;
        self.get(profile_id)?.ok_or_else(|| EngenError::NotFound("Profile not found.".into()))
    }

    pub fn associate_wallet(&self, profile_id: &str, wallet: &str) -> Result<Profile> {
        if !is_pubkey_like(wallet) {
            return Err(EngenError::InvalidInput("Invalid-looking Solana address".into()));
        }
        let mut profile = self
            .get(profile_id)?
            .ok_or_else(|| EngenError::NotFound("Profile not found.".into()))?;
        if !profile.source_wallets.iter().any(|w| w == wallet) {
            profile.source_wallets.push(wallet.to_string());
        }
        self.conn()?.execute(
            "UPDATE profiles SET source_wallets=?1 WHERE profile_id=?2",
            params![serde_json::to_string(&profile.source_wallets).unwrap(), profile_id],
        )?;
        let _ = self.add_wallet(wallet);
        self.get(profile_id)?.ok_or_else(|| EngenError::NotFound("Profile not found.".into()))
    }

    pub fn disassociate_wallet(&self, profile_id: &str, wallet: &str) -> Result<Profile> {
        let mut profile = self
            .get(profile_id)?
            .ok_or_else(|| EngenError::NotFound("Profile not found.".into()))?;
        profile.source_wallets.retain(|w| w != wallet);
        self.conn()?.execute(
            "UPDATE profiles SET source_wallets=?1 WHERE profile_id=?2",
            params![serde_json::to_string(&profile.source_wallets).unwrap(), profile_id],
        )?;
        self.get(profile_id)?.ok_or_else(|| EngenError::NotFound("Profile not found.".into()))
    }

    pub fn delete_profile(&self, profile_id: &str) -> Result<()> {
        let changed = self
            .conn()?
            .execute("DELETE FROM profiles WHERE profile_id=?1", params![profile_id])?;
        if changed == 0 {
            return Err(EngenError::NotFound("Profile not found.".into()));
        }
        let _ = self.conn()?.execute(
            "DELETE FROM profile_monitor WHERE profile_id=?1",
            params![profile_id],
        );
        Ok(())
    }

    pub fn set_monitor(&self, profile_id: &str, enabled: bool) -> Result<serde_json::Value> {
        let profile = self
            .get(profile_id)?
            .ok_or_else(|| EngenError::NotFound("Profile not found.".into()))?;
        if enabled {
            if profile.status != "active" {
                return Err(EngenError::InvalidInput(
                    "Activate this profile before starting monitoring.".into(),
                ));
            }
            if profile.source_wallets.is_empty() {
                return Err(EngenError::InvalidInput(
                    "Associate at least one source public wallet with this profile first.".into(),
                ));
            }
        }
        self.conn()?.execute(
            "INSERT INTO profile_monitor(profile_id, enabled) VALUES(?1, ?2)
             ON CONFLICT(profile_id) DO UPDATE SET enabled=excluded.enabled",
            params![profile_id, if enabled { 1 } else { 0 }],
        )?;
        self.monitor_status(profile_id)
    }

    pub fn monitor_status(&self, profile_id: &str) -> Result<serde_json::Value> {
        if self.get(profile_id)?.is_none() {
            return Err(EngenError::NotFound("Profile not found.".into()));
        }
        let enabled: i64 = self.conn()?.query_row(
            "SELECT enabled FROM profile_monitor WHERE profile_id=?1",
            params![profile_id],
            |row| row.get(0),
        ).unwrap_or(0);
        Ok(serde_json::json!({
            "profile_id": profile_id,
            "monitor": {
                "enabled": enabled != 0,
                "interval_seconds": 15,
                "wallets": [],
                "real_trading": false,
                "transaction_broadcast": false
            }
        }))
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

    pub fn record_swap(
        &self,
        signature: &str,
        wallet: &str,
        classification: &str,
        asset_mint: Option<&str>,
        quote_mint: Option<&str>,
    ) -> Result<()> {
        if signature.is_empty() {
            return Ok(());
        }
        let now = chrono::Utc::now().to_rfc3339();
        self.conn()?.execute(
            "INSERT INTO observed_swaps(signature, wallet, classification, asset_mint, quote_mint, created_at)
             VALUES(?1, ?2, ?3, ?4, ?5, ?6)
             ON CONFLICT(signature) DO NOTHING",
            params![signature, wallet, classification, asset_mint, quote_mint, now],
        )?;
        Ok(())
    }

    pub fn list_swaps(&self, limit: i64) -> Result<Vec<serde_json::Value>> {
        let limit = limit.clamp(1, 100);
        let conn = self.conn()?;
        let mut stmt = conn.prepare(
            "SELECT signature, wallet, classification, asset_mint, quote_mint, created_at
             FROM observed_swaps ORDER BY created_at DESC LIMIT ?1",
        )?;
        let rows = stmt.query_map(params![limit], |row| {
            Ok(serde_json::json!({
                "signature": row.get::<_, String>(0)?,
                "wallet": row.get::<_, String>(1)?,
                "classification": row.get::<_, String>(2)?,
                "asset_mint": row.get::<_, Option<String>>(3)?,
                "quote_mint": row.get::<_, Option<String>>(4)?,
                "created_at": row.get::<_, String>(5)?,
                "real_trading": false
            }))
        })?;
        let mut out = Vec::new();
        for row in rows {
            out.push(row?);
        }
        Ok(out)
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
