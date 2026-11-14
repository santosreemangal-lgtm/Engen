//! Secret-safe console logging for the Rust runtime.
//!
//! Mirrors the legacy engine logger: one idempotent stdout handler, the
//! configured DEBUG/INFO/WARNING/ERROR threshold, a consistent timestamped
//! format, and redaction before any message is emitted.

use std::sync::atomic::{AtomicU8, Ordering};
use std::sync::OnceLock;

use chrono::Utc;
use log::{Level, LevelFilter, Log, Metadata, Record};

const ERROR: u8 = 1;
const WARNING: u8 = 2;
const INFO: u8 = 3;
const DEBUG: u8 = 4;

struct EngineLogger {
    max_level: AtomicU8,
}

impl EngineLogger {
    fn accepts(&self, level: Level) -> bool {
        let severity = match level {
            Level::Error => ERROR,
            Level::Warn => WARNING,
            Level::Info => INFO,
            Level::Debug => DEBUG,
            Level::Trace => DEBUG + 1,
        };
        severity <= self.max_level.load(Ordering::Relaxed)
    }
}

impl Log for EngineLogger {
    fn enabled(&self, metadata: &Metadata<'_>) -> bool {
        self.accepts(metadata.level())
    }

    fn log(&self, record: &Record<'_>) {
        if !self.enabled(record.metadata()) {
            return;
        }

        let message = redact(&format!("{}", record.args()));
        let timestamp = Utc::now().format("%Y-%m-%d %H:%M:%S%.3f");
        let level = match record.level() {
            Level::Error => "ERROR",
            Level::Warn => "WARNING",
            Level::Info => "INFO",
            Level::Debug => "DEBUG",
            Level::Trace => "TRACE",
        };
        println!(
            "{} | {:<7} | {} | {}",
            timestamp,
            level,
            record.target(),
            message
        );
    }

    fn flush(&self) {}
}

static LOGGER: EngineLogger = EngineLogger {
    max_level: AtomicU8::new(INFO),
};
static LOGGER_READY: OnceLock<()> = OnceLock::new();

/// Configure the single global engine logger. Safe to call repeatedly.
pub fn setup_logging(level: &str) -> Result<(), String> {
    let (threshold, filter) = match level.trim().to_ascii_uppercase().as_str() {
        "DEBUG" => (DEBUG, LevelFilter::Debug),
        "INFO" => (INFO, LevelFilter::Info),
        "WARNING" => (WARNING, LevelFilter::Warn),
        "ERROR" => (ERROR, LevelFilter::Error),
        _ => return Err("LOG_LEVEL must be DEBUG, INFO, WARNING, or ERROR.".to_string()),
    };

    LOGGER_READY.get_or_init(|| {
        // If another logger has already been installed by an embedding host,
        // do not replace it. The local CLI installs this logger first.
        let _ = log::set_logger(&LOGGER);
    });
    LOGGER.max_level.store(threshold, Ordering::Relaxed);
    log::set_max_level(filter);
    Ok(())
}

const SECRET_KEYS: &[&str] = &[
    "api_key",
    "api-key",
    "apikey",
    "token",
    "secret",
    "private_key",
    "private-key",
    "privatekey",
    "seed phrase",
    "seed_phrase",
    "seed-phrase",
    "seedphrase",
    "seed",
    "password",
];

fn is_word_byte(value: u8) -> bool {
    value.is_ascii_alphanumeric() || value == b'_'
}

