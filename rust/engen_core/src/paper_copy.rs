//! Maps to engine/paper_copy_runner.py (paper only).

use crate::paper_simulator::{MarketPrice, OrderSide, PaperOrder, PaperSimulator};
use crate::source_freshness::source_block_time_ok;
use crate::Result;
use rust_decimal::Decimal;
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
    /// For BUY: optional USD notional target (string decimal).
    pub target_notional_usd: Option<String>,
    /// For SELL: fraction of paper position (0–1).
    pub source_sell_fraction: Option<String>,
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
        let mut details = serde_json::json!({
            "origin": "AUTOMATIC_WALLET_COPY",
            "source_wallet": c.wallet,
            "source_signature": c.signature,
            "source_block_time": c.block_time,
        });
        if let Some(ref n) = c.target_notional_usd {
            details["target_notional_usd"] = serde_json::json!(n);
        }
        if let Some(ref f) = c.source_sell_fraction {
            details["source_sell_fraction"] = serde_json::json!(f);
        }
        let order = sim.create_order(
            &signal_id,
            c.side.clone(),
            &c.asset_mint,
            "1",
            "USD",
            details,
        )?;
        queued.push(order);
    }
    Ok(queued)
}

/// Try to fill all CREATED automatic orders with one market snapshot provider.
pub fn execute_pending_paper_orders(
    sim: &mut PaperSimulator,
    price_for_mint: &dyn Fn(&str) -> Option<MarketPrice>,
) -> Result<Vec<serde_json::Value>> {
    let mut outcomes = Vec::new();
    for order_id in sim.pending_order_ids() {
        // Re-read side/mint from report path via execute which loads order
        let order_mint = {
            // execute_order needs market; we need mint from pending list — use report
            let report = sim.report();
            report["orders"]
                .as_array()
                .and_then(|arr| {
                    arr.iter().find(|o| o["order_id"] == order_id)
                })
                .and_then(|o| o["asset_mint"].as_str().map(|s| s.to_string()))
        };
        let Some(mint) = order_mint else {
            continue;
        };
        let Some(market) = price_for_mint(&mint) else {
            outcomes.push(serde_json::json!({
                "order_id": order_id,
                "status": "WAITING_FOR_MARKET_DATA",
                "reason": "No market snapshot",
            }));
            continue;
        };
        let result = sim.execute_order(
            &order_id,
            &market,
            50,
            10,
            Decimal::from(10_000),
            60,
        )?;
        outcomes.push(result);
    }
    Ok(outcomes)
}
