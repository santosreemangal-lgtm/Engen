//! Environment configuration validation. This stage is permanently read-only.

use std::{collections::HashMap, env};

use serde::Serialize;
use thiserror::Error;

const LIVE_OFF_VALUES: &[&str] = &["", "false", "0", "no", "off"];
const VALID_LOG_LEVELS: &[&str] = &["DEBUG", "INFO", "WARNING", "ERROR"];

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct Config {
    pub engine_mode: String,
    pub log_level: String,
    pub live_trading_enabled: bool,
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum ConfigError {
    #[error("{0}")]
    Invalid(String),
}

impl Config {
    /// JSON-compatible equivalent of the Python Config.as_dict().
    pub fn as_json(&self) -> serde_json::Value {
        serde_json::json!({
            "engine_mode": self.engine_mode,
            "log_level": self.log_level,
            "live_trading_enabled": self.live_trading_enabled
        })
    }
}

fn read(env: &HashMap<String, String>, name: &str, default: &str) -> String {
    env.get(name).map(|value| value.trim().to_owned()).unwrap_or_else(|| default.to_owned())
}

/// Validate a supplied environment snapshot. All invalid settings are reported together.
pub fn load_config(env: &HashMap<String, String>) -> Result<Config, ConfigError> {
    let mode = read(env, "ENGINE_MODE", "read_only").to_ascii_lowercase();
    let mode = if mode.is_empty() { "read_only".to_owned() } else { mode };

    let level = read(env, "LOG_LEVEL", "INFO").to_ascii_uppercase();
    let level = if level.is_empty() { "INFO".to_owned() } else { level };

    let live = read(env, "LIVE_TRADING_ENABLED", "false").to_ascii_lowercase();

    let mut problems = Vec::new();
    if mode != "read_only" {
        problems.push("ENGINE_MODE must be read_only; live execution is not supported".to_owned());
    }
    if !VALID_LOG_LEVELS.contains(&level.as_str()) {
        problems.push("LOG_LEVEL must be one of DEBUG, INFO, WARNING, ERROR".to_owned());
    }
    if !LIVE_OFF_VALUES.contains(&live.as_str()) {
        problems.push("Live trading is not available; remove LIVE_TRADING_ENABLED or set it to false".to_owned());
    }
    if !problems.is_empty() {
        return Err(ConfigError::Invalid(problems.into_iter().map(|p| format!("- {p}")).collect::<Vec<_>>().join("\n")));
    }

    Ok(Config { engine_mode: mode, log_level: level, live_trading_enabled: false })
}

/// Load and validate the current process environment.
pub fn load_config_from_env() -> Result<Config, ConfigError> {
    let snapshot: HashMap<String, String> = env::vars().collect();
    load_config(&snapshot)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_to_read_only() {
        let config = load_config(&HashMap::new()).unwrap();
        assert_eq!(config.engine_mode, "read_only");
        assert_eq!(config.log_level, "INFO");
        assert!(!config.live_trading_enabled);
        assert_eq!(config.as_json()["engine_mode"], "read_only");
    }

    #[test]
    fn accepts_case_and_whitespace_like_python() {
        let env = HashMap::from([
            ("ENGINE_MODE".into(), " READ_ONLY ".into()),
            ("LOG_LEVEL".into(), " warning ".into()),
            ("LIVE_TRADING_ENABLED".into(), " OFF ".into()),
        ]);
        let config = load_config(&env).unwrap();
        assert_eq!(config.log_level, "WARNING");
    }

    #[test]
    fn rejects_live_mode() {
        let env = HashMap::from([("ENGINE_MODE".into(), "live".into())]);
        assert!(load_config(&env).is_err());
    }

    #[test]
    fn rejects_live_toggle() {
        let env = HashMap::from([("LIVE_TRADING_ENABLED".into(), "true".into())]);
        assert!(load_config(&env).is_err());
    }

    #[test]
    fn reports_multiple_invalid_settings_together() {
        let env = HashMap::from([
            ("ENGINE_MODE".into(), "live".into()),
            ("LOG_LEVEL".into(), "TRACE".into()),
            ("LIVE_TRADING_ENABLED".into(), "true".into()),
        ]);
        let error = load_config(&env).unwrap_err().to_string();
        assert!(error.contains("ENGINE_MODE"));
        assert!(error.contains("LOG_LEVEL"));
        assert!(error.contains("Live trading"));
    }
}
