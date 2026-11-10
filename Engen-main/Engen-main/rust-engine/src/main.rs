//! Read-only Engen Rust CLI bootstrap.
//!
//! The CLI intentionally exposes no transaction signing, order construction for live
//! execution, transaction broadcast, private-key, or seed-phrase capability.

use std::error::Error;
use std::fs;
use std::io;
use std::path::PathBuf;
use std::time::Duration;

use engen_engine::config::Config;
use engen_engine::signature_store::SignatureStore;
use engen_engine::solana_public::{RpcConfig, SolanaRpcClient};
use engen_engine::solana_validation::{
    is_valid_solana_address, is_valid_transaction_signature,
};
use engen_engine::transaction_analysis::classify_transaction;
use engen_engine::transaction_monitor::monitor_wallet;
use serde_json::{json, Value};

const DEFAULT_SCAN_LIMIT: usize = 25;
const MAX_SCAN_LIMIT: usize = 1_000;

#[derive(Debug, PartialEq, Eq)]
struct ScanOptions {
    wallet: String,
    signature_db: PathBuf,
    limit: usize,
}

fn invalid_input(message: impl Into<String>) -> Box<dyn Error> {
    Box::new(io::Error::new(io::ErrorKind::InvalidInput, message.into()))
}

fn help_text() -> &'static str {
    "Engen Rust CLI — read-only / paper-only\n\
\n\
USAGE:\n\
  engen --help\n\
  engen version\n\
  engen check-config\n\
  engen validate-wallet <PUBLIC_ADDRESS>\n\
  engen validate-signature <TRANSACTION_SIGNATURE>\n\
  engen classify-transaction <PUBLIC_WALLET> <TRANSACTION_JSON_FILE>\n\
  engen scan-wallet-once --wallet <PUBLIC_ADDRESS> --signature-db <EXPLICIT_DB_PATH> [--limit <1..1000>]\n\
\n\
ENVIRONMENT:\n\
  ENGEN_RPC_URL                       Optional read-only HTTPS Solana RPC endpoint\n\
  ENGEN_RPC_TIMEOUT_SECONDS           Request timeout (default 10)\n\
  ENGEN_RPC_RETRIES                   Bounded retries (default 2, max 5)\n\
  ENGEN_RPC_BACKOFF_SECONDS           Retry backoff (default 0.5)\n\
  ENGEN_RPC_MIN_INTERVAL_SECONDS      Request pacing (default 0.1)\n\
  ENGINE_MODE                         Must be read_only\n\
  LIVE_TRADING_ENABLED                Must be absent or false\n\
\n\
SAFETY:\n\
  No transaction signing, private-key/seed handling, or transaction broadcasting exists.\n\
  scan-wallet-once requires an explicit SQLite path; it never guesses or opens data/ databases.\n\
  The first scan initializes a public-wallet baseline and normally returns no transactions.\n\
"
}

fn parse_scan_options(args: &[String]) -> Result<ScanOptions, Box<dyn Error>> {
    let mut wallet = None;
    let mut signature_db = None;
    let mut limit = DEFAULT_SCAN_LIMIT;
    let mut index = 0;

    while index < args.len() {
        let flag = args[index].as_str();
        let value = args
            .get(index + 1)
            .ok_or_else(|| invalid_input(format!("Missing value for {flag}.")))?;
        match flag {
            "--wallet" => wallet = Some(value.trim().to_string()),
            "--signature-db" => {
                let path = PathBuf::from(value);
                if path.as_os_str().is_empty() {
                    return Err(invalid_input("The explicit signature database path is required."));
                }
                signature_db = Some(path);
            }
            "--limit" => {
                limit = value.parse::<usize>().map_err(|_| {
                    invalid_input("The scan limit must be an integer between 1 and 1000.")
                })?;
                if !(1..=MAX_SCAN_LIMIT).contains(&limit) {
                    return Err(invalid_input(
                        "The scan limit must be an integer between 1 and 1000.",
                    ));
                }
            }
            _ => return Err(invalid_input(format!("Unknown scan option: {flag}."))),
        }
        index += 2;
    }

    let wallet = wallet.ok_or_else(|| invalid_input("--wallet is required."))?;
    if !is_valid_solana_address(&wallet) {
        return Err(invalid_input("Invalid Solana public wallet address."));
    }
    let signature_db =
        signature_db.ok_or_else(|| invalid_input("--signature-db is required; no default data path is used."))?;

    Ok(ScanOptions {
        wallet,
        signature_db,
        limit,
    })
}

