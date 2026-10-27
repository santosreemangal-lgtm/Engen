"""Stage 1 command-line entry point for the read-only Solana engine."""
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
    """Run a deterministic, offline-only classification smoke test."""
    print("=== Running Offline Transaction Analysis Mock Test ===")
    wallet = "MOCK_WALLET_A"
    token_mint = "MOCK_TOKEN_XYZ_MINT"
    wrapped_sol_mint = "So11111111111111111111111111111111111111112"
    transaction = {
        "slot": 250000000,
        "transaction": {"message": {"accountKeys": [wallet, token_mint, wrapped_sol_mint, "MOCK_ACCOUNT"], "header": {"numRequiredSignatures": 1, "numReadonlySignedAccounts": 0, "numReadonlyUnsignedAccounts": 0}}},
        "meta": {
            "err": None, "fee": 5000,
            "preBalances": [2500000000, 0, 0, 5000000000],
            "postBalances": [2490000000, 0, 0, 5000000000],
            "preTokenBalances": [{"accountIndex": 1, "mint": token_mint, "owner": wallet, "uiTokenAmount": {"amount": "10000000", "decimals": 6}}],
            "postTokenBalances": [{"accountIndex": 1, "mint": token_mint, "owner": wallet, "uiTokenAmount": {"amount": "22851247", "decimals": 6}}],
        },
    }
    try:
        transaction_info = analyze_transaction(transaction)
        token_changes = analyze_token_balance_changes(transaction["meta"])
        native_sol_changes = analyze_native_sol_balance_changes(transaction)
        classification = classify_token_balance_changes(token_changes + native_sol_changes, wallet)
        matched = transaction_info.get("status") == "success" and classification.get("classification") == "BUY_CANDIDATE" and wrapped_sol_mint in classification.get("quote_mints", [])
        report = {"test_status": "SUCCESS" if matched else "FAILED", "tx_status": transaction_info.get("status"), "slot": transaction_info.get("slot"), "wallet_monitored": wallet, "classification_result": classification, "expected_classification": "BUY_CANDIDATE", "matches_expected": matched, "mode": "offline_mock_only"}
        print(json.dumps(report, indent=4))
        return 0 if matched else 1
    except Exception as exc:
        print(json.dumps({"test_status": "FAILED", "error": str(exc), "mode": "offline_mock_only"}, indent=4))
        return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the read-only Solana engine health checks.")
    parser.add_argument("--mock-analysis-test", action="store_true", help="Run an offline mock transaction-analysis check.")
    # Unit tests call main() from inside unittest; don't parse unittest's own CLI flags.
    if argv is None and "unittest" in sys.modules:
        argv = []
    args = parser.parse_args(argv)
    if args.mock_analysis_test:
        return run_mock_analysis_test()

    start_time = time.perf_counter()
    try:
        config = load_config()
    except ConfigError as exc:
        # Keep startup diagnostics on stdout so CLI callers and tests capture them consistently.
        print(f"CONFIG ERROR: {exc}")
        return 1

    setup_logging(config.log_level)
    checks = run_checks(config)
    status = get_status(config)
    status["checks"] = [{"name": check.name, "passed": check.passed, "detail": check.detail} for check in checks]
    status["startup_ms"] = round((time.perf_counter() - start_time) * 1000, 3)
    all_passed = all(check.passed for check in checks)
    status["status"] = "ok" if all_passed else "failed"
    print(json.dumps(status, indent=2))
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
