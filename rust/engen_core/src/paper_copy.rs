//! Maps to engine/paper_copy_runner.py (queue side, paper only).

use crate::paper_simulator::{OrderSide, PaperOrder, PaperSimulator};
use crate::source_freshness::source_block_time_ok;
use crate::Result;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MonitorCandidate {
    pub signature: String,
    pub wallet: String,
    pub side: OrderSide,
    pub asset_mint: String,
    pub block_time: Option<i64>,
    pub confirmation_status: String,
    pub metadata_complete: bool,
}

/// Queue paper orders from monitor candidates. Skips stale / incomplete.
pub fn queue_paper_orders(
    sim: &mut PaperSimulator,
    candidates: &[MonitorCandidate],
) -> Result<Vec<PaperOrder>> {
    let mut queued = Vec::new();
    for c in candidates {
        if !c.metadata_complete {
            continue;
        }
        if c.confirmation_status != "confirmed" && c.confirmation_status != "finalized" {
            continue;
        }
        if !source_block_time_ok(c.block_time, None) {
            continue;
        }
        let signal_id = format!("wallet-copy:{}:{}", c.wallet, c.signature);
        let order = sim.create_order(
            &signal_id,
            c.side.clone(),
            &c.asset_mint,
            "1",
            "USD",
            serde_json::json!({
                "origin": "AUTOMATIC_WALLET_COPY",
                "source_wallet": c.wallet,
                "source_signature": c.signature,
                "source_block_time": c.block_time,
            }),
        )?;
        queued.push(order);
    }
    Ok(queued)
}
