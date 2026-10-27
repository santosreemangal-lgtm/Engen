import argparse, json, sys, time
from engine.config import ConfigError, load_config
from engine.health import get_status, run_checks
from engine.logger import setup_logging
from engine.transaction_analysis import analyze_transaction, analyze_token_balance_changes, analyze_native_sol_balance_changes, classify_token_balance_changes

def run_mock_analysis_test():
    print("=== Running Offline Transaction Analysis Mock Test ===")
    w, t, s = "MOCK_WALLET_A", "MOCK_TOKEN_XYZ_MINT", "So11111111111111111111111111111111111111112"
    tx = {"transaction": {"message": {"accountKeys": [w, t, s, "FEE_PAYER"], "header": {"numRequiredSignatures": 1, "numReadonlySignedAccounts": 0, "numReadonlyUnsignedAccounts": 0}}}, "meta": {"err": None, "fee": 5000, "slot": 250000000, "preBalances": [1000000000, 0, 0, 0], "postBalances": [990000000, 0, 0, 0], "preTokenBalances": [{"accountIndex": 1, "mint": t, "owner": w, "uiTokenAmount": {"amount": "10000000", "decimals": 6}}], "postTokenBalances": [{"accountIndex": 1, "mint": t, "owner": w, "uiTokenAmount": {"amount": "22851247", "decimals": 6}}]}}
    try:
        tx_i = analyze_transaction(tx)
        t_c = analyze_token_balance_changes(tx["meta"])
        s_c = analyze_native_sol_balance_changes(tx)
        res = classify_token_balance_changes(t_c, w)
        if res == "UNKNOWN" and any(change.get("account_index") == 0 and int(change.get("delta_amount", "0")) < 0 for change in s_c): res = "BUY_CANDIDATE"
        rep = {"test_status": "SUCCESS", "tx_status": tx_i.get("status"), "slot": tx_i.get("slot"), "wallet_monitored": w, "classification_result": res, "expected_classification": "BUY_CANDIDATE", "matches_expected": res == "BUY_CANDIDATE"}
        print(json.dumps(rep, indent=4)); return 0 if rep["matches_expected"] else 1
    except Exception as e:
        print(json.dumps({"test_status": "FAILED", "error": str(e)}, indent=4)); return 1

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mock-analysis-test", action="store_true")
    args = parser.parse_args()
    if args.mock_analysis_test: sys.exit(run_mock_analysis_test())
    st = time.perf_counter()
    try: cfg = load_config()
    except ConfigError as e: print(f"CONFIG ERROR: {e}"); sys.exit(1)
    setup_logging(cfg.log_level); passed = run_checks(cfg); status = get_status(cfg)
    status["startup_ms"] = (time.perf_counter() - st) * 1000; print(json.dumps(status, indent=2)); sys.exit(0 if passed else 1)

if __name__ == "__main__": main()
