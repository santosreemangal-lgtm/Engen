//! Offline evaluation of manually reviewed transaction-classification labels.
//!
//! This module reads a local CSV only. It does not fetch chain data, mutate a
//! trading ledger, submit transactions, or enable live execution.

use std::collections::BTreeMap;
use std::fs;
use std::path::Path;

pub const LABELS: [&str; 7] = [
    "BUY_CANDIDATE",
    "FAILED_TRANSACTION",
    "LIQUIDITY_OPERATION",
    "RENT",
    "SELL_CANDIDATE",
    "TRANSFER",
    "UNKNOWN",
];

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ReviewedRow {
    pub signature: String,
    pub expected: String,
    pub predicted: String,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Evaluation {
    pub total: usize,
    pub correct: usize,
    pub false_buy: usize,
    pub false_sell: usize,
    pub buy_precision: Option<f64>,
    pub buy_recall: Option<f64>,
    pub sell_precision: Option<f64>,
    pub sell_recall: Option<f64>,
    pub unknown_count: usize,
    pub unknown_rate: Option<f64>,
    pub accuracy: Option<f64>,
    pub matrix: BTreeMap<(String, String), usize>,
}

/// Load only rows explicitly marked reviewed=true/yes/y/1 (case-insensitive).
pub fn load_reviewed_rows(path: &Path) -> Result<Vec<ReviewedRow>, String> {
    let input = fs::read_to_string(path)
        .map_err(|error| format!("could not read CSV file: {error}"))?;
    let records = parse_csv_records(&input)?;
    let Some(header) = records.first() else {
        return Err(required_columns_error());
    };

    let mut columns = BTreeMap::new();
    for (index, name) in header.iter().enumerate() {
        // Match Python DictReader's last-value behavior for duplicate names.
        columns.insert(name.clone(), index);
    }
    for required in ["signature", "expected_label", "predicted_label", "reviewed"] {
        if !columns.contains_key(required) {
            return Err(required_columns_error());
        }
    }

    let mut rows = Vec::new();
    for (record_index, record) in records.iter().skip(1).enumerate() {
        let value = |name: &str| -> &str {
            columns
                .get(name)
                .and_then(|index| record.get(*index))
                .map(String::as_str)
                .unwrap_or("")
        };

        if !is_truthy(value("reviewed")) {
            continue;
        }

        let expected = value("expected_label").trim().to_ascii_uppercase();
        let predicted = value("predicted_label").trim().to_ascii_uppercase();
        if !LABELS.contains(&expected.as_str()) || !LABELS.contains(&predicted.as_str()) {
            return Err(format!(
                "Line {}: unsupported label (expected={expected:?}, predicted={predicted:?})",
                record_index + 2
            ));
        }

        rows.push(ReviewedRow {
            signature: value("signature").trim().to_string(),
            expected,
            predicted,
        });
    }

    Ok(rows)
}

fn required_columns_error() -> String {
    "CSV must contain columns: signature,expected_label,predicted_label,reviewed".to_string()
}

fn is_truthy(value: &str) -> bool {
    matches!(
        value.trim().to_ascii_lowercase().as_str(),
        "1" | "true" | "yes" | "y"
    )
}

/// Parse RFC-4180-style records, including quoted commas, escaped quotes and
/// newlines inside quoted fields. No third-party CSV dependency is required.
fn parse_csv_records(input: &str) -> Result<Vec<Vec<String>>, String> {
    let mut records = Vec::new();
    let mut record = Vec::new();
    let mut field = String::new();
    let mut characters = input.chars().peekable();
    let mut in_quotes = false;
    let mut after_quote = false;
    let mut record_started = false;

    while let Some(character) = characters.next() {
        if in_quotes {
            if character == '"' {
                if characters.peek() == Some(&'"') {
                    characters.next();
                    field.push('"');
                } else {
                    in_quotes = false;
                    after_quote = true;
                }
            } else {
                field.push(character);
            }
            continue;
        }

        if after_quote {
            match character {
                ',' => {
                    record.push(std::mem::take(&mut field));
                    after_quote = false;
                    record_started = true;
                }
                '\n' => {
                    record.push(std::mem::take(&mut field));
                    records.push(std::mem::take(&mut record));
                    after_quote = false;
                    record_started = false;
                }
                '\r' => {
                    if characters.peek() == Some(&'\n') {
                        characters.next();
                    }
                    record.push(std::mem::take(&mut field));
                    records.push(std::mem::take(&mut record));
                    after_quote = false;
                    record_started = false;
                }
                _ => return Err("Malformed CSV: expected a comma or record ending after a quoted field.".to_string()),
            }
            continue;
        }

        match character {
            '"' if field.is_empty() => {
                in_quotes = true;
                record_started = true;
            }
            '"' => return Err("Malformed CSV: quote inside an unquoted field.".to_string()),
            ',' => {
                record.push(std::mem::take(&mut field));
                record_started = true;
            }
            '\n' => {
                record.push(std::mem::take(&mut field));
                records.push(std::mem::take(&mut record));
                record_started = false;
            }
            '\r' => {
                if characters.peek() == Some(&'\n') {
                    characters.next();
                }
                record.push(std::mem::take(&mut field));
                records.push(std::mem::take(&mut record));
                record_started = false;
            }
            _ => {
                field.push(character);
                record_started = true;
            }
        }
    }

    if in_quotes {
        return Err("Malformed CSV: unterminated quoted field.".to_string());
    }
    if record_started || !record.is_empty() || !field.is_empty() || after_quote {
        record.push(field);
        records.push(record);
    }

    Ok(records)
}

pub fn evaluate(rows: &[ReviewedRow]) -> Evaluation {
    let total = rows.len();
    let correct = rows
        .iter()
        .filter(|row| row.expected == row.predicted)
        .count();
    let false_buy = rows
        .iter()
        .filter(|row| row.predicted == "BUY_CANDIDATE" && row.expected != "BUY_CANDIDATE")
        .count();
    let false_sell = rows
        .iter()
        .filter(|row| row.predicted == "SELL_CANDIDATE" && row.expected != "SELL_CANDIDATE")
        .count();
    let predicted_buys = rows
        .iter()
        .filter(|row| row.predicted == "BUY_CANDIDATE")
        .count();
    let expected_buys = rows
        .iter()
        .filter(|row| row.expected == "BUY_CANDIDATE")
        .count();
    let predicted_sells = rows
        .iter()
        .filter(|row| row.predicted == "SELL_CANDIDATE")
        .count();
    let expected_sells = rows
        .iter()
        .filter(|row| row.expected == "SELL_CANDIDATE")
        .count();
    let true_buys = rows
        .iter()
        .filter(|row| row.expected == "BUY_CANDIDATE" && row.predicted == "BUY_CANDIDATE")
        .count();
    let true_sells = rows
        .iter()
        .filter(|row| row.expected == "SELL_CANDIDATE" && row.predicted == "SELL_CANDIDATE")
        .count();
    let unknown_count = rows
        .iter()
        .filter(|row| row.predicted == "UNKNOWN")
        .count();

    let mut matrix = BTreeMap::new();
    for row in rows {
        *matrix
            .entry((row.expected.clone(), row.predicted.clone()))
            .or_insert(0) += 1;
    }

    Evaluation {
        total,
        correct,
        false_buy,
        false_sell,
        buy_precision: ratio(true_buys, predicted_buys),
        buy_recall: ratio(true_buys, expected_buys),
        sell_precision: ratio(true_sells, predicted_sells),
        sell_recall: ratio(true_sells, expected_sells),
        unknown_count,
        unknown_rate: ratio(unknown_count, total),
        accuracy: ratio(correct, total),
        matrix,
    }
}

fn ratio(numerator: usize, denominator: usize) -> Option<f64> {
    (denominator != 0).then(|| numerator as f64 / denominator as f64)
}

fn format_percent(value: Option<f64>) -> String {
    value
        .map(|ratio| format!("{:.1}%", ratio * 100.0))
        .unwrap_or_else(|| "N/A".to_string())
}

pub fn format_report(report: &Evaluation) -> String {
    let mut output = format!(
        "Reviewed transactions: {}\n\
Exact-label accuracy: {}\n\
False-buy count: {}\n\
False-sell count: {}\n\
BUY precision / recall: {} / {}\n\
SELL precision / recall: {} / {}\n\
Predicted UNKNOWN: {} ({})\n\
Confusion matrix (expected -> predicted):\n",
        report.total,
        format_percent(report.accuracy),
        report.false_buy,
        report.false_sell,
        format_percent(report.buy_precision),
        format_percent(report.buy_recall),
        format_percent(report.sell_precision),
        format_percent(report.sell_recall),
        report.unknown_count,
        format_percent(report.unknown_rate),
    );

    for expected in LABELS {
        let cells: Vec<String> = LABELS
            .iter()
            .filter_map(|predicted| {
                report
                    .matrix
                    .get(&(expected.to_string(), (*predicted).to_string()))
                    .map(|count| format!("{predicted}={count}"))
            })
            .collect();
        if !cells.is_empty() {
            output.push_str(&format!("  {expected}: {}\n", cells.join(", ")));
        }
    }
    output.push_str(
        "Interpretation: metrics describe only reviewed rows and are not a guarantee of future accuracy.\n",
    );
    output
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    fn row(expected: &str, predicted: &str) -> ReviewedRow {
        ReviewedRow {
            signature: "sig".to_string(),
            expected: expected.to_string(),
            predicted: predicted.to_string(),
        }
    }

    #[test]
    fn counts_false_buy_false_sell_and_unknown_rate() {
        let rows = vec![
            row("BUY_CANDIDATE", "BUY_CANDIDATE"),
            row("TRANSFER", "BUY_CANDIDATE"),
            row("SELL_CANDIDATE", "UNKNOWN"),
            row("BUY_CANDIDATE", "SELL_CANDIDATE"),
        ];

        let report = evaluate(&rows);

        assert_eq!(report.total, 4);
        assert_eq!(report.correct, 1);
        assert_eq!(report.false_buy, 1);
        assert_eq!(report.false_sell, 1);
        assert_eq!(report.unknown_count, 1);
        assert_eq!(report.unknown_rate, Some(0.25));
        assert_eq!(report.buy_precision, Some(0.5));
        assert_eq!(report.buy_recall, Some(0.5));
        assert_eq!(report.sell_precision, Some(0.0));
        assert_eq!(report.sell_recall, Some(0.0));
    }

    #[test]
    fn loads_only_reviewed_rows_and_parses_quoted_csv_fields() {
        let directory = tempdir().unwrap();
        let path = directory.path().join("labels.csv");
        fs::write(
            &path,
            "signature,expected_label,predicted_label,reviewed,notes\r\n\
\"sig,1\",BUY_CANDIDATE,BUY_CANDIDATE,yes,\"comma, and \"\"quote\"\" inside\"\r\n\
ignored,,,false,not-reviewed\r\n",
        )
        .unwrap();

        let rows = load_reviewed_rows(&path).unwrap();

        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].signature, "sig,1");
        assert_eq!(rows[0].expected, "BUY_CANDIDATE");
        assert_eq!(rows[0].predicted, "BUY_CANDIDATE");
    }

    #[test]
    fn rejects_missing_columns_and_unsupported_reviewed_labels() {
        let directory = tempdir().unwrap();
        let missing = directory.path().join("missing.csv");
        fs::write(&missing, "signature,expected_label\nsig,BUY_CANDIDATE\n").unwrap();
        assert!(load_reviewed_rows(&missing)
            .unwrap_err()
            .contains("CSV must contain columns"));

        let invalid = directory.path().join("invalid.csv");
        fs::write(
            &invalid,
            "signature,expected_label,predicted_label,reviewed\nsig,BUY_CANDIDATE,MADE_UP,true\n",
        )
        .unwrap();
        assert!(load_reviewed_rows(&invalid)
            .unwrap_err()
            .contains("unsupported label"));
    }

    #[test]
    fn rejects_unterminated_quoted_fields() {
        assert!(parse_csv_records("a,b\n\"unfinished,value\n")
            .unwrap_err()
            .contains("unterminated"));
    }

    #[test]
    fn empty_dataset_has_no_defined_rates() {
        let report = evaluate(&[]);
        assert_eq!(report.total, 0);
        assert_eq!(report.accuracy, None);
        assert_eq!(report.buy_precision, None);
        assert_eq!(report.unknown_rate, None);
    }
}
