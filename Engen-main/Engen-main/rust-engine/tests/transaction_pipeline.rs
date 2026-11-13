use std::io::{Read, Write};
use std::net::TcpListener;
use std::thread;
use std::time::Duration;

use engen_engine::signature_store::SignatureStore;
use engen_engine::solana_public::{RpcConfig, SolanaRpcClient};
use engen_engine::transaction_monitor::monitor_wallet;
use serde_json::{json, Value};

const WALLET: &str = "So11111111111111111111111111111111111111112";
const OTHER: &str = "11111111111111111111111111111112";
const OLD_SIGNATURE: &str = "1111111111111111111111111111111111111111111111111111111111111111";
const NEW_SIGNATURE: &str = "1111111111111111111111111111111111111111111111111111111111111112";
const SYSTEM_PROGRAM: &str = "11111111111111111111111111111111";
const TOKEN_2022_PROGRAM: &str = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb";

fn spawn_mock_rpc(responses: Vec<Value>) -> (String, thread::JoinHandle<()>) {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind local mock RPC");
    let address = listener.local_addr().expect("read mock RPC address");
    let worker = thread::spawn(move || {
        for body in responses {
            let (mut stream, _) = listener.accept().expect("accept mock RPC request");
            stream
                .set_read_timeout(Some(Duration::from_secs(3)))
                .expect("set mock socket timeout");
            let mut request = [0_u8; 4096];
            let _ = stream.read(&mut request);
            let payload = serde_json::to_vec(&body).expect("serialize mock JSON-RPC result");
            let headers = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                payload.len()
            );
            stream
                .write_all(headers.as_bytes())
                .expect("write mock headers");
            stream.write_all(&payload).expect("write mock JSON body");
            stream.flush().expect("flush mock JSON response");
        }
    });
    (format!("http://{address}"), worker)
}

fn rpc_response(id: u64, result: Value) -> Value {
    json!({"jsonrpc": "2.0", "id": id, "result": result})
}

#[test]
fn shared_phase2_golden_corpus_matches_transaction_type_contract() {
    use engen_engine::transaction_analysis::{analyze_transaction, classify_transaction};

    let cases: Vec<Value> = serde_json::from_str(include_str!("fixtures/phase2_transactions.json"))
        .expect("parse shared Phase 2 golden corpus");
    assert!(cases.len() >= 6);
    for case in cases {
        let name = case["name"].as_str().expect("fixture name");
        let wallet = case["wallet"].as_str().expect("fixture wallet");
        let transaction = &case["transaction"];
        let changes = &case["balance_changes"];
        let result = classify_transaction(transaction, wallet, Some(changes));
        assert_eq!(
            result["transaction_type"], case["expected_type"],
            "transaction type mismatch for fixture {name}"
        );
        assert_eq!(
            result["trade_signal"], case["expected_signal"],
            "trade signal mismatch for fixture {name}"
        );
        if let Some(program_id) = case
            .get("expected_outer_program_id")
            .and_then(Value::as_str)
        {
            let summary = analyze_transaction(transaction).expect("analyze versioned fixture");
            assert_eq!(
                summary["instructions"][0]["program_id"], program_id,
                "lookup-table program id mismatch for fixture {name}"
            );
        }
    }
}

