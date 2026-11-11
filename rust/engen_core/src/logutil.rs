//! Minimal stderr logger. No external log crate required.

use std::io::{self, Write};
use std::time::{SystemTime, UNIX_EPOCH};

pub fn info(message: &str) {
    emit("INFO", message);
}

pub fn warn(message: &str) {
    emit("WARN", message);
}

fn emit(level: &str, message: &str) {
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let _ = writeln!(io::stderr(), "{secs} {level} {message}");
}
