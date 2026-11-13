//! End-to-end read-only transaction monitoring and conservative signal preparation.

use std::collections::HashSet;
use std::sync::atomic::{AtomicU64, Ordering};

use serde_json::{json, Value};
use thiserror::Error;

use crate::latency::LatencyRecorder;
use crate::monitor_scheduler::{ScanPlan, ScanStage, DEFAULT_HISTORY_BATCH, MAX_FRESH_BATCH};
use crate::signature_store::{ProcessingStage, SignatureStore, SignatureStoreError};
use crate::solana_public::{SolanaRpcClient, SolanaRpcError};
use crate::solana_validation::{is_valid_solana_address, is_valid_transaction_signature};
use crate::source_freshness::source_block_time_ok;
use crate::transaction_analysis::classify_transaction;
use crate::transaction_processor::{process_transaction_signature, TransactionProcessorError};
use crate::wallet_monitor::{
    get_and_store_new_wallet_signatures, resume_history_backfill, WalletMonitorError,
};

const MAX_PROCESSING_ATTEMPTS: u32 = 6;
const SIGNATURE_CLAIM_LEASE_SECONDS: u64 = 120;
static CLAIM_SEQUENCE: AtomicU64 = AtomicU64::new(1);

fn fresh_claim_owner() -> String {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    let sequence = CLAIM_SEQUENCE.fetch_add(1, Ordering::Relaxed);
    format!("monitor-{now}-{sequence}")
}

fn first_signature_occurrence(signature: &str, seen: &mut HashSet<String>) -> bool {
    seen.insert(signature.to_string())
}

fn is_terminal_signature_status(status: Option<&str>) -> bool {
    matches!(status, Some("processed" | "stale" | "needs_review"))
}

