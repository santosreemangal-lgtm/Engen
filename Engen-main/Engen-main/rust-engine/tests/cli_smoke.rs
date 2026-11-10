use std::process::{Command, Output};

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
