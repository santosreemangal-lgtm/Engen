//! Maps to engine/profile_store.py — stub for Phase R4.

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Profile {
    pub profile_id: String,
    pub name: String,
    pub source_wallets: Vec<String>,
    pub active: bool,
}

/// Placeholder until rusqlite profile schema is ported.
#[derive(Debug, Default)]
pub struct ProfileStore {
    profiles: Vec<Profile>,
}

impl ProfileStore {
    pub fn list(&self) -> &[Profile] {
        &self.profiles
    }
}
