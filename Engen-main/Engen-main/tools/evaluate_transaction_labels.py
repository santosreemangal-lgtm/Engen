"""Evaluate reviewed transaction-classification labels from a CSV file.

This tool is offline-only: it reads a local CSV and prints metrics. It does not
fetch blockchain data, submit transactions, or enable live trading.

CSV columns: signature,expected_label,predicted_label,reviewed
Only rows with reviewed=true are included in metrics.
"""
import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

LABELS = {
    "BUY_CANDIDATE",
    "SELL_CANDIDATE",
    "TRANSFER",
    "UNKNOWN",
    "FAILED_TRANSACTION",
    "RENT",
    "LIQUIDITY_OPERATION",
}


def truthy(value):
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def load_reviewed_rows(path):
    rows = []
    with Path(path).open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        required = {"signature", "expected_label", "predicted_label", "reviewed"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError(
                "CSV must contain columns: signature,expected_label,"
                "predicted_label,reviewed"
            )
        for line_number, row in enumerate(reader, start=2):
            if not truthy(row.get("reviewed", "")):
                continue
            expected = (row.get("expected_label") or "").strip().upper()
            predicted = (row.get("predicted_label") or "").strip().upper()
            if expected not in LABELS or predicted not in LABELS:
                raise ValueError(
                    f"Line {line_number}: unsupported label "
                    f"(expected={expected!r}, predicted={predicted!r})"
                )
            rows.append({
                "signature": (row.get("signature") or "").strip(),
                "expected": expected,
                "predicted": predicted,
            })
    return rows


def evaluate(rows):
    total = len(rows)
    correct = sum(row["expected"] == row["predicted"] for row in rows)
    matrix = Counter((row["expected"], row["predicted"]) for row in rows)
    false_buy = sum(
        row["predicted"] == "BUY_CANDIDATE"
        and row["expected"] != "BUY_CANDIDATE"
        for row in rows
    )
    false_sell = sum(
        row["predicted"] == "SELL_CANDIDATE"
        and row["expected"] != "SELL_CANDIDATE"
        for row in rows
    )
    predicted_buys = sum(row["predicted"] == "BUY_CANDIDATE" for row in rows)
    expected_buys = sum(row["expected"] == "BUY_CANDIDATE" for row in rows)
    predicted_sells = sum(row["predicted"] == "SELL_CANDIDATE" for row in rows)
    expected_sells = sum(row["expected"] == "SELL_CANDIDATE" for row in rows)
    unknown_count = sum(row["predicted"] == "UNKNOWN" for row in rows)

    def ratio(numerator, denominator):
        return numerator / denominator if denominator else None

    return {
        "total": total,
        "correct": correct,
        "accuracy": ratio(correct, total),
        "false_buy": false_buy,
        "false_sell": false_sell,
        "buy_precision": ratio(predicted_buys - false_buy, predicted_buys),
        "buy_recall": ratio(
            sum(row["expected"] == row["predicted"] == "BUY_CANDIDATE" for row in rows),
            expected_buys,
        ),
        "sell_precision": ratio(predicted_sells - false_sell, predicted_sells),
        "sell_recall": ratio(
            sum(row["expected"] == row["predicted"] == "SELL_CANDIDATE" for row in rows),
            expected_sells,
        ),
        "unknown_count": unknown_count,
        "unknown_rate": ratio(unknown_count, total),
        "matrix": matrix,
    }


def format_percent(value):
    return "N/A" if value is None else f"{value:.1%}"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", help="Path to reviewed-label CSV")
    args = parser.parse_args(argv)
    try:
        rows = load_reviewed_rows(args.csv_path)
    except (OSError, ValueError, csv.Error) as exc:
        print(f"Dataset error: {exc}", file=sys.stderr)
        return 2

    if not rows:
        print("No reviewed rows found; no error rate can be calculated.")
        return 1

    report = evaluate(rows)
    print(f"Reviewed transactions: {report['total']}")
    print(f"Exact-label accuracy: {format_percent(report['accuracy'])}")
    print(f"False-buy count: {report['false_buy']}")
    print(f"False-sell count: {report['false_sell']}")
    print(f"BUY precision / recall: {format_percent(report['buy_precision'])} / {format_percent(report['buy_recall'])}")
    print(f"SELL precision / recall: {format_percent(report['sell_precision'])} / {format_percent(report['sell_recall'])}")
    print(f"Predicted UNKNOWN: {report['unknown_count']} ({format_percent(report['unknown_rate'])})")
    print("Confusion matrix (expected -> predicted):")
    for expected in sorted(LABELS):
        cells = [
            f"{predicted}={report['matrix'][(expected, predicted)]}"
            for predicted in sorted(LABELS)
            if report["matrix"][(expected, predicted)]
        ]
        if cells:
            print(f"  {expected}: " + ", ".join(cells))
    print("Interpretation: metrics describe only reviewed rows and are not a guarantee of future accuracy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
