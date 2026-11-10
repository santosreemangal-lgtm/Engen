//! Paper-only binary. No network keys, no broadcast.

use engen_core::paper_copy::{queue_paper_orders, MonitorCandidate};
use engen_core::paper_simulator::{MarketPrice, OrderSide, PaperSimulator};
use rust_decimal::Decimal;
use std::time::{SystemTime, UNIX_EPOCH};

fn main() {
    println!("engen_paper — Rust paper core");
    println!("real_trading=false transaction_broadcast=false");

    let mut sim = PaperSimulator::new();
    sim.set_virtual_balance("100", "USD").expect("balance");

    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0);

    let candidates = vec![MonitorCandidate {
        signature: "demoSig111".into(),
        wallet: "DemoWallet111111111111111111111111111".into(),
        side: OrderSide::Buy,
        asset_mint: "DemoTokenMint11111111111111111111111".into(),
        block_time: Some(now - 5),
        confirmation_status: "finalized".into(),
        metadata_complete: true,
        target_notional_usd: Some("10".into()),
        source_sell_fraction: None,
    }];

    let queued = queue_paper_orders(&mut sim, &candidates).expect("queue");
    println!("queued={}", queued.len());

    if let Some(order) = queued.first() {
        let market = MarketPrice {
            price_usd: Decimal::from(2),
            liquidity_usd: Decimal::from(50_000),
            age_seconds: 1,
        };
        let fill = sim
            .execute_order(&order.order_id, &market, 50, 10, Decimal::from(10_000), 60)
            .expect("fill");
        println!("fill={}", fill);
    }

    println!("{}", serde_json::to_string_pretty(&sim.report()).unwrap());
}
