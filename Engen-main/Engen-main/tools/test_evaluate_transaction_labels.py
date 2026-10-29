import csv
import tempfile
import unittest
from pathlib import Path

from evaluate_transaction_labels import evaluate, load_reviewed_rows


class TransactionLabelEvaluationTests(unittest.TestCase):
    def test_counts_false_buy_false_sell_and_unknown_rate(self):
        rows = [
            {"expected": "BUY_CANDIDATE", "predicted": "BUY_CANDIDATE", "signature": "a"},
            {"expected": "TRANSFER", "predicted": "BUY_CANDIDATE", "signature": "b"},
            {"expected": "SELL_CANDIDATE", "predicted": "UNKNOWN", "signature": "c"},
            {"expected": "BUY_CANDIDATE", "predicted": "SELL_CANDIDATE", "signature": "d"},
        ]

        result = evaluate(rows)

        self.assertEqual(result["total"], 4)
        self.assertEqual(result["correct"], 1)
        self.assertEqual(result["false_buy"], 1)
        self.assertEqual(result["false_sell"], 1)
        self.assertEqual(result["unknown_count"], 1)
        self.assertEqual(result["unknown_rate"], 0.25)

    def test_only_reviewed_rows_are_loaded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "labels.csv"
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "signature", "expected_label", "predicted_label",
                        "reviewed", "notes",
                    ],
                )
                writer.writeheader()
                writer.writerow({
                    "signature": "sig-reviewed",
                    "expected_label": "BUY_CANDIDATE",
                    "predicted_label": "BUY_CANDIDATE",
                    "reviewed": "true",
                    "notes": "reviewed",
                })
                writer.writerow({
                    "signature": "sig-unreviewed",
                    "expected_label": "",
                    "predicted_label": "UNKNOWN",
                    "reviewed": "false",
                    "notes": "not reviewed",
                })

            rows = load_reviewed_rows(path)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["signature"], "sig-reviewed")

    def test_rejects_unknown_label(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "labels.csv"
            path.write_text(
                "signature,expected_label,predicted_label,reviewed\n"
                "sig-1,BUY_CANDIDATE,MADE_UP,true\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_reviewed_rows(path)


if __name__ == "__main__":
    unittest.main()