#[derive(Debug, Error)]
pub enum TransactionMonitorError {
    #[error("Invalid transaction-monitor input: {0}")]
    InvalidInput(String),
    #[error("Wallet scan failed.")]
    Wallet(#[from] WalletMonitorError),
    #[error("Signature-store operation failed.")]
    Store(#[from] SignatureStoreError),
}

/// Build signature-list observation data separately from transaction evidence.
fn include_signature_observation(mut result: Value, item: &Value) -> Value {
    let observation = json!({
        "slot": item.get("slot").cloned().unwrap_or(Value::Null),
        "confirmation_status": item.get("confirmationStatus").cloned().unwrap_or(Value::Null),
        "signature_error": item.get("err").cloned().unwrap_or(Value::Null),
        "block_time": item.get("blockTime").cloned().unwrap_or(Value::Null)
    });
    if let Some(object) = result.as_object_mut() {
        object.insert("signature_observation".to_string(), observation);
    }
    result
}

fn unknown_signal(reason: &str) -> Value {
    json!({
        "classification":"UNKNOWN",
        "reason":reason,
        "asset_mints":[],
        "quote_mints":[],
        "confidence":"low",
        "evidence":{
            "classification_basis":"signer ownership or confirmation could not be established",
            "automatic_trade_allowed":false
        }
    })
}

/// Attach an authoritative status lookup if the signature page omitted finality.
fn merge_signature_status(item: &Value, resolved: Option<&Value>) -> Value {
    let mut merged = item.as_object().cloned().unwrap_or_default();
    if let Some(status) = resolved.filter(|value| value.is_object()) {
        if let Some(value) = status.get("confirmationStatus") {
            merged.insert("confirmationStatus".to_string(), value.clone());
        }
        if merged.get("slot").is_none_or(Value::is_null) {
            if let Some(value) = status.get("slot") {
                merged.insert("slot".to_string(), value.clone());
            }
        }
        if merged.get("err").is_none_or(Value::is_null) {
            if let Some(value) = status.get("err") {
                merged.insert("err".to_string(), value.clone());
            }
        }
    }
    Value::Object(merged)
}

/// Apply signer, confirmation, and wallet-owner checks before exposing a copy candidate.
///
/// The signature-page observation is not itself transaction proof. Only transaction
/// metadata and wallet-owned balance changes are passed to the conservative classifier.
pub fn prepare_wallet_transaction_result(
    wallet_address: &str,
    signature_item: &Value,
    mut result: Value,
    resolved_status: Option<&Value>,
) -> Value {
    let item = merge_signature_status(signature_item, resolved_status);
    if let Some(object) = result.as_object_mut() {
        if let Some(raw) = object.get_mut("token_balance_changes") {
            if let Some(changes) = raw.as_array_mut() {
                changes.retain(|change| {
                    change.get("owner").and_then(Value::as_str) == Some(wallet_address)
                });
            }
        }
    }
    result = include_signature_observation(result, &item);

    let status = result.get("status").and_then(Value::as_str);
    let raw_transaction = result
        .get("raw_transaction")
        .cloned()
        .unwrap_or(Value::Null);
    let changes = result
        .get("token_balance_changes")
        .cloned()
        .unwrap_or_else(|| Value::Array(Vec::new()));
    let confirmation = item.get("confirmationStatus").and_then(Value::as_str);
    let signers = result.get("signers").and_then(Value::as_array);
    let wallet_is_signer = signers.is_some_and(|values| {
        values
            .iter()
            .any(|value| value.as_str() == Some(wallet_address))
    });
    let signature_error = item.get("err").is_some_and(|value| !value.is_null());

    let signal = if status == Some("failed") {
        classify_transaction(&raw_transaction, wallet_address, Some(&changes))
    } else if status == Some("success") && signature_error {
        unknown_signal(
            "Signature status reports a transaction error; no buy/sell candidate was created.",
        )
    } else if status == Some("success") && !matches!(confirmation, Some("confirmed" | "finalized"))
    {
        unknown_signal(
            "Transaction confirmation is insufficient; no buy/sell candidate was created.",
        )
    } else if status == Some("success") && !wallet_is_signer {
        unknown_signal("The monitored wallet is not confirmed as a transaction signer.")
    } else if status == Some("success") {
        classify_transaction(&raw_transaction, wallet_address, Some(&changes))
    } else {
        unknown_signal("Transaction status is unavailable; no trade candidate was created.")
    };

    let retryable = status != Some("failed")
        && (signature_error
            || !matches!(confirmation, Some("confirmed" | "finalized"))
            || result.get("available").and_then(Value::as_bool) != Some(true));
    if let Some(object) = result.as_object_mut() {
        object.insert("trade_signal".to_string(), signal);
        object.insert(
            "confirmation_sufficient".to_string(),
            Value::Bool(matches!(confirmation, Some("confirmed" | "finalized"))),
        );
        if retryable {
            object.insert("retryable".to_string(), Value::Bool(true));
        }
    }
    result
}

fn record_retry(
    store: &SignatureStore,
    wallet_address: &str,
    signature: &str,
    stage: ProcessingStage,
    code: &str,
    message: &str,
    raw_transaction: Option<&Value>,
) -> Result<String, SignatureStoreError> {
    let failure = store.record_failure(
        wallet_address,
        signature,
        stage,
        Some(code),
        Some(message),
        MAX_PROCESSING_ATTEMPTS,
    )?;
    if let Some(raw) = raw_transaction {
        let status = if failure.status == "needs_review" {
            "needs_review"
        } else {
            "incomplete"
        };
        store.save_transaction_record(wallet_address, signature, Some(raw), status)?;
    }
    Ok(failure.status)
}

/// Process a bounded set of saved signature observations.
/// Backward-compatible Fresh-lane processing entrypoint.
pub fn process_new_signatures(
    client: &SolanaRpcClient,
    store: &SignatureStore,
    wallet_address: &str,
    signatures: &[Value],
    defer_processed: bool,
    latency_recorder: Option<&LatencyRecorder>,
) -> Result<Vec<Value>, TransactionMonitorError> {
    process_signatures_for_lane(
        client,
        store,
        wallet_address,
        signatures,
        "fresh",
        defer_processed,
        latency_recorder,
    )
}

pub fn process_signatures_for_lane(
    client: &SolanaRpcClient,
    store: &SignatureStore,
    wallet_address: &str,
    signatures: &[Value],
    source_lane: &str,
    defer_processed: bool,
    latency_recorder: Option<&LatencyRecorder>,
) -> Result<Vec<Value>, TransactionMonitorError> {
    if !is_valid_solana_address(wallet_address) {
        return Err(TransactionMonitorError::InvalidInput(
            "Invalid Solana public wallet address.".to_string(),
        ));
    }

    if !matches!(source_lane, "fresh" | "history") {
        return Err(TransactionMonitorError::InvalidInput(
            "Signature source lane must be fresh or history.".to_string(),
        ));
    }

    let mut results = Vec::new();
    let mut seen_in_batch = HashSet::new();
    for item in signatures {
        let Some(signature) = item.get("signature").and_then(Value::as_str) else {
            continue;
        };
        if !first_signature_occurrence(signature, &mut seen_in_batch) {
            continue;
        }
        if !is_valid_transaction_signature(signature) {
            let invalid = json!({
                "signature":signature,
                "available":false,
                "metadata_complete":false,
                "metadata_status":"invalid_signature",
                "retryable":false,
                "error":"Invalid transaction signature; RPC request was not sent."
            });
            results.push(include_signature_observation(invalid, item));
            continue;
        }
        if is_terminal_signature_status(store.get_status(wallet_address, signature)?.as_deref()) {
            continue;
        }

        let claim_owner = fresh_claim_owner();
        if !store.claim_signature(
            wallet_address,
            signature,
            &claim_owner,
            chrono::Utc::now().timestamp(),
            SIGNATURE_CLAIM_LEASE_SECONDS,
        )? {
            continue;
        }

        let processed = match process_transaction_signature(client, signature, latency_recorder) {
            Ok(value) => value,
            Err(TransactionProcessorError::Rpc(error)) => {
                let error_code = match &error {
                    SolanaRpcError::Http(429) => "http_429",
                    SolanaRpcError::Http(_) => "http_error",
                    SolanaRpcError::Provider(_) => "rpc_provider_error",
                    SolanaRpcError::UnsupportedTransactionVersion => {
                        "unsupported_transaction_version"
                    }
                    SolanaRpcError::Transport => "rpc_transport_error",
                    SolanaRpcError::InvalidResponse => "rpc_invalid_response",
                    SolanaRpcError::ResponseTooLarge => "rpc_response_too_large",
                    SolanaRpcError::InvalidInput(_) => "rpc_invalid_input",
                };
                let message = error.to_string();
                let unsupported_version =
                    matches!(error, SolanaRpcError::UnsupportedTransactionVersion);
                let processing_status = if unsupported_version {
                    let failure = store.record_failure(
                        wallet_address,
                        signature,
                        ProcessingStage::RpcFetch,
                        Some(error_code),
                        Some(&message),
                        1,
                    )?;
                    store.save_transaction_record(
                        wallet_address,
                        signature,
                        None,
                        "needs_review",
                    )?;
                    failure.status
                } else {
                    record_retry(
                        store,
                        wallet_address,
                        signature,
                        ProcessingStage::RpcFetch,
                        error_code,
                        &message,
                        None,
                    )?
                };
                let error_result = json!({
                    "signature":signature,
                    "available":false,
                    "metadata_complete":false,
                    "metadata_status":if unsupported_version {
                        "unsupported_transaction_version"
                    } else {
                        "rpc_error"
                    },
                    "error":message,
                    "retryable":!unsupported_version && processing_status == "pending",
                    "processing_status":processing_status
                });
                results.push(include_signature_observation(error_result, item));
                continue;
            }
            Err(TransactionProcessorError::InvalidInput) => {
                let invalid = json!({
                    "signature":signature,
                    "available":false,
                    "metadata_complete":false,
                    "metadata_status":"invalid_signature",
                    "retryable":false,
                    "error":"Transaction signature or payload is invalid."
                });
                store.release_signature_claim(wallet_address, signature, &claim_owner)?;
                results.push(include_signature_observation(invalid, item));
                continue;
            }
            Err(TransactionProcessorError::Analysis) => {
                let processing_status = record_retry(
                    store,
                    wallet_address,
                    signature,
                    ProcessingStage::Interpretation,
                    "interpretation_error",
                    "Transaction interpretation failed; retry scheduled.",
                    None,
                )?;
                let error_result = json!({
                    "signature":signature,
                    "available":false,
                    "metadata_complete":false,
                    "metadata_status":"interpretation_error",
                    "error":"Transaction interpretation failed; retry scheduled.",
                    "retryable":processing_status == "pending",
                    "processing_status":processing_status
                });
                results.push(include_signature_observation(error_result, item));
                continue;
            }
        };

        let available = processed.get("available").and_then(Value::as_bool) == Some(true);
        let metadata_complete =
            processed.get("metadata_complete").and_then(Value::as_bool) == Some(true);
        if !available || !metadata_complete {
            let code = processed
                .get("metadata_status")
                .and_then(Value::as_str)
                .unwrap_or("incomplete_metadata");
            let message = processed
                .get("error")
                .and_then(Value::as_str)
                .unwrap_or("Transaction data is incomplete.");
            let raw = processed.get("raw_transaction");
            let processing_status = record_retry(
                store,
                wallet_address,
                signature,
                ProcessingStage::RpcFetch,
                code,
                message,
                raw,
            )?;
            let mut pending = processed;
            if let Some(object) = pending.as_object_mut() {
                object.insert(
                    "retryable".to_string(),
                    Value::Bool(processing_status == "pending"),
                );
                object.insert(
                    "processing_status".to_string(),
                    Value::String(processing_status),
                );
            }
            results.push(include_signature_observation(pending, item));
            continue;
        }

        let mut resolved = None;
        if !matches!(
            item.get("confirmationStatus").and_then(Value::as_str),
            Some("confirmed" | "finalized")
        ) && processed.get("status").and_then(Value::as_str) == Some("success")
        {
            resolved = client.get_signature_status(signature).ok().flatten();
        }

        let mut observation_item = item.clone();
        let mut result = prepare_wallet_transaction_result(
            wallet_address,
            &observation_item,
            processed.clone(),
            resolved.as_ref(),
        );

        let candidate = result
            .get("trade_signal")
            .and_then(|signal| signal.get("classification"))
            .and_then(Value::as_str)
            .is_some_and(|value| matches!(value, "BUY_CANDIDATE" | "SELL_CANDIDATE"));
        if candidate
            && observation_item
                .get("blockTime")
                .and_then(Value::as_i64)
                .is_none_or(|timestamp| timestamp <= 0)
        {
            let transaction_time = processed
                .get("raw_transaction")
                .and_then(|raw| raw.get("blockTime"))
                .and_then(Value::as_i64)
                .filter(|timestamp| *timestamp > 0);
            let recovered_time = if let Some(timestamp) = transaction_time {
                Some(timestamp)
            } else {
                let slot = observation_item
                    .get("slot")
                    .and_then(Value::as_u64)
                    .or_else(|| processed.get("slot").and_then(Value::as_u64));
                match slot {
                    Some(slot) => client.get_block_time(slot).ok().flatten(),
                    None => None,
                }
            };
            if let Some(timestamp) = recovered_time.filter(|timestamp| *timestamp > 0) {
                observation_item["blockTime"] = json!(timestamp);
                result = prepare_wallet_transaction_result(
                    wallet_address,
                    &observation_item,
                    processed.clone(),
                    resolved.as_ref(),
                );
            } else {
                let status = record_retry(
                    store,
                    wallet_address,
                    signature,
                    ProcessingStage::RpcFetch,
                    "block_time_unavailable",
                    "Source blockTime is missing and public RPC recovery did not return a valid timestamp.",
                    processed.get("raw_transaction"),
                )?;
                if let Some(object) = result.as_object_mut() {
                    object.insert(
                        "trade_signal".to_string(),
                        unknown_signal("Source blockTime could not be recovered; no copy candidate is permitted."),
                    );
                    object.insert(
                        "metadata_status".to_string(),
                        Value::String("block_time_unavailable".to_string()),
                    );
                    object.insert(
                        "error".to_string(),
                        Value::String(
                            "Source blockTime is missing; retry or manual review is required."
                                .to_string(),
                        ),
                    );
                    object.insert("retryable".to_string(), Value::Bool(status == "pending"));
                    object.insert("processing_status".to_string(), Value::String(status));
                }
            }
        }
        let raw = result.get("raw_transaction").cloned();
        if let Some(object) = result.as_object_mut() {
            object.insert(
                "source_lane".to_string(),
                Value::String(source_lane.to_string()),
            );
            object.insert(
                "paper_copy_eligible".to_string(),
                Value::Bool(source_lane == "fresh"),
            );
        }
        let record_status = if result.get("status").and_then(Value::as_str) == Some("failed") {
            "failed"
        } else {
            "observed"
        };
        let parsed_evidence = json!({
            "slot": result.get("slot"),
            "fee": result.get("fee"),
            "signers": result.get("signers"),
            "program_ids": result.get("program_ids"),
            "instructions": result.get("instructions"),
            "token_balance_changes": result.get("token_balance_changes"),
            "signature_observation": result.get("signature_observation"),
            "source_lane": result.get("source_lane"),
            "paper_copy_eligible": result.get("paper_copy_eligible")
        });
        store.save_transaction_evidence(
            wallet_address,
            signature,
            raw.as_ref(),
            Some(&parsed_evidence),
            result.get("trade_signal"),
            record_status,
        )?;

        let retryable = result.get("retryable").and_then(Value::as_bool) == Some(true);
        let held_for_review = matches!(
            result.get("processing_status").and_then(Value::as_str),
            Some("needs_review" | "incomplete" | "stale")
        );
        if !defer_processed && !retryable && !held_for_review {
            store.mark_claimed_processed(wallet_address, signature, &claim_owner)?;
        } else {
            store.release_signature_claim(wallet_address, signature, &claim_owner)?;
        }
        results.push(result);
    }
    Ok(results)
}

/// Mark stale BUY/SELL candidates as retained audit records and annotate the skip.
pub fn annotate_stale_candidates(
    store: &SignatureStore,
    wallet_address: &str,
    results: Vec<Value>,
    now_ts: i64,
) -> Result<Vec<Value>, TransactionMonitorError> {
    let mut annotated = Vec::with_capacity(results.len());
    for mut result in results {
        let classification = result
            .get("trade_signal")
            .and_then(|signal| signal.get("classification"))
            .and_then(Value::as_str);
        if matches!(classification, Some("BUY_CANDIDATE" | "SELL_CANDIDATE")) {
            let signature = result.get("signature").and_then(Value::as_str);
            let block_time = result
                .get("signature_observation")
                .and_then(|observation| observation.get("block_time"))
                .and_then(Value::as_i64);
            if !source_block_time_ok(block_time, now_ts) {
                if let Some(signature) =
                    signature.filter(|value| is_valid_transaction_signature(value))
                {
                    store.mark_stale(wallet_address, signature)?;
                }
                if let Some(object) = result.as_object_mut() {
                    object.insert(
                        "paper_copy_skip_reason".to_string(),
                        Value::String("SOURCE_TRANSACTION_STALE_OR_TIMESTAMP_MISSING".to_string()),
                    );
                    object.insert(
                        "processing_status".to_string(),
                        Value::String("stale".to_string()),
                    );
                    object.insert("retryable".to_string(), Value::Bool(false));
                    object.insert("paper_copy_eligible".to_string(), Value::Bool(false));
                }
            }
        }
        annotated.push(result);
    }
    Ok(annotated)
}

/// Discover signatures, persist the baseline/cursor state, and process only saved pending items.
pub fn monitor_wallet(
    client: &SolanaRpcClient,
    store: &SignatureStore,
    wallet_address: &str,
    limit: usize,
    defer_processed: bool,
    latency_recorder: Option<&LatencyRecorder>,
) -> Result<Vec<Value>, TransactionMonitorError> {
    if !is_valid_solana_address(wallet_address) {
        return Err(TransactionMonitorError::InvalidInput(
            "Invalid transaction-monitor input: ".to_string(),
        ));
    }
    let had_baseline = store.has_baseline(wallet_address)?;
    let _discovered = get_and_store_new_wallet_signatures(
        client,
        store,
        wallet_address,
        limit,
        latency_recorder,
    )?;
    if !had_baseline {
        return Ok(Vec::new());
    }

    let history_batch = std::env::var("ENGEN_HISTORY_BATCH")
        .ok()
        .and_then(|value| value.parse::<usize>().ok())
        .filter(|value| (10..=25).contains(value))
        .unwrap_or(DEFAULT_HISTORY_BATCH);
    let plan = ScanPlan::new(limit.min(MAX_FRESH_BATCH), history_batch, 0);
    let fresh_count = store.pending_count_for_lane(wallet_address, "fresh")?;
    let mut processed = Vec::new();

    if let Some(ScanStage::Fresh { limit: fresh_limit }) = plan.next_stage(fresh_count, 0) {
        let fresh = store.pending_signature_observations(wallet_address, "fresh", fresh_limit)?;
        processed.extend(process_signatures_for_lane(
            client,
            store,
            wallet_address,
            &fresh,
            "fresh",
            defer_processed,
            latency_recorder,
        )?);
    }

    // A pending/claimed Fresh item blocks History. Only after Fresh has drained
    // do we spend an RPC request on the bounded History cursor and process its batch.
    let fresh_remaining = store.pending_count_for_lane(wallet_address, "fresh")?;
    if fresh_remaining == 0 {
        resume_history_backfill(client, store, wallet_address, latency_recorder)?;
        let history_remaining = store.pending_count_for_lane(wallet_address, "history")?;
        if let Some(ScanStage::History {
            limit: history_limit,
            ..
        }) = plan.next_stage(0, history_remaining)
        {
            let history =
                store.pending_signature_observations(wallet_address, "history", history_limit)?;
            processed.extend(process_signatures_for_lane(
                client,
                store,
                wallet_address,
                &history,
                "history",
                defer_processed,
                latency_recorder,
            )?);
        }
    }

    annotate_stale_candidates(
        store,
        wallet_address,
        processed,
        chrono::Utc::now().timestamp(),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    const WALLET: &str = "11111111111111111111111111111111";
    const OTHER: &str = "11111111111111111111111111111112";
    const ASSET: &str = "11111111111111111111111111111113";
    const USDC: &str = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v";

    fn signature_item() -> Value {
        json!({
            "signature":"1111111111111111111111111111111111111111111111111111111111111111",
            "slot":100,
            "confirmationStatus":"confirmed",
            "err":null,
            "blockTime":1760000000
        })
    }

    fn processed_transaction() -> Value {
        json!({
            "signature":"1111111111111111111111111111111111111111111111111111111111111111",
            "available":true,
            "metadata_complete":true,
            "status":"success",
            "signers":[WALLET,OTHER],
            "raw_transaction":{"meta":{"err":null}},
            "token_balance_changes":[
                {"owner":WALLET,"mint":ASSET,"decimals":6,"delta_amount":"1000000"},
                {"owner":WALLET,"mint":USDC,"decimals":6,"delta_amount":"-2000000"},
                {"owner":OTHER,"mint":ASSET,"decimals":6,"delta_amount":"9000000"}
            ]
        })
    }

    #[test]
    fn duplicate_signature_occurrences_are_rejected_after_the_first() {
        let mut seen = HashSet::new();
        assert!(first_signature_occurrence("sig-a", &mut seen));
        assert!(!first_signature_occurrence("sig-a", &mut seen));
        assert!(first_signature_occurrence("sig-b", &mut seen));
    }

    #[test]
    fn terminal_signature_statuses_are_not_reprocessed() {
        assert!(is_terminal_signature_status(Some("processed")));
        assert!(is_terminal_signature_status(Some("stale")));
        assert!(is_terminal_signature_status(Some("needs_review")));
        assert!(!is_terminal_signature_status(Some("pending")));
        assert!(!is_terminal_signature_status(None));
    }

    #[test]
    fn stale_candidate_is_annotated_and_persisted_without_being_deleted() {
        let directory = tempfile::tempdir().unwrap();
        let store = SignatureStore::open(directory.path().join("signatures.db")).unwrap();
        let signature = signature_item()["signature"].as_str().unwrap().to_string();
        store.save(WALLET, &signature, Some(100)).unwrap();
        let mut candidate = processed_transaction();
        candidate["trade_signal"] = json!({"classification":"BUY_CANDIDATE"});
        candidate["signature_observation"] = json!({"block_time":1_759_999_900});
        let results =
            annotate_stale_candidates(&store, WALLET, vec![candidate], 1_760_000_001).unwrap();
        assert_eq!(results[0]["processing_status"], "stale");
        assert_eq!(
            results[0]["paper_copy_skip_reason"],
            "SOURCE_TRANSACTION_STALE_OR_TIMESTAMP_MISSING"
        );
        assert_eq!(
            store.get_status(WALLET, &signature).unwrap().as_deref(),
            Some("stale")
        );
    }

    #[test]
    fn confirmed_wallet_signer_keeps_only_its_own_changes_and_classifies() {
        let result = prepare_wallet_transaction_result(
            WALLET,
            &signature_item(),
            processed_transaction(),
            None,
        );
        assert_eq!(result["token_balance_changes"].as_array().unwrap().len(), 2);
        assert_eq!(
            result["signature_observation"]["confirmation_status"],
            "confirmed"
        );
        assert_eq!(result["trade_signal"]["classification"], "BUY_CANDIDATE");
        assert_eq!(result["retryable"], Value::Null);
    }

    #[test]
    fn insufficient_confirmation_stays_unknown_and_retryable() {
        let mut item = signature_item();
        item["confirmationStatus"] = json!("processed");
        let result =
            prepare_wallet_transaction_result(WALLET, &item, processed_transaction(), None);
        assert_eq!(result["trade_signal"]["classification"], "UNKNOWN");
        assert_eq!(result["retryable"], true);
    }

    #[test]
    fn mismatched_signer_never_becomes_candidate() {
        let mut processed = processed_transaction();
        processed["signers"] = json!([OTHER]);
        let result = prepare_wallet_transaction_result(WALLET, &signature_item(), processed, None);
        assert_eq!(result["trade_signal"]["classification"], "UNKNOWN");
    }

    #[test]
    fn resolved_finality_is_merged_without_replacing_provider_observation() {
        let mut item = signature_item();
        item["confirmationStatus"] = Value::Null;
        let resolved = json!({"confirmationStatus":"finalized","slot":101,"err":null});
        let result = prepare_wallet_transaction_result(
            WALLET,
            &item,
            processed_transaction(),
            Some(&resolved),
        );
        assert_eq!(
            result["signature_observation"]["confirmation_status"],
            "finalized"
        );
        assert_eq!(result["signature_observation"]["slot"], 100);
    }

    #[test]
    fn failed_transactions_are_never_emitted_as_buy_or_sell_candidates() {
        let mut processed = processed_transaction();
        processed["status"] = json!("failed");
        processed["raw_transaction"] = json!({"meta":{"err":{"InstructionError":[0,"Custom"]}}});
        let result = prepare_wallet_transaction_result(WALLET, &signature_item(), processed, None);
        assert_eq!(
            result["trade_signal"]["classification"],
            "FAILED_TRANSACTION"
        );
    }
}
