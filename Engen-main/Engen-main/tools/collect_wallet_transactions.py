"""Collect a bounded sample of public Solana transactions for manual review.

Read-only only. Writes raw transaction evidence and predicted labels for review;
it never treats its own prediction as ground truth and never submits transactions.
"""
import csv
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# When executed as `python tools/collect_wallet_transactions.py`, Python puts
# the tools directory first on sys.path. Add the project root so the engine
# package can be imported reliably in GitHub Actions and local runs.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from engine.transaction_analysis import classify_transaction

WALLET = os.environ.get("SOLANA_WALLET", "").strip()
RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com").strip()
LIMIT = min(max(int(os.environ.get("SAMPLE_LIMIT", "5")), 1), 5)


def rpc(method, params):
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params,
    }).encode("utf-8")
    request = urllib.request.Request(
        RPC_URL, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        body = json.loads(response.read().decode("utf-8"))
    if not isinstance(body, dict) or body.get("error") is not None or "result" not in body:
        raise RuntimeError(f"RPC call failed: {method}")
    return body["result"]


def main():
    if not WALLET:
        print("SOLANA_WALLET is required.", file=sys.stderr)
        return 2

    signatures = rpc("getSignaturesForAddress", [
        WALLET, {"limit": LIMIT, "commitment": "confirmed"},
    ])
    if not isinstance(signatures, list):
        raise RuntimeError("RPC did not return a signature list.")

    output = Path("transaction-validation-sample")
    output.mkdir(parents=True, exist_ok=True)
    raw_rows = []
    csv_rows = []
    errors = []

    for item in signatures[:LIMIT]:
        signature = item.get("signature") if isinstance(item, dict) else None
        if not isinstance(signature, str) or not signature:
            continue
        try:
            transaction = rpc("getTransaction", [
                signature,
                {
                    "encoding": "json",
                    "commitment": "confirmed",
                    "maxSupportedTransactionVersion": 1,
                },
            ])
            if not isinstance(transaction, dict):
                errors.append({"signature": signature, "error": "Transaction unavailable"})
                continue
            prediction = classify_transaction(transaction, WALLET)
            raw_rows.append({
                "signature": signature,
                "slot": transaction.get("slot"),
                "block_time": transaction.get("blockTime"),
                "transaction_status": (
                    "failed" if (transaction.get("meta") or {}).get("err") is not None
                    else "success"
                ),
                "predicted_label": prediction.get("classification", "UNKNOWN"),
                "confidence": prediction.get("confidence", "low"),
                "reason": prediction.get("reason", ""),
                "prediction_evidence": prediction.get("evidence", {}),
                "raw_transaction": transaction,
                "independent_review_required": True,
            })
            csv_rows.append({
                "signature": signature,
                "expected_label": "",
                "predicted_label": prediction.get("classification", "UNKNOWN"),
                "reviewed": "false",
                "notes": "Fill expected_label only after independent review of raw evidence.",
            })
        except Exception as exc:
            errors.append({
                "signature": signature,
                "error_type": type(exc).__name__,
                "error": str(exc)[:300],
            })
        time.sleep(0.2)

    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "wallet_address": WALLET,
        "read_only": True,
        "live_trading": False,
        "requested_limit": LIMIT,
        "fetched_transactions": len(raw_rows),
        "independent_labels_collected": 0,
        "metrics_calculated": False,
        "errors": errors,
        "transactions": raw_rows,
    }
    (output / "raw-review-sample.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    with (output / "review-labels.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["signature", "expected_label", "predicted_label", "reviewed", "notes"],
        )
        writer.writeheader()
        writer.writerows(csv_rows)

    print(json.dumps({
        "fetched_transactions": len(raw_rows),
        "review_rows_ready": len(csv_rows),
        "independent_labels_collected": 0,
        "metrics_calculated": False,
        "errors": len(errors),
        "artifact_directory": str(output),
        "read_only": True,
        "live_trading": False,
    }, indent=2))
    return 0 if raw_rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
