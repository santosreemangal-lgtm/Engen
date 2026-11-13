//! Durable, read-only wallet-signature discovery with baseline and cursor recovery.

use std::collections::{BTreeMap, HashSet};

use serde_json::Value;
use thiserror::Error;

use crate::latency::LatencyRecorder;
use crate::monitor_scheduler::MAX_FRESH_BATCH;
use crate::signature_store::{SignatureStore, SignatureStoreError};
use crate::solana_public::{SolanaRpcClient, SolanaRpcError};
use crate::solana_validation::{is_valid_solana_address, is_valid_transaction_signature};

const MAX_SCAN_PAGES: usize = MAX_BACKFILL_SIGNATURES;
const MAX_PAGE_SIZE: usize = 1000;
const MAX_BACKFILL_SIGNATURES: usize = 1_000;

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
pub fn filter_new_signatures(signatures: &[Value], seen_signatures: &[String]) -> Vec<Value> {
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
    let signatures = get_wallet_signatures(client, wallet_address, limit, None, latency_recorder)?;
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
    let cursor = signatures
        .iter()
        .rev()
        .filter_map(|item| item.get("signature").and_then(Value::as_str))
        .find(|signature| is_valid_transaction_signature(signature));
    store.start_history_backfill(wallet_address, cursor)?;
    Ok(())
}

