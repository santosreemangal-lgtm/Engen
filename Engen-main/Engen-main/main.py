import argparse
import json
import sys
import time

from engine.config import ConfigError, load_config
from engine.health import get_status, run_checks
from engine.logger import setup_logging
from engine.transaction_analysis import (
    analyze_native_sol_balance_changes,
    analyze_token_balance_changes,
    analyze_transaction,
    classify_token_balance_changes,
)


def run_mock_analysis_test():
    print("=== Running Offline Transaction Analysis Mock Test ===")
    wallet = "MOCK_WALLET_A"
    token = "MOCK_TOKEN_XYZ_MINT"
    wrapped_sol = "So11111111111111111111111111111111111111112"
    transaction = {
        "transaction": {
            "message": {
                "accountKeys": [wallet, token, wrapped_sol, "FEE_PAYER"],
                "header": {
                    "numRequiredSignatures": 1,
                    "numReadonlySignedAccounts": 0,
                    "numReadonlyUnsignedAccounts": 0,
                },
            }
        },
        "meta": {
            "err": None,
            "fee": 5000,
            "slot": 250000000,
            "preBalances": [1000000000, 0, 0, 0],
            "postBalances": [990000000, 0, 0, 0],
            "preTokenBalances": [
                {
                    "accountIndex": 1,
                    "mint": token,
                    "owner": wallet,
                    "uiTokenAmount": {"amount": "10000000", "decimals": 6},
                }
            ],
            "postTokenBalances": [
                {
                    "accountIndex": 1,
                    "mint": token,
                    "owner": wallet,
                    "uiTokenAmount": {"amount": "22851247", "decimals": 6},
                }
            ],
        },
    }

    try:
        transaction_info = analyze_transaction(transaction)
        token_changes = analyze_token_balance_changes(transaction["meta"])
        sol_changes = analyze_native_sol_balance_changes(transaction)
        classification = classify_token_balance_changes(token_changes, wallet)

        if classification.get("classification") == "UNKNOWN":
            classification = classify_token_balance_changes(
                token_changes + sol_changes, wallet
            )

        report = {
            "test_status": "SUCCESS",
            "tx_status": transaction_info.get("status"),
            "slot": transaction_info.get("slot"),
            "wallet_monitored": wallet,
            "classification_result": classification.get("classification"),
            "classification_reason": classification.get("reason"),
            "expected_classification": "BUY_CANDIDATE",
            "matches_expected": classification.get("classification") == "BUY_CANDIDATE",
        }
        print(json.dumps(report, indent=4))
        return 0 if report["matches_expected"] else 1
    except Exception as error:
        print(json.dumps({"test_status": "FAILED", "error": str(error)}, indent=4))
        return 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mock-analysis-test", action="store_true")
    args = parser.parse_args()

    if args.mock_analysis_test:
        sys.exit(run_mock_analysis_test())

    started = time.perf_counter()
    try:
        config = load_config()
    except ConfigError as error:
        print(f"CONFIG ERROR: {error}", file=sys.stderr)
        sys.exit(1)

    setup_logging(config.log_level)
    checks = run_checks(config)
    status = get_status(config)
    status["checks"] = [
        {"name": item.name, "passed": item.passed, "detail": item.detail}
        for item in checks
    ]
    status["startup_ms"] = (time.perf_counter() - started) * 1000
    status["status"] = "ok" if all(item.passed for item in checks) else "error"
    print(json.dumps(status, indent=2))
    sys.exit(0 if all(item.passed for item in checks) else 1)


if __name__ == "__main__":
    main()
