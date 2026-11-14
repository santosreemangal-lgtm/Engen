//! Secret-safe logging helpers for the Rust runtime.
//!
//! This module deliberately avoids logging configuration that could expose
//! environment values. Callers redact untrusted error text before writing it.

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
    use super::redact;

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
}
