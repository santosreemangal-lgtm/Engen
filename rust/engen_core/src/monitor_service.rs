//! Maps to dashboard/monitor_service.py + monitor_bridge.py (paper loop).

use crate::paper_copy::{queue_paper_orders, MonitorCandidate};
use crate::paper_simulator::PaperSimulator;
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
        self.monitors.push(TransactionMonitor::new(wallet));
    }

    /// One scan tick: poll → queue paper orders.
    pub fn tick(&self, sim: &mut PaperSimulator) -> Result<usize> {
        let mut all: Vec<MonitorCandidate> = Vec::new();
        for m in &self.monitors {
            all.extend(m.poll_candidates()?);
        }
        let queued = queue_paper_orders(sim, &all)?;
        Ok(queued.len())
    }
}

impl Default for MonitorService {
    fn default() -> Self {
        Self::new()
    }
}
