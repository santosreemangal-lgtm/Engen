//! Rust-native equivalents of the legacy Stage-1 health checks.
//!
//! Health checks validate the Rust production layout and fail closed when
//! configuration or safety invariants are not satisfied. They never access
//! user databases or make network requests.

use std::env;
use std::path::{Path, PathBuf};

use chrono::{SecondsFormat, Utc};
use serde_json::{json, Value};

use crate::config::Config;
use crate::safety::block_live_execution;

const REQUIRED_RUNTIME_FILES: &[&str] = &[
    "rust-engine/Cargo.toml",
    "rust-engine/src/main.rs",
    "rust-engine/src/lib.rs",
    "rust-engine/src/dashboard_server.rs",
    "rust-engine/src/monitor_service.rs",
    "rust-engine/src/transaction_processor.rs",
    "rust-engine/src/paper_simulator.rs",
    "dashboard/index.html",
    "dashboard/pending_copy_ui.js",
];

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CheckResult {
    pub name: String,
    pub passed: bool,
    pub detail: String,
}

impl CheckResult {
    fn as_json(&self) -> Value {
        json!({
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail
        })
    }
}

/// Resolve the application root without relying on a Python entry point.
/// The launcher sets ENGEN_APP_ROOT for hosted deployments; development and
/// CI fall back to the Rust crate's parent directory.
pub fn project_root() -> PathBuf {
    if let Some(root) = env::var_os("ENGEN_APP_ROOT") {
        let root = PathBuf::from(root);
        if !root.as_os_str().is_empty() {
            return root;
        }
    }

    if let Ok(current) = env::current_dir() {
        if current.join("rust-engine/Cargo.toml").is_file() {
            return current;
        }
    }

    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap_or_else(|| Path::new(env!("CARGO_MANIFEST_DIR")))
        .to_path_buf()
}

/// Evaluate the former Stage-1 checks against Rust runtime artifacts.
pub fn run_checks(config: &Config, root: &Path) -> Vec<CheckResult> {
    let missing: Vec<&str> = REQUIRED_RUNTIME_FILES
        .iter()
        .copied()
        .filter(|relative| !root.join(relative).is_file())
        .collect();

    let live_execution_blocked = block_live_execution().is_err();

    vec![
        CheckResult {
            name: "Required Rust production files exist".to_string(),
            passed: missing.is_empty(),
            detail: if missing.is_empty() {
                "all required Rust runtime files are present".to_string()
            } else {
                format!("missing: {}", missing.join(", "))
            },
        },
        CheckResult {
            name: "Engine mode is read_only".to_string(),
            passed: config.engine_mode == "read_only",
            detail: config.engine_mode.to_string(),
        },
        CheckResult {
            name: "Live trading is disabled".to_string(),
            passed: !config.live_trading_enabled,
            detail: config.live_trading_enabled.to_string(),
        },
        CheckResult {
            name: "Live execution is unconditionally blocked".to_string(),
            passed: live_execution_blocked,
            detail: live_execution_blocked.to_string(),
        },
    ]
}

/// Return a machine-readable health report suitable for a CLI or process probe.
pub fn get_status(config: &Config, root: &Path) -> Value {
    let checks = run_checks(config, root);
    let healthy = checks.iter().all(|check| check.passed);
    let emergency_stop_engaged = block_live_execution().is_err();

    json!({
        "status": if healthy { "ok" } else { "failed" },
        "stage": "rust_runtime",
        "engine_mode": config.engine_mode,
        "live_trading_enabled": config.live_trading_enabled,
        "emergency_stop_engaged": emergency_stop_engaged,
        "rust_engine_version": env!("CARGO_PKG_VERSION"),
        "time_utc": Utc::now().to_rfc3339_opts(SecondsFormat::Secs, true),
        "checks": checks.iter().map(CheckResult::as_json).collect::<Vec<_>>()
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;

    #[test]
    fn current_rust_application_layout_passes_health_checks() {
        let config = Config::from_map(&BTreeMap::new()).unwrap();
        let report = get_status(&config, &project_root());
        assert_eq!(report["status"], "ok");
        assert_eq!(report["engine_mode"], "read_only");
        assert_eq!(report["live_trading_enabled"], false);
        assert_eq!(report["emergency_stop_engaged"], true);
        assert!(report["checks"]
            .as_array()
            .unwrap()
            .iter()
            .all(|item| item["passed"] == true));
    }

    #[test]
    fn missing_runtime_files_are_reported_without_guessing() {
        let directory = tempfile::tempdir().unwrap();
        let config = Config::from_map(&BTreeMap::new()).unwrap();
        let checks = run_checks(&config, directory.path());
        let files = checks
            .iter()
            .find(|check| check.name == "Required Rust production files exist")
            .unwrap();
        assert!(!files.passed);
        assert!(files.detail.contains("rust-engine/Cargo.toml"));
        assert!(files.detail.contains("dashboard/index.html"));
    }

    #[test]
    fn report_never_advertises_live_execution() {
        let config = Config::from_map(&BTreeMap::new()).unwrap();
        let report = get_status(&config, &project_root());
        assert_eq!(report["live_trading_enabled"], false);
        assert_eq!(report["emergency_stop_engaged"], true);
    }
}
