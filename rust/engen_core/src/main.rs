//! Paper-only binary entry. No network, no keys, no broadcast.

use engen_core::paper_simulator::PaperSimulator;

fn main() {
    println!("engen_paper — Rust paper core scaffold");
    println!("real_trading=false transaction_broadcast=false");
    let mut sim = PaperSimulator::new();
    sim.set_virtual_balance("100", "USD").expect("balance");
    let report = sim.report();
    println!("{}", serde_json::to_string_pretty(&report).unwrap());
}