#[test]
fn missing_signature_block_time_is_recovered_and_persisted_before_fresh_eligibility() {
    let block_time = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .expect("system clock after Unix epoch")
        .as_secs() as i64;

    let buy_transaction = json!({
        "slot": 103,
        "transaction": {
            "message": {
                "header": {"numRequiredSignatures": 1},
                "accountKeys": [
                    {"pubkey": WALLET, "signer": true, "writable": true},
                    {"pubkey": "ASSET_TOKEN_ACCOUNT", "signer": false, "writable": true},
                    {"pubkey": "QUOTE_TOKEN_ACCOUNT", "signer": false, "writable": true},
                    {"pubkey": "JUPITER_PROGRAM", "signer": false, "writable": false}
                ],
                "instructions": [{
                    "program": "jupiter",
                    "programId": "JUPITER_PROGRAM",
                    "parsed": {"type": "swap"}
                }]
            }
        },
        "meta": {
            "err": null,
            "fee": 5000,
            "preBalances": [10000000, 0, 0, 0],
            "postBalances": [9995000, 0, 0, 0],
            "preTokenBalances": [
                {
                    "accountIndex": 1,
                    "mint": "MINT_A",
                    "owner": WALLET,
                    "uiTokenAmount": {"amount": "0", "decimals": 6}
                },
                {
                    "accountIndex": 2,
                    "mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                    "owner": WALLET,
                    "uiTokenAmount": {"amount": "2500000", "decimals": 6}
                }
            ],
            "postTokenBalances": [
                {
                    "accountIndex": 1,
                    "mint": "MINT_A",
                    "owner": WALLET,
                    "uiTokenAmount": {"amount": "100", "decimals": 6}
                },
                {
                    "accountIndex": 2,
                    "mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                    "owner": WALLET,
                    "uiTokenAmount": {"amount": "0", "decimals": 6}
                }
            ]
        }
    });

    let (endpoint, server) = spawn_mock_rpc(vec![
        rpc_response(
            6,
            json!([{
                "signature": NEW_SIGNATURE,
                "slot": 103,
                "err": null,
                "confirmationStatus": "confirmed",
                "blockTime": null
            }]),
        ),
        rpc_response(4, buy_transaction),
        rpc_response(7, json!(block_time)),
        rpc_response(6, json!([])),
    ]);

    let client = SolanaRpcClient::new(RpcConfig {
        endpoint,
        timeout: Duration::from_secs(3),
        retries: 0,
        backoff: Duration::ZERO,
        min_interval: Duration::ZERO,
    })
    .expect("create loopback-only blockTime-recovery RPC client");

    let directory = tempfile::tempdir().expect("create disposable DB directory");
    let database_path = directory.path().join("block-time-recovery.db");
    let store = SignatureStore::open(&database_path).expect("open disposable store");
    store.mark_baseline(WALLET).expect("initialize baseline");

    let results = monitor_wallet(&client, &store, WALLET, 10, false, None)
        .expect("monitor source wallet and recover missing blockTime");

    assert_eq!(results.len(), 1);
    assert_eq!(
        results[0]["trade_signal"]["classification"],
        "BUY_CANDIDATE"
    );
    assert_eq!(
        results[0]["signature_observation"]["block_time"],
        block_time
    );
    assert_eq!(results[0]["paper_copy_eligible"], true);
    assert_eq!(
        store.get_status(WALLET, NEW_SIGNATURE).unwrap().as_deref(),
        Some("processed")
    );
    let record = store
        .get_transaction_record(WALLET, NEW_SIGNATURE)
        .unwrap()
        .expect("persist recovered transaction evidence");
    assert_eq!(
        record.parsed_evidence.as_ref().unwrap()["signature_observation"]["block_time"],
        block_time
    );
    assert_eq!(
        record.parsed_evidence.as_ref().unwrap()["source_lane"],
        "fresh"
    );
    server
        .join()
        .expect("mock RPC server completed blockTime recovery requests");
}

