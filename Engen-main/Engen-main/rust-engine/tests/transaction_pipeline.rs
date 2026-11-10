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
