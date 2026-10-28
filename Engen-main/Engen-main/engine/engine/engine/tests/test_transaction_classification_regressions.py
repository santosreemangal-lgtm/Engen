import unittest
from unittest.mock import patch

from engine.solana_public import get_transaction
from engine.transaction_analysis import (
    WRAPPED_SOL_MINT,
    classify_token_balance_changes,
)

WALLET = "WALLET_A"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
ASSET = "TOKEN_A"


class TransactionClassificationRegressionTests(unittest.TestCase):
    @patch("engine.solana_public._rpc_request")
    def test_get_transaction_requests_parsed_instructions(self, rpc_request):
        rpc_request.return_value = {"transaction": {}, "meta": {}}

        get_transaction("1" * 64)

        params = rpc_request.call_args.args[0]["params"]
        self.assertEqual(params[1]["encoding"], "jsonParsed")
        self.assertEqual(params[1]["commitment"], "confirmed")

    def test_usdc_buy_is_classified(self):
        result = classify_token_balance_changes([
            {"owner": WALLET, "mint": ASSET, "delta_amount": "100", "decimals": 2},
            {"owner": WALLET, "mint": USDC_MINT, "delta_amount": "-2500000", "decimals": 6},
        ], WALLET)
        self.assertEqual(result["classification"], "BUY_CANDIDATE")
        self.assertEqual(result["asset_mints"], [ASSET])
        self.assertEqual(result["quote_mints"], [USDC_MINT])

    def test_usdc_sell_and_full_asset_exit_are_classified(self):
        # A fully closed token account is represented by a negative delta;
        # classification must not depend on the account still existing after the swap.
        result = classify_token_balance_changes([
            {"owner": WALLET, "mint": ASSET, "delta_amount": "-100", "decimals": 2},
            {"owner": WALLET, "mint": USDC_MINT, "delta_amount": "2500000", "decimals": 6},
        ], WALLET)
        self.assertEqual(result["classification"], "SELL_CANDIDATE")
        self.assertEqual(result["asset_mints"], [ASSET])
        self.assertEqual(result["quote_mints"], [USDC_MINT])

    def test_usdt_sell_is_classified(self):
        result = classify_token_balance_changes([
            {"owner": WALLET, "mint": ASSET, "delta_amount": "-100", "decimals": 2},
            {"owner": WALLET, "mint": USDT_MINT, "delta_amount": "2500000", "decimals": 6},
        ], WALLET)
        self.assertEqual(result["classification"], "SELL_CANDIDATE")

    def test_native_sol_buy_candidate_requires_asset_and_quote_deltas(self):
        result = classify_token_balance_changes([
            {"owner": WALLET, "mint": ASSET, "delta_amount": "100", "decimals": 2},
            {"owner": WALLET, "mint": WRAPPED_SOL_MINT, "delta_amount": "-200000000", "decimals": 9,
             "source": "native_sol", "fee_adjusted": True},
        ], WALLET)
        self.assertEqual(result["classification"], "BUY_CANDIDATE")
        self.assertEqual(result["quote_mints"], [WRAPPED_SOL_MINT])

    def test_parsed_swap_instruction_preserves_buy_candidate(self):
        from engine.transaction_analysis import classify_transaction

        transaction = {
            "transaction": {
                "message": {
                    "accountKeys": [{"pubkey": WALLET, "signer": True}],
                    "instructions": [{
                        "program": "jupiter",
                        "programId": "JUPITER_PROGRAM",
                        "parsed": {"type": "swap"},
                    }],
                }
            },
            "meta": {"err": None, "fee": 5000, "innerInstructions": []},
        }
        changes = [
            {"owner": WALLET, "mint": ASSET, "delta_amount": "100", "decimals": 2},
            {"owner": WALLET, "mint": WRAPPED_SOL_MINT, "delta_amount": "-200000000",
             "decimals": 9, "source": "native_sol", "fee_adjusted": True},
        ]
        result = classify_transaction(transaction, WALLET, changes)
        self.assertEqual(result["classification"], "BUY_CANDIDATE")

    def test_transfer_instruction_with_sol_delta_remains_unknown(self):
        from engine.transaction_analysis import classify_transaction

        transaction = {
            "transaction": {
                "message": {
                    "accountKeys": [{"pubkey": WALLET, "signer": True}],
                    "instructions": [{
                        "program": "system",
                        "programId": "11111111111111111111111111111111",
                        "parsed": {"type": "transfer"},
                    }],
                }
            },
            "meta": {"err": None, "fee": 5000, "innerInstructions": []},
        }
        changes = [
            {"owner": WALLET, "mint": ASSET, "delta_amount": "100", "decimals": 2},
            {"owner": WALLET, "mint": WRAPPED_SOL_MINT, "delta_amount": "-200000000",
             "decimals": 9, "source": "native_sol", "fee_adjusted": True},
        ]
        result = classify_transaction(transaction, WALLET, changes)
        self.assertEqual(result["classification"], "UNKNOWN")

    def test_failed_transaction_never_becomes_buy_or_sell(self):
        from engine.transaction_analysis import classify_transaction

        result = classify_transaction(
            {"meta": {"err": {"InstructionError": [0, "Custom"]}, "fee": 5000}},
            WALLET,
            [
                {"owner": WALLET, "mint": ASSET, "delta_amount": "100", "decimals": 2},
                {"owner": WALLET, "mint": USDC_MINT, "delta_amount": "-2500000", "decimals": 6},
            ],
        )
        self.assertEqual(result["classification"], "FAILED_TRANSACTION")

    def test_failed_or_transfer_only_activity_must_not_be_promoted(self):
        # A plain SOL debit without a received asset is not a sell signal.
        result = classify_token_balance_changes([
            {"owner": WALLET, "mint": WRAPPED_SOL_MINT, "delta_amount": "-5000", "decimals": 9,
             "source": "native_sol", "fee_adjusted": True},
        ], WALLET)
        self.assertEqual(result["classification"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