#[test]
fn pending_fresh_signature_blocks_history_processing_in_the_same_monitor_cycle() {
    const HISTORY_SIGNATURE: &str =
        "1111111111111111111111111111111111111111111111111111111111111113";

    let (endpoint, server) = spawn_mock_rpc(vec![
        rpc_response(
            6,
            json!([{
                "signature": NEW_SIGNATURE,
                "slot": 102,
                "err": null,
                "confirmationStatus": "confirmed",
                "blockTime": 1760000002
            }]),
        ),
        rpc_response(
            6,
            json!([{
                "signature": HISTORY_SIGNATURE,
                "slot": 101,
                "err": null,
                "confirmationStatus": "confirmed",
                "blockTime": 1760000001
            }]),
        ),
        rpc_response(6, json!([])),
        rpc_response(4, Value::Null),
    ]);

    let client = SolanaRpcClient::new(RpcConfig {
        endpoint,
        timeout: Duration::from_secs(3),
        retries: 0,
        backoff: Duration::ZERO,
        min_interval: Duration::ZERO,
    })
    .expect("create loopback-only Fresh-first RPC client");

    let directory = tempfile::tempdir().expect("create disposable DB directory");
    let database_path = directory.path().join("fresh-history.db");
    let store = SignatureStore::open(&database_path).expect("open disposable store");
    store
        .mark_baseline(WALLET)
        .expect("initialize source baseline");
    store
        .save(WALLET, OLD_SIGNATURE, Some(100))
        .expect("save processed anchor");
    store
        .mark_processed(WALLET, OLD_SIGNATURE)
        .expect("advance processed anchor");

    let results = monitor_wallet(&client, &store, WALLET, 1, false, None)
        .expect("run Fresh-first monitoring cycle");

    assert_eq!(results.len(), 1);
    assert_eq!(results[0]["signature"], NEW_SIGNATURE);
    assert_eq!(results[0]["processing_status"], "pending");
    assert_eq!(
        store.get_status(WALLET, NEW_SIGNATURE).unwrap().as_deref(),
        Some("pending")
    );
    assert_eq!(
        store
            .get_status(WALLET, HISTORY_SIGNATURE)
            .unwrap()
            .as_deref(),
        Some("pending")
    );
    assert_eq!(
        store
            .pending_signatures_for_lane(WALLET, "fresh", 10)
            .unwrap(),
        vec![NEW_SIGNATURE.to_string()]
    );
    assert_eq!(
        store
            .pending_signatures_for_lane(WALLET, "history", 10)
            .unwrap(),
        vec![HISTORY_SIGNATURE.to_string()]
    );

    server
        .join()
        .expect("mock RPC server completed only Fresh processing requests");
}

