//! Opt-in read-only smoke test against the public Solana mainnet RPC.
//! It performs only getHealth/getSlot; no wallet credentials or write methods are used.

use std::time::Duration;

use engen_engine::solana_public::{RpcConfig, SolanaRpcClient};

#[test]
fn public_mainnet_rpc_health_and_slot_are_readable() {
    if std::env::var("ENGEN_PUBLIC_RPC_INTEGRATION").as_deref() != Ok("1") {
        eprintln!("Skipping public RPC smoke test; ENGEN_PUBLIC_RPC_INTEGRATION is not set.");
        return;
    }

    let endpoint = std::env::var("ENGEN_PUBLIC_RPC_URL")
        .unwrap_or_else(|_| "https://api.mainnet-beta.solana.com".to_string());
    let client = SolanaRpcClient::new(RpcConfig {
        endpoint,
        timeout: Duration::from_secs(20),
        retries: 2,
        backoff: Duration::from_millis(500),
        min_interval: Duration::ZERO,
    })
    .expect("construct bounded read-only RPC client");

    let health = client.get_health().expect("public RPC getHealth");
    assert_eq!(health, "ok", "RPC health response must be ok");

    let slot = client.get_slot().expect("public RPC getSlot");
    assert!(slot > 0, "mainnet slot must be positive");
}