/// Replace values assigned to secret-looking fields with [REDACTED].
///
/// Supported forms include api_key=value, Token: value, private-key=value,
/// seed phrase: value, and query-string token=value. The key and separator
/// remain visible so operators can identify which field was protected.
pub fn redact(text: &str) -> String {
    let lowered = text.to_ascii_lowercase();
    let bytes = text.as_bytes();
    let lowered_bytes = lowered.as_bytes();
    let mut output = String::with_capacity(text.len());
    let mut cursor = 0;

    while cursor < bytes.len() {
        let starts_at_word_boundary = cursor == 0 || !is_word_byte(bytes[cursor - 1]);
        let mut replacement_end = None;
        let mut value_start = None;

        if starts_at_word_boundary {
            for key in SECRET_KEYS {
                let key_bytes = key.as_bytes();
                let key_end = cursor + key_bytes.len();
                if key_end > bytes.len() || &lowered_bytes[cursor..key_end] != key_bytes {
                    continue;
                }

                let mut separator_end = key_end;
                while separator_end < bytes.len() && bytes[separator_end].is_ascii_whitespace() {
                    separator_end += 1;
                }

                if separator_end >= bytes.len() || !matches!(bytes[separator_end], b'=' | b':') {
                    continue;
                }

                separator_end += 1;
                while separator_end < bytes.len() && bytes[separator_end].is_ascii_whitespace() {
                    separator_end += 1;
                }

                let mut value_end = separator_end;
                while value_end < bytes.len() && !bytes[value_end].is_ascii_whitespace() {
                    value_end += 1;
                }

                if value_end > separator_end {
                    value_start = Some(separator_end);
                    replacement_end = Some(value_end);
                    break;
                }
            }
        }

        if let (Some(start), Some(end)) = (value_start, replacement_end) {
            output.push_str(&text[cursor..start]);
            output.push_str("[REDACTED]");
            cursor = end;
        } else {
            let character = text[cursor..]
                .chars()
                .next()
                .expect("cursor always remains on a UTF-8 boundary");
            output.push(character);
            cursor += character.len_utf8();
        }
    }

    output
}

#[cfg(test)]
mod tests {
    use super::{redact, EngineLogger, DEBUG, ERROR, INFO, WARNING};
    use log::Level;

    #[test]
    fn redacts_secret_assignment_variants_case_insensitively() {
        for (message, secret) in [
            ("api_key=abc123", "abc123"),
            ("Token: xyz789", "xyz789"),
            ("password = hunter2", "hunter2"),
            ("private-key=key-material", "key-material"),
            ("seed phrase: twelve-secret-words", "twelve-secret-words"),
            ("apikey: topsecret", "topsecret"),
        ] {
            let result = redact(message);
            assert!(!result.contains(secret), "secret leaked in {result}");
            assert!(result.contains("[REDACTED]"), "no redaction in {result}");
        }
    }

    #[test]
    fn redacts_query_string_tokens_and_multiple_secrets() {
        let input = "GET /api?token=TOPSECRET HTTP/1.1 api_key=ANOTHERSECRET";
        let result = redact(input);
        assert!(!result.contains("TOPSECRET"));
        assert!(!result.contains("ANOTHERSECRET"));
        assert_eq!(result.matches("[REDACTED]").count(), 2);
    }

    #[test]
    fn leaves_normal_text_and_unassigned_words_unchanged() {
        let input = "normal message, token bucket, password reset requested";
        assert_eq!(redact(input), input);
    }

    #[test]
    fn preserves_unicode_around_redacted_values() {
        let input = "সংযোগ ব্যর্থ token=SECRET — আবার চেষ্টা করুন";
        let result = redact(input);
        assert!(!result.contains("SECRET"));
        assert!(result.starts_with("সংযোগ ব্যর্থ token="));
        assert!(result.ends_with(" — আবার চেষ্টা করুন"));
    }

    #[test]
    fn configured_levels_match_legacy_debug_info_warning_error_order() {
        let logger = EngineLogger {
            max_level: std::sync::atomic::AtomicU8::new(INFO),
        };
        assert!(logger.accepts(Level::Error));
        assert!(logger.accepts(Level::Warn));
        assert!(logger.accepts(Level::Info));
        assert!(!logger.accepts(Level::Debug));
        logger
            .max_level
            .store(DEBUG, std::sync::atomic::Ordering::Relaxed);
        assert!(logger.accepts(Level::Debug));
        logger
            .max_level
            .store(WARNING, std::sync::atomic::Ordering::Relaxed);
        assert!(!logger.accepts(Level::Info));
        logger
            .max_level
            .store(ERROR, std::sync::atomic::Ordering::Relaxed);
        assert!(!logger.accepts(Level::Warn));
    }
}