#[test]
fn initial_baseline_defers_bounded_history_backfill_until_next_cycle_and_persists_history_as_non_copyable(
) {
    const HISTORY_SIGNATURE: &str =
        "1111111111111111111111111111111111111111111111111111111111111114";

    let history_transfer = json!({
        "slot": 99,
        "transaction": {
            "message": {
                "header": {"numRequiredSignatures": 1},
                "accountKeys": [
                    {"pubkey": WALLET, "signer": true, "writable": true},
                    {"pubkey": OTHER, "signer": false, "writable": true},
                    {"pubkey": SYSTEM_PROGRAM, "signer": false, "writable": false}
                ],
                "instructions": [{
                    "program": "system",
                    "programId": SYSTEM_PROGRAM,
                    "parsed": {
                        "type": "transfer",
                        "info": {
                            "source": WALLET,
                            "destination": OTHER,
                            "lamports": 1000000
                        }
                    }
                }]
            }
        },
        "meta": {
            "err": null,
            "fee": 5000,
            "preBalances": [10000000, 0, 1],
            "postBalances": [8995000, 1000000, 1],
            "preTokenBalances": [],
            "postTokenBalances": [],
            "innerInstructions": []
        }
    });

    let (endpoint, server) = spawn_mock_rpc(vec![
        rpc_response(
            6,
            json!([{
                "signature": OLD_SIGNATURE,
                "slot": 100,
                "err": null,
                "confirmationStatus": "finalized",
                "blockTime": 1760000000
            }]),
        ),
        rpc_response(
            6,
            json!([{
                "signature": OLD_SIGNATURE,
                "slot": 100,
                "err": null,
                "confirmationStatus": "finalized",
                "blockTime": 1760000000
            }]),
        ),
        rpc_response(
            6,
            json!([{
                "signature": HISTORY_SIGNATURE,
                "slot": 99,
                "err": null,
                "confirmationStatus": "confirmed",
                "blockTime": 1759999999
            }]),
        ),
        rpc_response(4, history_transfer.clone()),
    ]);

    let client = SolanaRpcClient::new(RpcConfig {
        endpoint,
        timeout: Duration::from_secs(3),
        retries: 0,
        backoff: Duration::ZERO,
        min_interval: Duration::ZERO,
    })
    .expect("create loopback-only History backfill client");

    let directory = tempfile::tempdir().expect("create disposable DB directory");
    let database_path = directory.path().join("history-backfill-e2e.db");
    {
        let store = SignatureStore::open(&database_path).expect("open disposable signature store");

        let baseline = monitor_wallet(&client, &store, WALLET, 10, false, None)
            .expect("first observation must only establish the baseline");
        assert!(baseline.is_empty());
        assert_eq!(
            store.history_backfill_state(WALLET).unwrap(),
            Some((Some(OLD_SIGNATURE.to_string()), 0, false))
        );

        let history_results = monitor_wallet(&client, &store, WALLET, 10, false, None)
            .expect("resume the bounded History cursor on the next cycle");
        assert_eq!(history_results.len(), 1);
        assert_eq!(history_results[0]["signature"], HISTORY_SIGNATURE);
        assert_eq!(history_results[0]["source_lane"], "history");
        assert_eq!(history_results[0]["paper_copy_eligible"], false);
        assert_eq!(
            history_results[0]["trade_signal"]["transaction_type"],
            "TRANSFER"
        );
        assert_eq!(
            history_results[0]["trade_signal"]["trade_signal"],
            "NOT_COPYABLE"
        );
        assert_eq!(
            store
                .get_status(WALLET, HISTORY_SIGNATURE)
                .unwrap()
                .as_deref(),
            Some("processed")
        );
        assert_eq!(
            store.history_backfill_state(WALLET).unwrap(),
            Some((Some(HISTORY_SIGNATURE.to_string()), 1, true))
        );
        let record = store
            .get_transaction_record(WALLET, HISTORY_SIGNATURE)
            .unwrap()
            .expect("History transaction evidence must be persisted");
        assert_eq!(record.status, "processed");
        assert_eq!(
            record.parsed_evidence.as_ref().unwrap()["source_lane"],
            "history"
        );
        assert_eq!(
            record.parsed_evidence.as_ref().unwrap()["paper_copy_eligible"],
            false
        );
        assert_eq!(
            record.classification.as_ref().unwrap()["transaction_type"],
            "TRANSFER"
        );
    }

    let restarted = SignatureStore::open(&database_path).expect("reopen after monitoring restart");
    assert_eq!(
        restarted.history_backfill_state(WALLET).unwrap(),
        Some((Some(HISTORY_SIGNATURE.to_string()), 1, true))
    );
    let record = restarted
        .get_transaction_record(WALLET, HISTORY_SIGNATURE)
        .unwrap()
        .expect("History evidence survives restart");
    assert_eq!(record.status, "processed");
    assert_eq!(record.parsed_evidence.unwrap()["source_lane"], "history");
    server
        .join()
        .expect("mock RPC server completed exactly the expected read-only requests");
}

