//! Strict environment configuration with safe-by-default execution policy.

use std::collections::BTreeMap;
use std::env;
use std::fmt;

use crate::rpc_policy::validate_rpc_url;

const LIVE_OFF_VALUES: &[&str] = &["", "false", "0", "no", "off"];
const MAX_RPC_RETRIES: u8 = 5;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LogLevel {
    Debug,
    Info,
    Warning,
    Error,
}

impl LogLevel {
    fn parse(value: &str) -> Option<Self> {
        match value.to_ascii_uppercase().as_str() {
            "DEBUG" => Some(Self::Debug),
            "INFO" => Some(Self::Info),
            "WARNING" => Some(Self::Warning),
            "ERROR" => Some(Self::Error),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct Config {
    pub engine_mode: &'static str,
    pub log_level: LogLevel,
    /// Always false. There is no code path to enable live execution.
    pub live_trading_enabled: bool,
    pub rpc_url: Option<String>,
    pub rpc_timeout_seconds: f64,
    pub rpc_retries: u8,
    pub rpc_backoff_seconds: f64,
    pub rpc_min_interval_seconds: f64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConfigError {
    pub problems: Vec<String>,
}

impl fmt::Display for ConfigError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        for (index, problem) in self.problems.iter().enumerate() {
            if index > 0 {
                writeln!(f)?;
            }
            write!(f, "- {problem}")?;
        }
        Ok(())
    }
}

impl std::error::Error for ConfigError {}

impl Config {
    pub fn from_process_env() -> Result<Self, ConfigError> {
        let values = env::vars().collect::<BTreeMap<_, _>>();
        Self::from_map(&values)
    }

    /// Parse a supplied environment map. This is also the deterministic test entrypoint.
    pub fn from_map(values: &BTreeMap<String, String>) -> Result<Self, ConfigError> {
        let mut problems = Vec::new();

        let mode = read(values, "ENGINE_MODE", "read_only").to_ascii_lowercase();
        if mode != "read_only" {
            problems.push(
                "ENGINE_MODE must be read_only; this release does not support other modes."
                    .to_string(),
            );
        }

        let raw_level = read(values, "LOG_LEVEL", "INFO");
        let log_level = match LogLevel::parse(&raw_level) {
            Some(level) => level,
            None => {
                problems
                    .push("LOG_LEVEL must be one of DEBUG, INFO, WARNING, or ERROR.".to_string());
                LogLevel::Info
            }
        };

        let live = read(values, "LIVE_TRADING_ENABLED", "false").to_ascii_lowercase();
        if !LIVE_OFF_VALUES.contains(&live.as_str()) {
            problems.push(
                "Live trading is unavailable. Remove LIVE_TRADING_ENABLED or set it to false."
                    .to_string(),
            );
        }

        let rpc_url = values
            .get("ENGEN_RPC_URL")
            .map(|raw| raw.trim().to_string())
            .filter(|raw| !raw.is_empty());

        if let Some(url) = rpc_url.as_deref() {
            if let Err(message) = validate_rpc_url(url) {
                problems.push(message);
            }
        }

        let rpc_timeout_seconds = parse_float(
            values,
            "ENGEN_RPC_TIMEOUT_SECONDS",
            10.0,
            0.0,
            60.0,
            true,
            &mut problems,
        );
        let rpc_backoff_seconds = parse_float(
            values,
            "ENGEN_RPC_BACKOFF_SECONDS",
            0.5,
            0.0,
            60.0,
            false,
            &mut problems,
        );
        let rpc_min_interval_seconds = parse_float(
            values,
            "ENGEN_RPC_MIN_INTERVAL_SECONDS",
            0.1,
            0.0,
            60.0,
            false,
            &mut problems,
        );

        let rpc_retries = match values.get("ENGEN_RPC_RETRIES") {
            None => 2,
            Some(raw) => match raw.trim().parse::<u64>() {
                Ok(value) => value.min(u64::from(MAX_RPC_RETRIES)) as u8,
                Err(_) => {
                    problems.push("ENGEN_RPC_RETRIES must be a non-negative integer.".to_string());
                    2
                }
            },
        };

        if !problems.is_empty() {
            return Err(ConfigError { problems });
        }

        Ok(Self {
            engine_mode: "read_only",
            log_level,
            live_trading_enabled: false,
            rpc_url,
            rpc_timeout_seconds,
            rpc_retries,
            rpc_backoff_seconds,
            rpc_min_interval_seconds,
        })
    }
}

fn read(values: &BTreeMap<String, String>, name: &str, default: &str) -> String {
    values
        .get(name)
        .map(|value| value.trim().to_string())
        .unwrap_or_else(|| default.to_string())
}

fn parse_float(
    values: &BTreeMap<String, String>,
    name: &str,
    default: f64,
    min: f64,
    max: f64,
    min_exclusive: bool,
    problems: &mut Vec<String>,
) -> f64 {
    let Some(raw) = values.get(name) else {
        return default;
    };

    match raw.trim().parse::<f64>() {
        Ok(value)
            if value.is_finite()
                && if min_exclusive {
                    value > min && value <= max
                } else {
                    value >= min && value <= max
                } =>
        {
            value
        }
        _ => {
            let relation = if min_exclusive {
                "greater than"
            } else {
                "at least"
            };
            problems.push(format!(
                "{name} must be a finite number {relation} {min} and no greater than {max}."
            ));
            default
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn map(items: &[(&str, &str)]) -> BTreeMap<String, String> {
        items
            .iter()
            .map(|(key, value)| (key.to_string(), value.to_string()))
            .collect()
    }

    #[test]
    fn safe_defaults_are_read_only() {
        let config = Config::from_map(&BTreeMap::new()).unwrap();
        assert_eq!(config.engine_mode, "read_only");
        assert!(!config.live_trading_enabled);
        assert_eq!(config.rpc_retries, 2);
    }

    #[test]
    fn rejects_unsupported_modes_and_live_flag() {
        let values = map(&[("ENGINE_MODE", "live"), ("LIVE_TRADING_ENABLED", "true")]);
        let error = Config::from_map(&values).unwrap_err();
        assert_eq!(error.problems.len(), 2);
    }

    #[test]
    fn accepts_explicit_false_spellings() {
        for value in ["false", "0", "no", "off", ""] {
            let values = map(&[("LIVE_TRADING_ENABLED", value)]);
            assert!(!Config::from_map(&values).unwrap().live_trading_enabled);
        }
    }

    #[test]
    fn bounds_rpc_retries_and_timeout() {
        let values = map(&[
            ("ENGEN_RPC_RETRIES", "99"),
            ("ENGEN_RPC_TIMEOUT_SECONDS", "5"),
        ]);
        let config = Config::from_map(&values).unwrap();
        assert_eq!(config.rpc_retries, MAX_RPC_RETRIES);
        assert_eq!(config.rpc_timeout_seconds, 5.0);
    }

    #[test]
    fn rejects_invalid_timeout() {
        let values = map(&[("ENGEN_RPC_TIMEOUT_SECONDS", "NaN")]);
        assert!(Config::from_map(&values).is_err());
    }
}
