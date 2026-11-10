//! Read-only public signature polling. Never signs or broadcasts.
//!
//! `getSignaturesForAddress` does not include swap side or mints.
//! Those candidates are ingested only after classification (Python or a later parser).

use crate::error::{EngenError, Result};
use crate::paper_copy::MonitorCandidate;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct SignatureObservation {
    pub signature: String,
    pub slot: Option<i64>,
    pub confirmation_status: Option<String>,
    pub block_time: Option<i64>,
    pub failed: bool,
}

#[derive(Debug, Clone)]
pub struct TransactionMonitor {
    pub wallet: String,
    rpc_url: Option<String>,
    observations: Vec<SignatureObservation>,
    candidates: Vec<MonitorCandidate>,
}

impl TransactionMonitor {
    pub fn new(wallet: impl Into<String>) -> Self {
        Self {
            wallet: wallet.into(),
            rpc_url: None,
            observations: Vec::new(),
            candidates: Vec::new(),
        }
    }

    pub fn with_rpc(wallet: impl Into<String>, rpc_url: impl Into<String>) -> Self {
        let mut monitor = Self::new(wallet);
        monitor.rpc_url = Some(rpc_url.into());
        monitor
    }

    pub fn rpc_url(&self) -> Option<&str> {
        self.rpc_url.as_deref()
    }

    /// Store already-classified paper candidates. Deduped by signature.
    pub fn ingest(&mut self, incoming: Vec<MonitorCandidate>) {
        for item in incoming {
            if self
                .candidates
                .iter()
                .any(|c| c.signature == item.signature && c.wallet == item.wallet)
            {
                continue;
            }
            self.candidates.push(item);
        }
    }

    /// Parse a `getSignaturesForAddress` result array. Does not create trades.
    pub fn record_signature_list(&mut self, value: &serde_json::Value) -> Result<usize> {
        let rows = value
            .as_array()
            .ok_or_else(|| EngenError::InvalidInput("signature list must be an array".into()))?;
        let mut added = 0;
        for row in rows {
            let Some(signature) = row.get("signature").and_then(|v| v.as_str()) else {
                continue;
            };
            if signature.is_empty() {
                continue;
            }
            if self.observations.iter().any(|o| o.signature == signature) {
                continue;
            }
            let failed = !row.get("err").map(|v| v.is_null()).unwrap_or(true);
            self.observations.push(SignatureObservation {
                signature: signature.to_string(),
                slot: row.get("slot").and_then(|v| v.as_i64()),
                confirmation_status: row
                    .get("confirmationStatus")
                    .and_then(|v| v.as_str())
                    .map(|s| s.to_string()),
                block_time: row.get("blockTime").and_then(|v| v.as_i64()),
                failed,
            });
            added += 1;
        }
        Ok(added)
    }

    pub fn observations(&self) -> &[SignatureObservation] {
        &self.observations
    }

    /// Drain classified candidates only. Raw signatures stay observations.
    pub fn poll_candidates(&mut self) -> Result<Vec<MonitorCandidate>> {
        Ok(std::mem::take(&mut self.candidates))
    }

    /// Read-only JSON-RPC. Returns the result array, or an error. No signing.
    pub fn fetch_signatures(&self, limit: u32) -> Result<serde_json::Value> {
        let url = self
            .rpc_url
            .as_deref()
            .ok_or_else(|| EngenError::InvalidInput("rpc url is not set".into()))?;
        let limit = limit.clamp(1, 25);
        let body = serde_json::json!({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getSignaturesForAddress",
            "params": [self.wallet, {"limit": limit, "commitment": "confirmed"}]
        });
        let response = ureq::post(url)
            .set("Content-Type", "application/json")
            .send_json(body)
            .map_err(|e| EngenError::Other(format!("rpc request failed: {e}")))?;
        let parsed: serde_json::Value = response
            .into_json()
            .map_err(|e| EngenError::Other(format!("rpc json failed: {e}")))?;
        if let Some(err) = parsed.get("error") {
            return Err(EngenError::Other(format!("rpc error: {err}")));
        }
        parsed
            .get("result")
            .cloned()
            .ok_or_else(|| EngenError::Other("rpc response missing result".into()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::paper_simulator::OrderSide;

    #[test]
    fn signature_list_does_not_invent_trades() {
        let mut monitor = TransactionMonitor::new("WalletA");
        let n = monitor
            .record_signature_list(&serde_json::json!([
                {
                    "signature": "sig1",
                    "slot": 10,
                    "confirmationStatus": "finalized",
                    "blockTime": 1_700_000_000,
                    "err": null
                }
            ]))
            .unwrap();
        assert_eq!(n, 1);
        assert!(monitor.poll_candidates().unwrap().is_empty());
        assert_eq!(monitor.observations().len(), 1);
        assert!(!monitor.observations()[0].failed);
    }

    #[test]
    fn ingest_is_deduped() {
        let mut monitor = TransactionMonitor::new("WalletA");
        let c = MonitorCandidate {
            signature: "sig".into(),
            wallet: "WalletA".into(),
            side: OrderSide::Buy,
            asset_mint: "Mint".into(),
            block_time: Some(1_700_000_000),
            confirmation_status: "finalized".into(),
            metadata_complete: true,
            target_notional_usd: Some("5".into()),
            source_sell_fraction: None,
        };
        monitor.ingest(vec![c.clone(), c]);
        assert_eq!(monitor.poll_candidates().unwrap().len(), 1);
        assert!(monitor.poll_candidates().unwrap().is_empty());
    }
}