#[test]
fn unsupported_transaction_version_is_retained_for_manual_review_after_restart() {
    let (endpoint, server) = spawn_mock_rpc(vec![
        rpc_response(
            6,
            json!([{
                "signature": OLD_SIGNATURE,
                "slot": 100,
                "err": null,
                "confirmationStatus": "finalized",
                "blockTime": 1760000000
            }]),
        ),
        rpc_response(
            6,
            json!([{
                "signature": NEW_SIGNATURE,
                "slot": 101,
                "err": null,
                "confirmationStatus": "confirmed",
                "blockTime": 1760000001
            }]),
        ),
        json!({
            "jsonrpc":"2.0",
            "id":4,
            "error":{
                "code":-32015,
                "message":"Transaction version (1) is not supported by this RPC node"
            }
        }),
        rpc_response(6, json!([])),
    ]);

    let client = SolanaRpcClient::new(RpcConfig {
        endpoint,
        timeout: Duration::from_secs(3),
        retries: 0,
        backoff: Duration::ZERO,
        min_interval: Duration::ZERO,
    })
    .expect("create loopback-only unsupported-version RPC client");

    let directory = tempfile::tempdir().expect("create disposable DB directory");
    let database_path = directory.path().join("signatures.db");
    {
        let store = SignatureStore::open(&database_path).expect("open disposable store");
        let baseline = monitor_wallet(&client, &store, WALLET, 10, false, None)
            .expect("initialize public-wallet baseline");
        assert!(baseline.is_empty());

        let results = monitor_wallet(&client, &store, WALLET, 10, false, None)
            .expect("scan unsupported transaction version");
        assert_eq!(results.len(), 1);
        assert_eq!(
            results[0]["metadata_status"],
            "unsupported_transaction_version"
        );
        assert_eq!(results[0]["processing_status"], "needs_review");
        assert_eq!(results[0]["retryable"], false);
        assert_eq!(
            store.get_status(WALLET, NEW_SIGNATURE).unwrap().as_deref(),
            Some("needs_review")
        );
        let record = store
            .get_transaction_record(WALLET, NEW_SIGNATURE)
            .unwrap()
            .expect("unsupported version must be retained");
        assert_eq!(record.status, "needs_review");
        assert_eq!(record.raw_transaction, None);
    }

    let reopened = SignatureStore::open(&database_path).expect("reopen disposable store");
    assert_eq!(
        reopened
            .get_status(WALLET, NEW_SIGNATURE)
            .unwrap()
            .as_deref(),
        Some("needs_review")
    );
    let record = reopened
        .get_transaction_record(WALLET, NEW_SIGNATURE)
        .unwrap()
        .expect("review evidence survives process restart");
    assert_eq!(record.status, "needs_review");
    assert_eq!(record.raw_transaction, None);
    server
        .join()
        .expect("mock RPC server completed expected calls");
}

