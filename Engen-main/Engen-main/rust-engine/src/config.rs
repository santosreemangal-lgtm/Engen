use std::collections::HashMap;

use serde::Serialize;
use thiserror::Error;

const LIVE_OFF_VALUES: &[&str] = &["", "false", "0", "no", "off"];

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

pub fn load_config(env: &HashMap<String, String>) -> Result<Config, ConfigError> {
    let mode = env.get("ENGINE_MODE").map(|v| v.trim().to_ascii_lowercase())
        .filter(|v| !v.is_empty()).unwrap_or_else(|| "read_only".to_owned());
    if mode != "read_only" {
        return Err(ConfigError::Invalid(
            "ENGINE_MODE must be read_only; live execution is not supported".into(),
        ));
    }

    let level = env.get("LOG_LEVEL").map(|v| v.trim().to_ascii_uppercase())
        .filter(|v| !v.is_empty()).unwrap_or_else(|| "INFO".to_owned());
    if !["DEBUG", "INFO", "WARNING", "ERROR"].contains(&level.as_str()) {
        return Err(ConfigError::Invalid("LOG_LEVEL is invalid".into()));
    }

    let live = env.get("LIVE_TRADING_ENABLED").map(|v| v.trim().to_ascii_lowercase())
        .unwrap_or_else(|| "false".to_owned());
    if !LIVE_OFF_VALUES.contains(&live.as_str()) {
        return Err(ConfigError::Invalid(
            "Live trading is not available; LIVE_TRADING_ENABLED must be false".into(),
        ));
    }

    Ok(Config {
        engine_mode: mode,
        log_level: level,
        live_trading_enabled: false,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_to_read_only() {
        let config = load_config(&HashMap::new()).unwrap();
        assert_eq!(config.engine_mode, "read_only");
        assert!(!config.live_trading_enabled);
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
}