fn run_scan(options: ScanOptions) -> Result<(), Box<dyn Error>> {
    let config = Config::from_process_env()?;
    let endpoint = config
        .rpc_url
        .clone()
        .ok_or_else(|| invalid_input("ENGEN_RPC_URL is required for scan-wallet-once."))?;

    let client = SolanaRpcClient::new(RpcConfig {
        endpoint,
        timeout: Duration::from_secs_f64(config.rpc_timeout_seconds),
        retries: config.rpc_retries,
        backoff: Duration::from_secs_f64(config.rpc_backoff_seconds),
        min_interval: Duration::from_secs_f64(config.rpc_min_interval_seconds),
    })?;

    let store = SignatureStore::open(&options.signature_db)?;
    let results = monitor_wallet(
        &client,
        &store,
        &options.wallet,
        options.limit,
        false,
        None,
    )?;

    let transaction_count = results.len();
    let output = json!({
        "engine_mode": "read_only",
        "monitoring": "one_shot",
        "wallet": options.wallet,
        "signature_database": options.signature_db,
        "scan_limit": options.limit,
        "transactions": results,
        "transaction_count": transaction_count,
        "paper_execution_enabled": false,
        "live_trading_enabled": false,
        "transaction_broadcast": false
    });
    println!("{}", serde_json::to_string_pretty(&output)?);
    Ok(())
}

fn run_classification(args: &[String]) -> Result<(), Box<dyn Error>> {
    if args.len() != 2 {
        return Err(invalid_input(
            "classify-transaction requires a wallet address and one transaction JSON file.",
        ));
    }
    let wallet = args[0].trim();
    if !is_valid_solana_address(wallet) {
        return Err(invalid_input("Invalid Solana public wallet address."));
    }
    let payload = fs::read_to_string(&args[1])?;
    let transaction: Value = serde_json::from_str(&payload)?;
    let classification = classify_transaction(&transaction, wallet, None);
    println!("{}", serde_json::to_string_pretty(&classification)?);
    Ok(())
}

fn run(args: Vec<String>) -> Result<(), Box<dyn Error>> {
    let Some(command) = args.first().map(String::as_str) else {
        println!("{}", help_text());
        return Ok(());
    };

    match command {
        "--help" | "-h" | "help" => {
            println!("{}", help_text());
            Ok(())
        }
        "version" | "--version" => {
            println!("engen-engine {}", env!("CARGO_PKG_VERSION"));
            Ok(())
        }
        "check-config" => {
            let config = Config::from_process_env()?;
            let output = json!({
                "engine_mode": config.engine_mode,
                "log_level": format!("{:?}", config.log_level),
                "rpc_configured": config.rpc_url.is_some(),
                "rpc_timeout_seconds": config.rpc_timeout_seconds,
                "rpc_retries": config.rpc_retries,
                "live_trading_enabled": false,
                "signing_available": false,
                "broadcast_available": false
            });
            println!("{}", serde_json::to_string_pretty(&output)?);
            Ok(())
        }
        "validate-wallet" => {
            if args.len() != 2 {
                return Err(invalid_input("validate-wallet requires one public address."));
            }
            if !is_valid_solana_address(&args[1]) {
                return Err(invalid_input("Invalid Solana public wallet address."));
            }
            println!("{}", json!({"valid":true,"kind":"public_solana_address"}));
            Ok(())
        }
        "validate-signature" => {
            if args.len() != 2 {
                return Err(invalid_input("validate-signature requires one transaction signature."));
            }
            if !is_valid_transaction_signature(&args[1]) {
                return Err(invalid_input("Invalid Solana transaction signature."));
            }
            println!("{}", json!({"valid":true,"kind":"solana_transaction_signature"}));
            Ok(())
        }
        "classify-transaction" => run_classification(&args[1..]),
        "scan-wallet-once" => run_scan(parse_scan_options(&args[1..])?),
        _ => Err(invalid_input(format!(
            "Unknown command: {command}. Use --help to see supported commands."
        ))),
    }
}

fn main() {
    if let Err(error) = run(std::env::args().skip(1).collect()) {
        eprintln!("engen: {error}");
        std::process::exit(2);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scan_requires_explicit_database_and_valid_wallet() {
        let wallet = "11111111111111111111111111111111".to_string();
        let options = parse_scan_options(&[
            "--wallet".to_string(),
            wallet.clone(),
            "--signature-db".to_string(),
            "temporary/signatures.db".to_string(),
        ])
        .unwrap();
        assert_eq!(
            options,
            ScanOptions {
                wallet,
                signature_db: PathBuf::from("temporary/signatures.db"),
                limit: DEFAULT_SCAN_LIMIT
            }
        );
    }

    #[test]
    fn scan_rejects_implicit_database_path_and_invalid_limits() {
        let wallet = "11111111111111111111111111111111".to_string();
        assert!(parse_scan_options(&["--wallet".into(), wallet.clone()]).is_err());
        assert!(parse_scan_options(&[
            "--wallet".into(),
            wallet.clone(),
            "--signature-db".into(),
            "temporary/signatures.db".into(),
            "--limit".into(),
            "1001".into()
        ])
        .is_err());
        assert!(parse_scan_options(&[
            "--wallet".into(),
            "not-a-wallet".into(),
            "--signature-db".into(),
            "temporary/signatures.db".into()
        ])
        .is_err());
    }

    #[test]
    fn config_status_does_not_expose_rpc_endpoint_or_enable_live_capabilities() {
        let config = Config::from_map(&Default::default()).unwrap();
        assert_eq!(config.engine_mode, "read_only");
        assert!(!config.live_trading_enabled);
    }
}