#[test]
fn failed_transaction_is_persisted_as_non_copyable_evidence_after_restart() {
    let failed_transaction = json!({
        "slot": 102,
        "transaction": {
            "message": {
                "header": {"numRequiredSignatures": 1},
                "accountKeys": [
                    {"pubkey": WALLET, "signer": true, "writable": true},
                    {"pubkey": OTHER, "signer": false, "writable": true},
                    {"pubkey": SYSTEM_PROGRAM, "signer": false, "writable": false}
                ],
                "instructions": [{
                    "program": "system",
                    "programId": SYSTEM_PROGRAM,
                    "parsed": {
                        "type": "transfer",
                        "info": {"source": WALLET, "destination": OTHER, "lamports": 1000000}
                    }
                }]
            }
        },
        "meta": {
            "err": {"InstructionError": [0, "Custom"]},
            "fee": 5000,
            "preBalances": [10000000, 0, 1],
            "postBalances": [8995000, 1000000, 1],
            "preTokenBalances": [{
                "accountIndex": 0,
                "mint": "ExampleMint",
                "owner": WALLET,
                "uiTokenAmount": {"amount": "0", "decimals": 6}
            }],
            "postTokenBalances": [{
                "accountIndex": 0,
                "mint": "ExampleMint",
                "owner": WALLET,
                "uiTokenAmount": {"amount": "100", "decimals": 6}
            }]
        }
    });

    let (endpoint, server) = spawn_mock_rpc(vec![
        rpc_response(
            6,
            json!([{
                "signature": OLD_SIGNATURE,
                "slot": 100,
                "err": null,
                "confirmationStatus": "finalized",
                "blockTime": 1760000000
            }]),
        ),
        rpc_response(
            6,
            json!([{
                "signature": NEW_SIGNATURE,
                "slot": 102,
                "err": {"InstructionError": [0, "Custom"]},
                "confirmationStatus": "confirmed",
                "blockTime": 1760000002
            }]),
        ),
        rpc_response(4, failed_transaction.clone()),
        rpc_response(6, json!([])),
    ]);

    let client = SolanaRpcClient::new(RpcConfig {
        endpoint,
        timeout: Duration::from_secs(3),
        retries: 0,
        backoff: Duration::ZERO,
        min_interval: Duration::ZERO,
    })
    .expect("create loopback-only failed-transaction RPC client");

    let directory = tempfile::tempdir().expect("create disposable DB directory");
    let database_path = directory.path().join("signatures.db");
    {
        let store = SignatureStore::open(&database_path).expect("open disposable store");
        let baseline = monitor_wallet(&client, &store, WALLET, 10, false, None)
            .expect("initialize public-wallet baseline");
        assert!(baseline.is_empty());

        let results = monitor_wallet(&client, &store, WALLET, 10, false, None)
            .expect("process failed transaction evidence");
        assert_eq!(results.len(), 1);
        assert_eq!(results[0]["status"], "failed");
        assert_eq!(results[0]["trade_signal"]["transaction_type"], "FAILED");
        assert_eq!(results[0]["trade_signal"]["trade_signal"], "NOT_COPYABLE");
        assert_eq!(
            store.get_status(WALLET, NEW_SIGNATURE).unwrap().as_deref(),
            Some("processed")
        );
        let record = store
            .get_transaction_record(WALLET, NEW_SIGNATURE)
            .unwrap()
            .expect("failed transaction evidence is persisted");
        assert_eq!(record.status, "failed");
        assert_eq!(record.raw_transaction, Some(failed_transaction.clone()));
        assert_eq!(
            record.classification.as_ref().unwrap()["transaction_type"],
            "FAILED"
        );
    }

    let reopened = SignatureStore::open(&database_path).expect("reopen disposable store");
    let recovered = reopened
        .get_transaction_record(WALLET, NEW_SIGNATURE)
        .unwrap()
        .expect("failed evidence survives restart");
    assert_eq!(recovered.status, "failed");
    assert_eq!(recovered.raw_transaction, Some(failed_transaction));
    assert_eq!(
        recovered.classification.unwrap()["trade_signal"],
        "NOT_COPYABLE"
    );
    server
        .join()
        .expect("mock RPC server completed expected calls");
}

