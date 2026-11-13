//! Strict environment configuration with safe-by-default execution policy.

use std::collections::BTreeMap;
use std::env;
use std::fmt;
use std::str::FromStr;

use bigdecimal::BigDecimal;
use num_traits::Zero;

use crate::paper_simulator::ExecutionPolicy;
use crate::rpc_policy::validate_rpc_url;

const LIVE_OFF_VALUES: &[&str] = &["", "false", "0", "no", "off"];
const MAX_RPC_RETRIES: u8 = 5;
const MAX_PAPER_FEE_BPS: u16 = 10_000;
const MAX_PAPER_NETWORK_FEE_USD: &str = "1000";

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
    /// Simulated exchange/trading fee, in basis points (100 bps = 1%).
    pub paper_trading_fee_bps: u16,
    /// Fixed simulated base network fee, in USD per fill.
    pub paper_network_fee_usd: String,
    /// Fixed simulated priority-fee estimate, in USD per fill.
    pub paper_priority_fee_usd: String,
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

        let paper_trading_fee_bps =
            parse_bps(values, "ENGEN_PAPER_TRADING_FEE_BPS", 10, &mut problems);
        let paper_network_fee_usd =
            parse_fee_usd(values, "ENGEN_PAPER_NETWORK_FEE_USD", "0", &mut problems);
        let paper_priority_fee_usd =
            parse_fee_usd(values, "ENGEN_PAPER_PRIORITY_FEE_USD", "0", &mut problems);
        let total_network_fee = BigDecimal::from_str(&paper_network_fee_usd)
            .unwrap_or_else(|_| BigDecimal::zero())
            + BigDecimal::from_str(&paper_priority_fee_usd).unwrap_or_else(|_| BigDecimal::zero());
        if total_network_fee > BigDecimal::from_str(MAX_PAPER_NETWORK_FEE_USD).unwrap() {
            problems.push(
                "Combined network and priority fees must not exceed 1000 USD per paper fill."
                    .to_string(),
            );
        }

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
            paper_trading_fee_bps,
            paper_network_fee_usd,
            paper_priority_fee_usd,
        })
    }

    /// Build the base paper execution policy. Per-profile settings may override
    /// these three fee values on the individual persisted copy order.
    pub fn paper_execution_policy(&self) -> ExecutionPolicy {
        ExecutionPolicy {
            fee_bps: self.paper_trading_fee_bps,
            network_cost: self.paper_network_fee_usd.clone(),
            priority_fee_usd: self.paper_priority_fee_usd.clone(),
            ..ExecutionPolicy::default()
        }
    }
}

fn parse_bps(
    values: &BTreeMap<String, String>,
    name: &str,
    default: u16,
    problems: &mut Vec<String>,
) -> u16 {
    let Some(raw) = values.get(name) else {
        return default;
    };

    match raw.trim().parse::<u16>() {
        Ok(value) if value <= MAX_PAPER_FEE_BPS => value,
        _ => {
            problems.push(format!(
                "{name} must be an integer between 0 and {MAX_PAPER_FEE_BPS} basis points."
            ));
            default
        }
    }
}

fn parse_fee_usd(
    values: &BTreeMap<String, String>,
    name: &str,
    default: &str,
    problems: &mut Vec<String>,
) -> String {
    let Some(raw) = values.get(name) else {
        return default.to_string();
    };

    match BigDecimal::from_str(raw.trim()) {
        Ok(value)
            if value >= BigDecimal::zero()
                && value <= BigDecimal::from_str(MAX_PAPER_NETWORK_FEE_USD).unwrap() =>
        {
            value.normalized().to_plain_string()
        }
        _ => {
            problems.push(format!(
                "{name} must be a non-negative decimal no greater than 1000 USD per paper fill."
            ));
            default.to_string()
        }
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

    #[test]
    fn paper_fee_config_defaults_and_builds_execution_policy() {
        let config = Config::from_map(&BTreeMap::new()).unwrap();
        assert_eq!(config.paper_trading_fee_bps, 10);
        assert_eq!(config.paper_network_fee_usd, "0");
        assert_eq!(config.paper_priority_fee_usd, "0");

        let policy = config.paper_execution_policy();
        assert_eq!(policy.fee_bps, 10);
        assert_eq!(policy.network_cost, "0");
        assert_eq!(policy.priority_fee_usd, "0");
    }

    #[test]
    fn paper_fee_config_accepts_explicit_global_values() {
        let config = Config::from_map(&map(&[
            ("ENGEN_PAPER_TRADING_FEE_BPS", "35"),
            ("ENGEN_PAPER_NETWORK_FEE_USD", "0.00075"),
            ("ENGEN_PAPER_PRIORITY_FEE_USD", "0.00425"),
        ]))
        .unwrap();

        let policy = config.paper_execution_policy();
        assert_eq!(policy.fee_bps, 35);
        assert_eq!(policy.network_cost, "0.00075");
        assert_eq!(policy.priority_fee_usd, "0.00425");
    }

    #[test]
    fn invalid_paper_fee_config_is_rejected() {
        for raw in ["-1", "10001", "10.5", "NaN", "not-an-integer"] {
            assert!(
                Config::from_map(&map(&[("ENGEN_PAPER_TRADING_FEE_BPS", raw)])).is_err(),
                "accepted invalid trading fee bps {raw}"
            );
        }
        for raw in ["-0.01", "1000.01", "NaN", "inf", "not-a-decimal"] {
            assert!(
                Config::from_map(&map(&[("ENGEN_PAPER_NETWORK_FEE_USD", raw)])).is_err(),
                "accepted invalid network fee {raw}"
            );
            assert!(
                Config::from_map(&map(&[("ENGEN_PAPER_PRIORITY_FEE_USD", raw)])).is_err(),
                "accepted invalid priority fee {raw}"
            );
        }
    }
}
