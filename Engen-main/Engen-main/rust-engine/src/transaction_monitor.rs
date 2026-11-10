//! End-to-end read-only transaction monitoring and conservative signal preparation.

use serde_json::{json, Value};
use thiserror::Error;

use crate::latency::LatencyRecorder;
use crate::signature_store::{ProcessingStage, SignatureStore, SignatureStoreError};
use crate::solana_public::{SolanaRpcClient, SolanaRpcError};
use crate::solana_validation::{is_valid_solana_address, is_valid_transaction_signature};
use crate::transaction_analysis::classify_transaction;
use crate::transaction_processor::{
    process_transaction_signature, TransactionProcessorError,
};
use crate::wallet_monitor::{
    get_and_store_new_wallet_signatures, WalletMonitorError,
};

const MAX_PROCESSING_ATTEMPTS: u32 = 6;

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
    let raw_transaction = result.get("raw_transaction").cloned().unwrap_or(Value::Null);
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
    } else if status == Some("success")
        && !matches!(confirmation, Some("confirmed" | "finalized"))
    {
        unknown_signal(
            "Transaction confirmation is insufficient; no buy/sell candidate was created.",
        )
    } else if status == Some("success") && !wallet_is_signer {
        unknown_signal(
            "The monitored wallet is not confirmed as a transaction signer.",
        )
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
pub fn process_new_signatures(
    client: &SolanaRpcClient,
    store: &SignatureStore,
    wallet_address: &str,
    signatures: &[Value],
    defer_processed: bool,
    latency_recorder: Option<&LatencyRecorder>,
) -> Result<Vec<Value>, TransactionMonitorError> {
    if !is_valid_solana_address(wallet_address) {
        return Err(TransactionMonitorError::InvalidInput(
            "Invalid Solana public wallet address.".to_string(),
        ));
    }

    let mut results = Vec::new();
    for item in signatures {
        let Some(signature) = item.get("signature").and_then(Value::as_str) else {
            continue;
        };
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

        let processed = match process_transaction_signature(client, signature, latency_recorder) {
            Ok(value) => value,
            Err(TransactionProcessorError::Rpc(error)) => {
                let error_code = match error {
                    SolanaRpcError::Http(429) => "http_429",
                    SolanaRpcError::Http(_) => "http_error",
                    SolanaRpcError::Provider(_) => "rpc_provider_error",
                    SolanaRpcError::Transport => "rpc_transport_error",
                    SolanaRpcError::InvalidResponse => "rpc_invalid_response",
                    SolanaRpcError::InvalidInput(_) => "rpc_invalid_input",
                };
                let message = error.to_string();
                let processing_status = record_retry(
                    store,
                    wallet_address,
                    signature,
                    ProcessingStage::RpcFetch,
                    error_code,
                    &message,
                    None,
                )?;
                let error_result = json!({
                    "signature":signature,
                    "available":false,
                    "metadata_complete":false,
                    "metadata_status":"rpc_error",
                    "error":message,
                    "retryable":processing_status == "pending",
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
                object.insert("retryable".to_string(), Value::Bool(processing_status == "pending"));
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

        let result = prepare_wallet_transaction_result(
            wallet_address,
            item,
            processed,
            resolved.as_ref(),
        );
        let raw = result.get("raw_transaction");
        let record_status = if result.get("status").and_then(Value::as_str) == Some("failed") {
            "failed"
        } else {
            "observed"
        };
        store.save_transaction_record(wallet_address, signature, raw, record_status)?;

        let retryable = result.get("retryable").and_then(Value::as_bool) == Some(true);
        if !defer_processed && !retryable {
            store.mark_processed(wallet_address, signature)?;
        }
        results.push(result);
    }
    Ok(results)
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
            "Invalid Solana public wallet address.".to_string(),
        ));
    }
    let pending = get_and_store_new_wallet_signatures(
        client,
        store,
        wallet_address,
        limit,
        latency_recorder,
    )?;
    process_new_signatures(
        client,
        store,
        wallet_address,
        &pending,
        defer_processed,
        latency_recorder,
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
    fn confirmed_wallet_signer_keeps_only_its_own_changes_and_classifies() {
        let result = prepare_wallet_transaction_result(
            WALLET,
            &signature_item(),
            processed_transaction(),
            None,
        );
        assert_eq!(result["token_balance_changes"].as_array().unwrap().len(), 2);
        assert_eq!(result["signature_observation"]["confirmation_status"], "confirmed");
        assert_eq!(result["trade_signal"]["classification"], "BUY_CANDIDATE");
        assert_eq!(result["retryable"], Value::Null);
    }

    #[test]
    fn insufficient_confirmation_stays_unknown_and_retryable() {
        let mut item = signature_item();
        item["confirmationStatus"] = json!("processed");
        let result = prepare_wallet_transaction_result(
            WALLET,
            &item,
            processed_transaction(),
            None,
        );
        assert_eq!(result["trade_signal"]["classification"], "UNKNOWN");
        assert_eq!(result["retryable"], true);
    }

    #[test]
    fn mismatched_signer_never_becomes_candidate() {
        let mut processed = processed_transaction();
        processed["signers"] = json!([OTHER]);
        let result = prepare_wallet_transaction_result(
            WALLET,
            &signature_item(),
            processed,
            None,
        );
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
        assert_eq!(result["signature_observation"]["confirmation_status"], "finalized");
        assert_eq!(result["signature_observation"]["slot"], 100);
    }

    #[test]
    fn failed_transactions_are_never_emitted_as_buy_or_sell_candidates() {
        let mut processed = processed_transaction();
        processed["status"] = json!("failed");
        processed["raw_transaction"] = json!({"meta":{"err":{"InstructionError":[0,"Custom"]}}});
        let result = prepare_wallet_transaction_result(
            WALLET,
            &signature_item(),
            processed,
            None,
        );
        assert_eq!(result["trade_signal"]["classification"], "FAILED_TRANSACTION");
    }
}
