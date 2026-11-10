//! Maps to engine/transaction_monitor.py.
//!
//! Phase R3: implement JSON-RPC polling and/or Yellowstone gRPC subscribe.
//! Paper path only — never signs or broadcasts.

use crate::paper_copy::MonitorCandidate;
use crate::{EngenError, Result};

/// Placeholder monitor: returns no candidates until RPC/gRPC is wired.
pub struct TransactionMonitor {
    pub wallet: String,
}

impl TransactionMonitor {
    pub fn new(wallet: impl Into<String>) -> Self {
        Self {
            wallet: wallet.into(),
        }
    }

    pub fn poll_candidates(&self) -> Result<Vec<MonitorCandidate>> {
        // Intentionally empty until network transport is configured.
        // Avoids accidental live behavior in the scaffold.
        let _ = &self.wallet;
        Ok(vec![])
    }

    pub fn require_not_live(&self) -> Result<()> {
        // Future: if someone enables broadcast flags, hard-fail here.
        Err(EngenError::LiveDisabled).or(Ok(()))
    }
}
