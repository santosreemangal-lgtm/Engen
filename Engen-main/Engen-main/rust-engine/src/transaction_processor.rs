//! Read-only retrieval and normalization of public Solana transaction data.

use serde_json::{Map, Value};
use thiserror::Error;

use crate::latency::LatencyRecorder;
use crate::solana_public::{SolanaRpcClient, SolanaRpcError};
use crate::solana_validation::require_transaction_signature;
use crate::transaction_analysis::{
    analyze_native_sol_balance_changes, analyze_token_balance_changes, analyze_transaction,
};

const REQUIRED_METADATA: &[&str] = &[
    "err",
    "fee",
    "preBalances",
    "postBalances",
    "preTokenBalances",
    "postTokenBalances",
];

#[derive(Debug, Error)]
pub enum TransactionProcessorError {
    #[error("Public Solana RPC request failed.")]
    Rpc(#[from] SolanaRpcError),
    #[error("Transaction signature or transaction payload is invalid.")]
    InvalidInput,
    #[error("Transaction payload could not be analyzed.")]
    Analysis,
}

/// Fetch a transaction over the read-only RPC boundary and normalize its analysis.
pub fn process_transaction_signature(
    client: &SolanaRpcClient,
    signature: &str,
    latency_recorder: Option<&LatencyRecorder>,
) -> Result<Value, TransactionProcessorError> {
    require_transaction_signature(signature)
        .map_err(|_| TransactionProcessorError::InvalidInput)?;
    let started = std::time::Instant::now();
    let response = client.get_transaction(signature);
    if let Some(recorder) = latency_recorder {
        let elapsed_ms = started.elapsed().as_secs_f64() * 1_000.0;
        let _ = recorder.record("transaction.rpc_fetch", elapsed_ms);
    }
    let transaction = response?;
    process_transaction_payload(signature, transaction)
}

/// Normalize an already retrieved RPC value. This is separately testable with safe fixtures.
pub fn process_transaction_payload(
    signature: &str,
    transaction: Option<Value>,
) -> Result<Value, TransactionProcessorError> {
    require_transaction_signature(signature)
        .map_err(|_| TransactionProcessorError::InvalidInput)?;

    let Some(transaction) = transaction else {
        return Ok(serde_json::json!({
            "signature": signature,
            "available": false,
            "metadata_complete": false,
            "metadata_status": "not_available_yet",
            "error": "Transaction data is not available yet."
        }));
    };
    if !transaction.is_object() {
        return Err(TransactionProcessorError::InvalidInput);
    }

    let slot = transaction.get("slot").cloned().unwrap_or(Value::Null);
    if !slot.is_null() && slot.as_u64().is_none() {
        return Ok(serde_json::json!({
            "signature": signature,
            "available": false,
            "metadata_complete": false,
            "metadata_status": "invalid_slot",
            "error": "Transaction slot metadata is invalid.",
            "raw_transaction": transaction
        }));
    }

    let account_keys = transaction
        .get("transaction")
        .and_then(|value| value.get("message"))
        .and_then(|value| value.get("accountKeys"));
    if !account_keys.is_some_and(Value::is_array) {
        return Ok(serde_json::json!({
            "signature": signature,
            "available": false,
            "metadata_complete": false,
            "metadata_status": "incomplete_transaction",
            "slot": slot,
            "error": "Transaction message or account keys are unavailable.",
            "raw_transaction": transaction
        }));
    }

    let Some(metadata) = transaction.get("meta") else {
        return Ok(metadata_unavailable(
            signature,
            "missing",
            "Transaction metadata is unavailable.",
            slot,
            &transaction,
        ));
    };
    if metadata.is_null() {
        return Ok(metadata_unavailable(
            signature,
            "missing",
            "Transaction metadata is unavailable.",
            slot,
            &transaction,
        ));
    }
    let Some(metadata) = metadata.as_object() else {
        return Ok(metadata_unavailable(
            signature,
            "invalid",
            "Transaction metadata is invalid.",
            slot,
            &transaction,
        ));
    };

    if let Some(fee) = metadata.get("fee") {
        if !fee.is_null() && fee.as_u64().is_none() {
            return Ok(serde_json::json!({
                "signature": signature,
                "available": false,
                "metadata_complete": false,
                "metadata_status": "invalid_fee",
                "slot": slot,
                "error": "Transaction fee metadata is invalid.",
                "raw_transaction": transaction
            }));
        }
    }

    let analysis =
        analyze_transaction(&transaction).map_err(|_| TransactionProcessorError::Analysis)?;
    let mut balance_changes =
        analyze_token_balance_changes(transaction.get("meta").unwrap_or(&Value::Null));
    if analysis.get("status").and_then(Value::as_str) == Some("success") {
        balance_changes.extend(analyze_native_sol_balance_changes(&transaction));
    }

    let missing_fields: Vec<&str> = REQUIRED_METADATA
        .iter()
        .copied()
        .filter(|field| !metadata.contains_key(*field))
        .collect();
    let mut output: Map<String, Value> = analysis
        .as_object()
        .cloned()
        .ok_or(TransactionProcessorError::Analysis)?;
    output.insert(
        "signature".to_string(),
        Value::String(signature.to_string()),
    );
    output.insert("available".to_string(), Value::Bool(true));
    output.insert(
        "metadata_complete".to_string(),
        Value::Bool(missing_fields.is_empty()),
    );
    output.insert(
        "metadata_missing_fields".to_string(),
        serde_json::json!(missing_fields),
    );
    output.insert(
        "token_balance_changes".to_string(),
        Value::Array(balance_changes),
    );
    output.insert("raw_transaction".to_string(), transaction);
    Ok(Value::Object(output))
}

fn metadata_unavailable(
    signature: &str,
    status: &str,
    message: &str,
    slot: Value,
    transaction: &Value,
) -> Value {
    serde_json::json!({
        "signature": signature,
        "available": false,
        "metadata_complete": false,
        "metadata_status": status,
        "slot": slot,
        "error": message,
        "raw_transaction": transaction
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    const SIGNATURE: &str = "1111111111111111111111111111111111111111111111111111111111111112";
    const WALLET: &str = "Wallet_A";
    const ASSET: &str = "Token_A";

    #[test]
    fn missing_transaction_is_reported_without_fabricating_metadata() {
        let result = process_transaction_payload(SIGNATURE, None).unwrap();
        assert_eq!(result["available"], false);
        assert_eq!(result["metadata_status"], "not_available_yet");
        assert!(!result.get("status").is_some());
        assert!(!result.get("token_balance_changes").is_some());
    }

    #[test]
    fn missing_or_invalid_metadata_is_explicit() {
        let missing = json!({
            "slot":123,
            "transaction":{"message":{"accountKeys":[]}},
            "meta":null
        });
        let result = process_transaction_payload(SIGNATURE, Some(missing)).unwrap();
        assert_eq!(result["metadata_status"], "missing");
        assert_eq!(result["slot"], 123);
        assert!(!result.get("status").is_some());

        let invalid = json!({
            "slot":123,
            "transaction":{"message":{"accountKeys":[]}},
            "meta":"invalid"
        });
        let result = process_transaction_payload(SIGNATURE, Some(invalid)).unwrap();
        assert_eq!(result["metadata_status"], "invalid");
    }

    #[test]
    fn invalid_slot_fee_and_incomplete_transaction_are_rejected_explicitly() {
        let invalid_slot = json!({
            "slot":-1,
            "transaction":{"message":{"accountKeys":[]}},
            "meta":{"err":null}
        });
        assert_eq!(
            process_transaction_payload(SIGNATURE, Some(invalid_slot)).unwrap()["metadata_status"],
            "invalid_slot"
        );

        let invalid_fee = json!({
            "slot":123,
            "transaction":{"message":{"accountKeys":[]}},
            "meta":{"err":null,"fee":-1}
        });
        assert_eq!(
            process_transaction_payload(SIGNATURE, Some(invalid_fee)).unwrap()["metadata_status"],
            "invalid_fee"
        );

        let incomplete = json!({"slot":123,"transaction":{"message":{}},"meta":{"err":null}});
        assert_eq!(
            process_transaction_payload(SIGNATURE, Some(incomplete)).unwrap()["metadata_status"],
            "incomplete_transaction"
        );
    }

    #[test]
    fn successful_transaction_returns_balance_deltas_and_missing_field_list() {
        let transaction = json!({
            "slot":789,
            "transaction":{"message":{"accountKeys":[WALLET]}},
            "meta":{
                "err":null,
                "fee":5000,
                "preTokenBalances":[{
                    "accountIndex":0,
                    "mint":ASSET,
                    "owner":WALLET,
                    "uiTokenAmount":{"amount":"100","decimals":2}
                }],
                "postTokenBalances":[{
                    "accountIndex":0,
                    "mint":ASSET,
                    "owner":WALLET,
                    "uiTokenAmount":{"amount":"175","decimals":2}
                }]
            }
        });
        let result = process_transaction_payload(SIGNATURE, Some(transaction)).unwrap();
        assert_eq!(result["available"], true);
        assert_eq!(result["status"], "success");
        assert_eq!(result["token_balance_changes"][0]["delta_amount"], "75");
        assert_eq!(result["metadata_complete"], false);
        assert!(result["metadata_missing_fields"]
            .as_array()
            .unwrap()
            .iter()
            .any(|field| field == "preBalances"));
    }

    #[test]
    fn failed_transaction_never_adds_native_sol_candidate_changes() {
        let transaction = json!({
            "slot":456,
            "transaction":{"message":{"accountKeys":[WALLET]}},
            "meta":{
                "err":{"InstructionError":[0,"Custom"]},
                "fee":5000,
                "preBalances":[10000],
                "postBalances":[5000],
                "preTokenBalances":[],
                "postTokenBalances":[]
            }
        });
        let result = process_transaction_payload(SIGNATURE, Some(transaction)).unwrap();
        assert_eq!(result["status"], "failed");
        assert_eq!(result["token_balance_changes"], json!([]));
    }

    #[test]
    fn rejects_invalid_signature_before_processing() {
        assert!(matches!(
            process_transaction_payload("bad-signature", None),
            Err(TransactionProcessorError::InvalidInput)
        ));
    }
}
