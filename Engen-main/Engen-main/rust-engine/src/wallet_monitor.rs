//! Durable, read-only wallet-signature discovery with baseline and cursor recovery.

use std::collections::{BTreeMap, HashSet};

use serde_json::Value;
use thiserror::Error;

use crate::latency::LatencyRecorder;
use crate::signature_store::{SignatureStore, SignatureStoreError};
use crate::solana_public::{SolanaRpcClient, SolanaRpcError};
use crate::solana_validation::{is_valid_solana_address, is_valid_transaction_signature};

const MAX_SCAN_PAGES: usize = 5;
const MAX_PAGE_SIZE: usize = 1000;

#[derive(Debug, Error)]
pub enum WalletMonitorError {
    #[error("Public Solana RPC request failed.")]
    Rpc(#[from] SolanaRpcError),
    #[error("Signature-store operation failed.")]
    Store(#[from] SignatureStoreError),
    #[error("Invalid wallet-monitor input: {0}")]
    InvalidInput(String),
}

fn validate_page_size(limit: usize) -> Result<(), WalletMonitorError> {
    if !(1..=MAX_PAGE_SIZE).contains(&limit) {
        return Err(WalletMonitorError::InvalidInput(
            "Signature-page limit must be between 1 and 1000.".to_string(),
        ));
    }
    Ok(())
}

/// Fetch one bounded page and discard malformed provider rows.
pub fn get_wallet_signatures(
    client: &SolanaRpcClient,
    wallet_address: &str,
    limit: usize,
    before: Option<&str>,
    latency_recorder: Option<&LatencyRecorder>,
) -> Result<Vec<Value>, WalletMonitorError> {
    if !is_valid_solana_address(wallet_address) {
        return Err(WalletMonitorError::InvalidInput(
            "Invalid Solana public wallet address.".to_string(),
        ));
    }
    validate_page_size(limit)?;

    let started = std::time::Instant::now();
    let result = client.get_signatures_for_address(wallet_address, limit, before);
    if let Some(recorder) = latency_recorder {
        let elapsed_ms = started.elapsed().as_secs_f64() * 1_000.0;
        let _ = recorder.record("wallet.signature_fetch", elapsed_ms);
    }
    Ok(result?)
}

/// Return unseen valid rows while preserving provider order and row metadata.
pub fn filter_new_signatures(
    signatures: &[Value],
    seen_signatures: &[String],
) -> Vec<Value> {
    let mut seen: HashSet<String> = seen_signatures.iter().cloned().collect();
    let mut new_rows = Vec::new();
    for item in signatures {
        let Some(signature) = item.get("signature").and_then(Value::as_str) else {
            continue;
        };
        if !is_valid_transaction_signature(signature) || !seen.insert(signature.to_string()) {
            continue;
        }
        new_rows.push(item.clone());
    }
    new_rows
}

pub fn get_new_wallet_signatures(
    client: &SolanaRpcClient,
    wallet_address: &str,
    seen_signatures: &[String],
    limit: usize,
    latency_recorder: Option<&LatencyRecorder>,
) -> Result<Vec<Value>, WalletMonitorError> {
    let signatures = get_wallet_signatures(
        client,
        wallet_address,
        limit,
        None,
        latency_recorder,
    )?;
    Ok(filter_new_signatures(&signatures, seen_signatures))
}

/// Persist the first observation as a baseline only; do not return trade signals.
pub fn initialize_wallet_baseline(
    store: &SignatureStore,
    wallet_address: &str,
    signatures: &[Value],
) -> Result<(), WalletMonitorError> {
    if !is_valid_solana_address(wallet_address) {
        return Err(WalletMonitorError::InvalidInput(
            "Invalid Solana public wallet address.".to_string(),
        ));
    }
    for item in signatures {
        let Some(signature) = item.get("signature").and_then(Value::as_str) else {
            continue;
        };
        if !is_valid_transaction_signature(signature) {
            continue;
        }
        let slot = item.get("slot").and_then(Value::as_u64);
        store.save(wallet_address, signature, slot)?;
        store.mark_processed(wallet_address, signature)?;
    }
    store.mark_baseline(wallet_address)?;
    Ok(())
}

/// Scan newest-first until the last processed anchor is found (up to five pages).
///
/// Every accepted signature is persisted before the next RPC call. If a later page
/// fails, the saved pending queue remains durable for the monitoring worker to drain.
pub fn scan_existing_wallet_pages<F>(
    store: &SignatureStore,
    wallet_address: &str,
    first_page: Vec<Value>,
    limit: usize,
    mut fetch_next_page: F,
) -> Result<Vec<Value>, WalletMonitorError>
where
    F: FnMut(&str, usize, Option<&str>) -> Result<Vec<Value>, WalletMonitorError>,
{
    if !is_valid_solana_address(wallet_address) {
        return Err(WalletMonitorError::InvalidInput(
            "Invalid Solana public wallet address.".to_string(),
        ));
    }
    validate_page_size(limit)?;
    let position = store.get_position(wallet_address)?;
    let anchor_signature = position.and_then(|value| value.signature);
    let mut records_by_signature = BTreeMap::new();
    let mut page = first_page;
    let mut pages = 0usize;

    loop {
        pages += 1;
        let mut anchor_found = false;
        for item in &page {
            let Some(signature) = item.get("signature").and_then(Value::as_str) else {
                continue;
            };
            if !is_valid_transaction_signature(signature) {
                continue;
            }
            let slot = item.get("slot").and_then(Value::as_u64);
            store.save(wallet_address, signature, slot)?;
            records_by_signature.insert(signature.to_string(), item.clone());
            if anchor_signature.as_deref() == Some(signature) {
                anchor_found = true;
                break;
            }
        }

        if anchor_found || page.len() < limit || pages >= MAX_SCAN_PAGES || page.is_empty() {
            break;
        }

        let Some(before) = page
            .last()
            .and_then(|item| item.get("signature"))
            .and_then(Value::as_str)
            .filter(|signature| is_valid_transaction_signature(signature))
            .map(str::to_string)
        else {
            break;
        };
        page = fetch_next_page(wallet_address, limit, Some(&before))?;
    }

    let pending = store.pending_signatures(wallet_address, limit)?;
    Ok(pending
        .into_iter()
        .map(|signature| {
            records_by_signature
                .get(&signature)
                .cloned()
                .unwrap_or_else(|| serde_json::json!({"signature":signature}))
        })
        .collect())
}

/// Full wallet scan: baseline on first observation, then bounded cursor recovery.
pub fn get_and_store_new_wallet_signatures(
    client: &SolanaRpcClient,
    store: &SignatureStore,
    wallet_address: &str,
    limit: usize,
    latency_recorder: Option<&LatencyRecorder>,
) -> Result<Vec<Value>, WalletMonitorError> {
    if !is_valid_solana_address(wallet_address) {
        return Err(WalletMonitorError::InvalidInput(
            "Invalid Solana public wallet address.".to_string(),
        ));
    }
    validate_page_size(limit)?;
    let first_page = get_wallet_signatures(
        client,
        wallet_address,
        limit,
        None,
        latency_recorder,
    )?;

    if !store.has_baseline(wallet_address)? {
        initialize_wallet_baseline(store, wallet_address, &first_page)?;
        return Ok(Vec::new());
    }

    scan_existing_wallet_pages(store, wallet_address, first_page, limit, |wallet, page_limit, before| {
        get_wallet_signatures(client, wallet, page_limit, before, latency_recorder)
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    const WALLET: &str = "11111111111111111111111111111111";
    const ANCHOR: &str = "1111111111111111111111111111111111111111111111111111111111111112";
    const NEW_A: &str = "1111111111111111111111111111111111111111111111111111111111111113";
    const NEW_B: &str = "1111111111111111111111111111111111111111111111111111111111111114";
    const OLDER: &str = "1111111111111111111111111111111111111111111111111111111111111115";

    fn store() -> (tempfile::TempDir, SignatureStore) {
        let directory = tempdir().expect("temporary directory");
        let database = directory.path().join("monitor.db");
        (directory, SignatureStore::open(database).expect("signature store"))
    }

    #[test]
    fn filters_seen_and_duplicate_signatures_without_losing_metadata() {
        let rows = vec![
            serde_json::json!({"signature":NEW_A,"slot":102,"blockTime":123}),
            serde_json::json!({"signature":NEW_A,"slot":102}),
            serde_json::json!({"signature":ANCHOR,"slot":100}),
            serde_json::json!({"signature":"bad"}),
        ];
        let result = filter_new_signatures(&rows, &[ANCHOR.to_string()]);
        assert_eq!(result.len(), 1);
        assert_eq!(result[0]["signature"], NEW_A);
        assert_eq!(result[0]["blockTime"], 123);
    }

    #[test]
    fn first_observation_is_saved_as_processed_baseline_only() {
        let (_directory, store) = store();
        let rows = vec![
            serde_json::json!({"signature":ANCHOR,"slot":100}),
            serde_json::json!({"signature":NEW_A,"slot":99}),
        ];
        initialize_wallet_baseline(&store, WALLET, &rows).unwrap();
        assert!(store.has_baseline(WALLET).unwrap());
        assert_eq!(store.get_status(WALLET, ANCHOR).unwrap().as_deref(), Some("processed"));
        assert_eq!(store.get_status(WALLET, NEW_A).unwrap().as_deref(), Some("processed"));
        assert_eq!(store.pending_signatures(WALLET, 10).unwrap(), Vec::<String>::new());
    }

    #[test]
    fn scans_back_to_processed_anchor_and_never_saves_older_history() {
        let (_directory, store) = store();
        store.mark_baseline(WALLET).unwrap();
        store.save(WALLET, ANCHOR, Some(100)).unwrap();
        store.mark_processed(WALLET, ANCHOR).unwrap();
        let first = vec![
            serde_json::json!({"signature":NEW_A,"slot":102}),
            serde_json::json!({"signature":NEW_B,"slot":101}),
        ];
        let mut calls = Vec::new();
        let result = scan_existing_wallet_pages(&store, WALLET, first, 2, |wallet, limit, before| {
            assert_eq!(wallet, WALLET);
            assert_eq!(limit, 2);
            calls.push(before.map(str::to_string));
            Ok(vec![
                serde_json::json!({"signature":ANCHOR,"slot":100}),
                serde_json::json!({"signature":OLDER,"slot":99}),
            ])
        }).unwrap();
        let signatures: HashSet<_> = result
            .iter()
            .filter_map(|item| item.get("signature").and_then(Value::as_str))
            .collect();
        assert!(signatures.contains(NEW_A));
        assert!(signatures.contains(NEW_B));
        assert_eq!(calls, vec![Some(NEW_B.to_string())]);
        assert!(!store.has_seen(WALLET, OLDER).unwrap());
    }

    #[test]
    fn partial_pages_are_durable_when_a_later_rpc_page_fails() {
        let (_directory, store) = store();
        store.mark_baseline(WALLET).unwrap();
        store.save(WALLET, ANCHOR, Some(100)).unwrap();
        store.mark_processed(WALLET, ANCHOR).unwrap();
        let first = vec![
            serde_json::json!({"signature":NEW_A,"slot":102}),
            serde_json::json!({"signature":NEW_B,"slot":101}),
        ];
        let result = scan_existing_wallet_pages(&store, WALLET, first, 2, |_wallet, _limit, _before| {
            Err(WalletMonitorError::InvalidInput("simulated RPC failure".to_string()))
        });
        assert!(result.is_err());
        assert_eq!(store.get_status(WALLET, NEW_A).unwrap().as_deref(), Some("pending"));
        assert_eq!(store.get_status(WALLET, NEW_B).unwrap().as_deref(), Some("pending"));
    }

    #[test]
    fn scan_stops_after_five_pages_even_without_an_anchor() {
        let (_directory, store) = store();
        store.mark_baseline(WALLET).unwrap();
        let first = vec![
            serde_json::json!({"signature":NEW_A,"slot":102}),
            serde_json::json!({"signature":NEW_B,"slot":101}),
        ];
        let mut calls = 0;
        let _ = scan_existing_wallet_pages(&store, WALLET, first, 2, |_wallet, _limit, _before| {
            calls += 1;
            let next = if calls % 2 == 0 { NEW_A } else { NEW_B };
            Ok(vec![
                serde_json::json!({"signature":next,"slot":90-calls as u64}),
                serde_json::json!({"signature":OLDER,"slot":89-calls as u64})
            ])
        });
        assert!(calls <= 4);
    }

    #[test]
    fn rejects_invalid_wallet_and_page_size() {
        assert!(validate_page_size(0).is_err());
        assert!(validate_page_size(1001).is_err());
        assert!(!is_valid_solana_address("not-a-wallet"));
    }
}
