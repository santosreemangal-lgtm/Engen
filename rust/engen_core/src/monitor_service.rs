//! Paper monitor loop: classified candidates → queue → optional fill.

use crate::paper_copy::{execute_pending_paper_orders, queue_paper_orders, MonitorCandidate};
use crate::paper_simulator::{MarketPrice, PaperSimulator};
use crate::transaction_monitor::TransactionMonitor;
use crate::Result;

pub struct MonitorService {
    pub monitors: Vec<TransactionMonitor>,
}

impl MonitorService {
    pub fn new() -> Self {
        Self {
            monitors: Vec::new(),
        }
    }

    pub fn add_wallet(&mut self, wallet: impl Into<String>) {
        let wallet = wallet.into();
        if self.monitors.iter().any(|m| m.wallet == wallet) {
            return;
        }
        self.monitors.push(TransactionMonitor::new(wallet));
    }

    pub fn add_wallet_rpc(&mut self, wallet: impl Into<String>, rpc_url: impl Into<String>) {
        let wallet = wallet.into();
        if self.monitors.iter().any(|m| m.wallet == wallet) {
            return;
        }
        self.monitors
            .push(TransactionMonitor::with_rpc(wallet, rpc_url));
    }

    pub fn ingest(&mut self, wallet: &str, candidates: Vec<MonitorCandidate>) {
        if let Some(monitor) = self.monitors.iter_mut().find(|m| m.wallet == wallet) {
            monitor.ingest(candidates);
        }
    }

    /// Poll classified candidates and queue paper orders. Does not hit the network.
    pub fn tick(&mut self, sim: &mut PaperSimulator) -> Result<usize> {
        let mut all = Vec::new();
        for monitor in &mut self.monitors {
            all.extend(monitor.poll_candidates()?);
        }
        let queued = queue_paper_orders(sim, &all)?;
        Ok(queued.len())
    }

    pub fn tick_and_fill(
        &mut self,
        sim: &mut PaperSimulator,
        price_for_mint: &dyn Fn(&str) -> Option<MarketPrice>,
    ) -> Result<Vec<serde_json::Value>> {
        self.tick(sim)?;
        execute_pending_paper_orders(sim, price_for_mint)
    }
}

impl Default for MonitorService {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::paper_simulator::OrderSide;
    use rust_decimal::Decimal;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn now() -> i64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs() as i64)
            .unwrap_or(0)
    }

    #[test]
    fn tick_queues_ingested_buy() {
        let mut service = MonitorService::new();
        service.add_wallet("WalletA");
        service.ingest(
            "WalletA",
            vec![MonitorCandidate {
                signature: "sig-tick".into(),
                wallet: "WalletA".into(),
                side: OrderSide::Buy,
                asset_mint: "TOKEN".into(),
                block_time: Some(now() - 2),
                confirmation_status: "finalized".into(),
                metadata_complete: true,
                target_notional_usd: Some("10".into()),
                source_sell_fraction: None,
            }],
        );
        let mut sim = PaperSimulator::new();
        sim.set_virtual_balance("100", "USD").unwrap();
        assert_eq!(service.tick(&mut sim).unwrap(), 1);
        assert_eq!(sim.pending_automatic_count(), 1);
        let outcomes = service
            .tick_and_fill(&mut sim, &|mint| {
                if mint == "TOKEN" {
                    Some(MarketPrice {
                        price_usd: Decimal::from(2),
                        liquidity_usd: Decimal::from(20_000),
                        age_seconds: 1,
                    })
                } else {
                    None
                }
            })
            .unwrap();
        assert!(outcomes.iter().any(|o| o["status"] == "FILLED"));
    }
}