#[test]
fn wallet_discovery_to_transfer_classification_and_restart_evidence_is_offline() {
    let transaction = json!({
        "slot": 101,
        "transaction": {
            "message": {
                "header": {"numRequiredSignatures": 1},
                "accountKeys": [
                    {"pubkey": WALLET, "signer": true, "writable": true},
                    {"pubkey": OTHER, "signer": false, "writable": true},
                    {"pubkey": SYSTEM_PROGRAM, "signer": false, "writable": false}
                ],
                "instructions": [{
                    "program": "system",
                    "programId": SYSTEM_PROGRAM,
                    "parsed": {
                        "type": "transfer",
                        "info": {
                            "source": WALLET,
                            "destination": OTHER,
                            "lamports": 1000000
                        }
                    }
                }]
            }
        },
        "meta": {
            "err": null,
            "fee": 5000,
            "preBalances": [10000000, 0, 1],
            "postBalances": [8995000, 1000000, 1],
            "preTokenBalances": [],
            "postTokenBalances": [],
            "innerInstructions": [{
                "index": 0,
                "instructions": [{
                    "program": "spl-token-2022",
                    "programId": TOKEN_2022_PROGRAM,
                    "parsed": {
                        "type": "transferChecked",
                        "info": {"mint": "ExampleMint", "tokenAmount": {"amount": "7", "decimals": 0}}
                    }
                }]
            }]
        }
    });

    let (endpoint, server) = spawn_mock_rpc(vec![
        rpc_response(
            6,
            json!([{
                "signature": OLD_SIGNATURE,
                "slot": 100,
                "err": null,
                "confirmationStatus": "finalized",
                "blockTime": 1760000000
            }]),
        ),
        rpc_response(
            6,
            json!([{
                "signature": NEW_SIGNATURE,
                "slot": 101,
                "err": null,
                "confirmationStatus": "confirmed",
                "blockTime": 1760000001
            }]),
        ),
        rpc_response(4, transaction.clone()),
        rpc_response(6, json!([])),
    ]);

    let client = SolanaRpcClient::new(RpcConfig {
        endpoint,
        timeout: Duration::from_secs(3),
        retries: 0,
        backoff: Duration::ZERO,
        min_interval: Duration::ZERO,
    })
    .expect("construct client against loopback-only mock server");

    let directory = tempfile::tempdir().expect("create disposable DB directory");
    let database_path = directory.path().join("signatures.db");
    let store = SignatureStore::open(&database_path).expect("open disposable signature store");

    let baseline = monitor_wallet(&client, &store, WALLET, 10, false, None)
        .expect("initialize public-wallet baseline");
    assert!(baseline.is_empty());

    let results = monitor_wallet(&client, &store, WALLET, 10, false, None)
        .expect("discover and process new signature");
    assert_eq!(results.len(), 1);
    assert_eq!(results[0]["signature"], NEW_SIGNATURE);
    assert_eq!(results[0]["status"], "success");
    assert_eq!(results[0]["trade_signal"]["transaction_type"], "TRANSFER");
    assert_eq!(results[0]["trade_signal"]["trade_signal"], "NOT_COPYABLE");
    assert_eq!(results[0]["instructions"][0]["source"], "message");
    assert_eq!(results[0]["instructions"][1]["source"], "inner");

    let record = store
        .get_transaction_record(WALLET, NEW_SIGNATURE)
        .expect("read persisted transaction record")
        .expect("record must exist");
    assert_eq!(
        record.raw_transaction.as_ref().unwrap()["meta"]["innerInstructions"][0]["instructions"][0]
            ["programId"],
        TOKEN_2022_PROGRAM
    );
    let parsed = record
        .parsed_evidence
        .as_ref()
        .expect("normalized parsed evidence is persisted");
    assert_eq!(parsed["instructions"][0]["source"], "message");
    assert_eq!(parsed["instructions"][1]["source"], "inner");
    let classification = record
        .classification
        .as_ref()
        .expect("classification evidence is persisted");
    assert_eq!(classification["transaction_type"], "TRANSFER");
    assert_eq!(classification["trade_signal"], "NOT_COPYABLE");
    assert_eq!(record.status, "processed");

    drop(store);
    let reopened = SignatureStore::open(&database_path).expect("reopen disposable signature store");
    let recovered = reopened
        .get_transaction_record(WALLET, NEW_SIGNATURE)
        .expect("read evidence after restart")
        .expect("evidence must survive restart");
    assert_eq!(recovered.raw_transaction, Some(transaction));
    let parsed = recovered
        .parsed_evidence
        .expect("normalized parsed evidence survives restart");
    assert_eq!(parsed["instructions"][0]["source"], "message");
    assert_eq!(parsed["instructions"][1]["source"], "inner");
    let classification = recovered
        .classification
        .expect("classification evidence survives restart");
    assert_eq!(classification["transaction_type"], "TRANSFER");
    assert_eq!(classification["trade_signal"], "NOT_COPYABLE");
    assert_eq!(
        reopened
            .get_status(WALLET, NEW_SIGNATURE)
            .unwrap()
            .as_deref(),
        Some("processed")
    );

    server
        .join()
        .expect("mock RPC server completed all expected requests");
}
