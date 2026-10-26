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
    """Exercise transaction analysis with fabricated data only; no RPC or wallet access."""
    print("=== Running Offline Transaction Analysis Mock Test ===")

    wallet = "MOCK_WALLET_A"
    token_mint = "MOCK_TOKEN_XYZ_MINT"

    # The fee payer spends 0.5 SOL after excluding the 5,000-lamport fee,
    # while its balance of a non-quote token increases.
    transaction = {
        "transaction": {
            "message": {
                "accountKeys": [
                    wallet,
                    "MOCK_TOKEN_ACCOUNT",
                    "MOCK_OTHER_ACCOUNT",
                    "MOCK_FEE_ACCOUNT",
                ],
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
            "preBalances": [2_000_000_000, 100, 0, 0],
            "postBalances": [1_500_005_000, 100, 0, 0],
            "preTokenBalances": [
                {
                    "accountIndex": 1,
                    "mint": token_mint,
                    "owner": wallet,
                    "uiTokenAmount": {"amount": "10000000", "decimals": 6},
                }
            ],
            "postTokenBalances": [
                {
                    "accountIndex": 1,
                    "mint": token_mint,
                    "owner": wallet,
                    "uiTokenAmount": {"amount": "22851247", "decimals": 6},
                }
            ],
        },
    }

    try:
        transaction_info = analyze_transaction(transaction)
        token_changes = analyze_token_balance_changes(transaction["meta"])
        native_sol_changes = analyze_native_sol_balance_changes(transaction)
        classification = classify_token_balance_changes(
            token_changes + native_sol_changes,
            wallet,
        )
        result = classification.get("classification", "UNKNOWN")
        matches_expected = (
            transaction_info.get("available") is True
            and transaction_info.get("status") == "success"
            and result == "BUY_CANDIDATE"
        )

        report = {
            "test_status": "SUCCESS" if matches_expected else "FAILED",
            "tx_status": transaction_info.get("status"),
            "slot": transaction_info.get("slot"),
            "wallet_monitored": wallet,
            "classification_result": result,
            "classification_reason": classification.get("reason"),
            "expected_classification": "BUY_CANDIDATE",
            "matches_expected": matches_expected,
        }
        print(json.dumps(report, indent=4))
        return 0 if matches_expected else 1
    except Exception as exc:
        print(
            json.dumps(
                {"test_status": "FAILED", "error": str(exc)},
                indent=4,
            )
        )
        return 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mock-analysis-test",
        action="store_true",
        help="Run the offline transaction-analysis test with fabricated data.",
    )
    args = parser.parse_args()

    if args.mock_analysis_test:
        sys.exit(run_mock_analysis_test())

    start_time = time.perf_counter()
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}")
        sys.exit(1)

    setup_logging(config.log_level)
    passed = run_checks(config)
    status = get_status(config)
    status["startup_ms"] = (time.perf_counter() - start_time) * 1000
    print(json.dumps(status, indent=2))
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