/// Scan newest-first until the last processed anchor is found, capped at 1,000 unique signatures.
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
        let previous_unique_count = records_by_signature.len();
        let lane = if pages == 1 { "fresh" } else { "history" };
        let mut anchor_found = false;
        for item in &page {
            let Some(signature) = item.get("signature").and_then(Value::as_str) else {
                continue;
            };
            if !is_valid_transaction_signature(signature) {
                continue;
            }
            if records_by_signature.len() >= MAX_BACKFILL_SIGNATURES {
                break;
            }
            let slot = item.get("slot").and_then(Value::as_u64);
            store.save_in_lane(wallet_address, signature, slot, lane, Some(item))?;
            records_by_signature.insert(signature.to_string(), item.clone());
            if anchor_signature.as_deref() == Some(signature) {
                anchor_found = true;
                break;
            }
        }

        if anchor_found
            || records_by_signature.len() >= MAX_BACKFILL_SIGNATURES
            || page.len() < limit
            || pages >= MAX_SCAN_PAGES
            || page.is_empty()
            || records_by_signature.len() == previous_unique_count
        {
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

/// Resume the one-time History backfill from its persisted cursor.
/// A fetched page is committed atomically at the page boundary; on RPC failure,
/// the cursor stays unchanged and the next process can safely retry that page.
fn resume_history_backfill_with<F>(
    store: &SignatureStore,
    wallet_address: &str,
    mut fetch_page: F,
) -> Result<usize, WalletMonitorError>
where
    F: FnMut(usize, Option<&str>) -> Result<Vec<Value>, WalletMonitorError>,
{
    let Some((cursor, already_scanned, completed)) =
        store.history_backfill_state(wallet_address)?
    else {
        return Ok(0);
    };
    if completed || already_scanned >= MAX_BACKFILL_SIGNATURES {
        return Ok(0);
    }

    let remaining = MAX_BACKFILL_SIGNATURES - already_scanned;
    let page_limit = remaining.min(MAX_PAGE_SIZE);
    let page = fetch_page(page_limit, cursor.as_deref())?;
    let mut seen = HashSet::new();
    let mut scanned = 0usize;
    let mut next_cursor = cursor.clone();

    for item in &page {
        let Some(signature) = item.get("signature").and_then(Value::as_str) else {
            continue;
        };
        if !is_valid_transaction_signature(signature) || !seen.insert(signature.to_string()) {
            continue;
        }
        store.save_in_lane(
            wallet_address,
            signature,
            item.get("slot").and_then(Value::as_u64),
            "history",
            Some(item),
        )?;
        scanned += 1;
        next_cursor = Some(signature.to_string());
    }

    let completed = page.is_empty()
        || page.len() < page_limit
        || scanned == 0
        || already_scanned.saturating_add(scanned) >= MAX_BACKFILL_SIGNATURES;
    store.advance_history_backfill(wallet_address, next_cursor.as_deref(), scanned, completed)?;
    Ok(scanned)
}

fn resume_history_backfill(
    client: &SolanaRpcClient,
    store: &SignatureStore,
    wallet_address: &str,
    latency_recorder: Option<&LatencyRecorder>,
) -> Result<usize, WalletMonitorError> {
    resume_history_backfill_with(store, wallet_address, |limit, before| {
        get_wallet_signatures(client, wallet_address, limit, before, latency_recorder)
    })
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
    // Fresh discovery never exceeds the approved per-cycle cap; History is backfilled
    // separately from a durable cursor and processed in its own 10-25 item lane.
    let fresh_limit = limit.min(MAX_FRESH_BATCH);
    let first_page =
        get_wallet_signatures(client, wallet_address, fresh_limit, None, latency_recorder)?;
    let history_cursor = first_page
        .iter()
        .rev()
        .filter_map(|item| item.get("signature").and_then(Value::as_str))
        .find(|signature| is_valid_transaction_signature(signature))
        .map(str::to_string);

    if !store.has_baseline(wallet_address)? {
        initialize_wallet_baseline(store, wallet_address, &first_page)?;
        resume_history_backfill(client, store, wallet_address, latency_recorder)?;
        return Ok(Vec::new());
    }

    let discovered = scan_existing_wallet_pages(
        store,
        wallet_address,
        first_page,
        fresh_limit,
        |wallet, page_limit, before| {
            get_wallet_signatures(client, wallet, page_limit, before, latency_recorder)
        },
    )?;

    if store.history_backfill_state(wallet_address)?.is_none() {
        store.start_history_backfill(wallet_address, history_cursor.as_deref())?;
    }
    resume_history_backfill(client, store, wallet_address, latency_recorder)?;
    Ok(discovered)
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
        (
            directory,
            SignatureStore::open(database).expect("signature store"),
        )
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
        assert_eq!(
            store.history_backfill_state(WALLET).unwrap(),
            Some((Some(NEW_A.to_string()), 0, false))
        );
        assert_eq!(
            store.get_status(WALLET, ANCHOR).unwrap().as_deref(),
            Some("processed")
        );
        assert_eq!(
            store.get_status(WALLET, NEW_A).unwrap().as_deref(),
            Some("processed")
        );
        assert_eq!(
            store.pending_signatures(WALLET, 10).unwrap(),
            Vec::<String>::new()
        );
    }

    #[test]
    fn history_backfill_persists_cursor_and_observations_for_restart_recovery() {
        let (_directory, store) = store();
        store.start_history_backfill(WALLET, Some(ANCHOR)).unwrap();
        let mut requested = Vec::new();
        let scanned = resume_history_backfill_with(&store, WALLET, |limit, before| {
            requested.push((limit, before.map(str::to_string)));
            Ok(vec![
                serde_json::json!({
                    "signature": NEW_A,
                    "slot": 98,
                    "blockTime": 1_760_000_000,
                    "confirmationStatus": "finalized"
                }),
                serde_json::json!({
                    "signature": NEW_B,
                    "slot": 97,
                    "blockTime": 1_759_999_999,
                    "confirmationStatus": "confirmed"
                }),
            ])
        })
        .unwrap();
        assert_eq!(scanned, 2);
        assert_eq!(requested, vec![(1_000, Some(ANCHOR.to_string()))]);
        assert_eq!(
            store.history_backfill_state(WALLET).unwrap(),
            Some((Some(NEW_B.to_string()), 2, true))
        );
        assert_eq!(
            store
                .pending_signatures_for_lane(WALLET, "history", 10)
                .unwrap(),
            vec![NEW_A.to_string(), NEW_B.to_string()]
        );

        let observations = store
            .pending_signature_observations(WALLET, "history", 10)
            .unwrap();
        assert_eq!(observations[0]["blockTime"], 1_760_000_000);
        assert_eq!(observations[1]["confirmationStatus"], "confirmed");
    }

    #[test]
    fn failed_history_page_leaves_the_persisted_cursor_unchanged_for_retry() {
        let (_directory, store) = store();
        store.start_history_backfill(WALLET, Some(ANCHOR)).unwrap();
        let error = resume_history_backfill_with(&store, WALLET, |_limit, _before| {
            Err(WalletMonitorError::InvalidInput(
                "simulated provider interruption".to_string(),
            ))
        });
        assert!(error.is_err());
        assert_eq!(
            store.history_backfill_state(WALLET).unwrap(),
            Some((Some(ANCHOR.to_string()), 0, false))
        );
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
        let result =
            scan_existing_wallet_pages(&store, WALLET, first, 2, |wallet, limit, before| {
                assert_eq!(wallet, WALLET);
                assert_eq!(limit, 2);
                calls.push(before.map(str::to_string));
                Ok(vec![
                    serde_json::json!({"signature":ANCHOR,"slot":100}),
                    serde_json::json!({"signature":OLDER,"slot":99}),
                ])
            })
            .unwrap();
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
        let result =
            scan_existing_wallet_pages(&store, WALLET, first, 2, |_wallet, _limit, _before| {
                Err(WalletMonitorError::InvalidInput(
                    "simulated RPC failure".to_string(),
                ))
            });
        assert!(result.is_err());
        assert_eq!(
            store.get_status(WALLET, NEW_A).unwrap().as_deref(),
            Some("pending")
        );
        assert_eq!(
            store.get_status(WALLET, NEW_B).unwrap().as_deref(),
            Some("pending")
        );
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
                serde_json::json!({"signature":OLDER,"slot":89-calls as u64}),
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
