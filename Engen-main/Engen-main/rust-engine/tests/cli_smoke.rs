use std::fs;
use std::process::{Command, Output};

use tempfile::tempdir;

const BIN: &str = env!("CARGO_BIN_EXE_engen-engine");
const WALLET: &str = "So11111111111111111111111111111111111111112";
const SIGNATURE: &str = "1111111111111111111111111111111111111111111111111111111111111112";

fn run(args: &[&str]) -> Output {
    Command::new(BIN)
        .args(args)
        .output()
        .expect("run the compiled Engen CLI")
}

#[test]
fn help_and_version_commands_run_through_the_real_binary() {
    let help = run(&["--help"]);
    assert!(help.status.success());
    let help_text = String::from_utf8(help.stdout).unwrap();
    assert!(help_text.contains("scan-wallet-once"));
    assert!(help_text.contains("No transaction signing"));

    let version = run(&["version"]);
    assert!(version.status.success());
    assert!(String::from_utf8(version.stdout)
        .unwrap()
        .starts_with("engen-engine "));
}

#[test]
fn public_wallet_and_transaction_signature_validators_run_through_the_binary() {
    let wallet = run(&["validate-wallet", WALLET]);
    assert!(wallet.status.success());
    assert!(String::from_utf8(wallet.stdout)
        .unwrap()
        .contains(r#""kind":"public_solana_address""#));

    let signature = run(&["validate-signature", SIGNATURE]);
    assert!(signature.status.success());
    assert!(String::from_utf8(signature.stdout)
        .unwrap()
        .contains(r#""kind":"solana_transaction_signature""#));

    let invalid = run(&["validate-wallet", "not-a-wallet"]);
    assert_eq!(invalid.status.code(), Some(2));
}

#[test]
fn config_check_fails_closed_and_never_echoes_the_rpc_endpoint() {
    let output = Command::new(BIN)
        .arg("check-config")
        .env_clear()
        .env("ENGINE_MODE", "read_only")
        .env("ENGEN_RPC_URL", "https://rpc.example.invalid/private-path")
        .output()
        .expect("run CLI config check with an isolated environment");

    assert!(output.status.success());
    let stdout = String::from_utf8(output.stdout).unwrap();
    let report: serde_json::Value =
        serde_json::from_str(&stdout).expect("config command returns JSON");
    assert_eq!(report["engine_mode"], "read_only");
    assert_eq!(report["rpc_configured"], true);
    assert_eq!(report["live_trading_enabled"], false);
    assert_eq!(report["signing_available"], false);
    assert_eq!(report["broadcast_available"], false);
    assert!(!stdout.contains("private-path"));
}

#[test]
fn one_shot_scan_refuses_to_guess_or_open_a_database_path() {
    let output = Command::new(BIN)
        .args(["scan-wallet-once", "--wallet", WALLET])
        .env_clear()
        .env("ENGINE_MODE", "read_only")
        .output()
        .expect("run incomplete scan command");

    assert_eq!(output.status.code(), Some(2));
    let stderr = String::from_utf8(output.stderr).unwrap();
    assert!(stderr.contains("--signature-db is required"));
    assert!(!stderr.contains("data/signatures.db"));
}


#[test]
fn reviewed_label_evaluator_reports_metrics_for_reviewed_rows_only() {
    let directory = tempdir().unwrap();
    let csv = directory.path().join("reviewed-labels.csv");
    fs::write(
        &csv,
        "signature,expected_label,predicted_label,reviewed\n\\
         sig-1,BUY_CANDIDATE,BUY_CANDIDATE,true\n\\
         sig-2,TRANSFER,BUY_CANDIDATE,true\n\\
         sig-3,SELL_CANDIDATE,UNKNOWN,true\n\\
         sig-4,BUY_CANDIDATE,SELL_CANDIDATE,true\n\\
         ignored,,,false\n",
    )
    .unwrap();

    let output = Command::new(BIN)
        .args(["evaluate-transaction-labels", csv.to_str().unwrap()])
        .output()
        .expect("run the Rust reviewed-label evaluator");

    assert!(output.status.success());
    let stdout = String::from_utf8(output.stdout).unwrap();
    assert!(stdout.contains("Reviewed transactions: 4"));
    assert!(stdout.contains("Exact-label accuracy: 25.0%"));
    assert!(stdout.contains("False-buy count: 1"));
    assert!(stdout.contains("False-sell count: 1"));
    assert!(stdout.contains("BUY precision / recall: 50.0% / 50.0%"));
    assert!(stdout.contains("SELL precision / recall: 0.0% / 0.0%"));
    assert!(stdout.contains("Predicted UNKNOWN: 1 (25.0%)"));
}

#[test]
fn reviewed_label_evaluator_returns_one_when_no_rows_are_reviewed() {
    let directory = tempdir().unwrap();
    let csv = directory.path().join("empty-review.csv");
    fs::write(
        &csv,
        "signature,expected_label,predicted_label,reviewed\nignored,,,false\n",
    )
    .unwrap();

    let output = Command::new(BIN)
        .args(["evaluate-transaction-labels", csv.to_str().unwrap()])
        .output()
        .expect("run the Rust reviewed-label evaluator");

    assert_eq!(output.status.code(), Some(1));
    assert!(String::from_utf8(output.stdout)
        .unwrap()
        .contains("No reviewed rows found"));
}

#[test]
fn reviewed_label_evaluator_rejects_unsupported_labels() {
    let directory = tempdir().unwrap();
    let csv = directory.path().join("invalid-label.csv");
    fs::write(
        &csv,
        "signature,expected_label,predicted_label,reviewed\nsig-1,BUY_CANDIDATE,NOT_A_LABEL,true\n",
    )
    .unwrap();

    let output = Command::new(BIN)
        .args(["evaluate-transaction-labels", csv.to_str().unwrap()])
        .output()
        .expect("run the Rust reviewed-label evaluator");

    assert_eq!(output.status.code(), Some(2));
    assert!(String::from_utf8(output.stderr)
        .unwrap()
        .contains("Dataset error:"));
}
