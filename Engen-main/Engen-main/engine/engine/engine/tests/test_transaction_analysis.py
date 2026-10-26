import unittest

from engine.transaction_analysis import (
    WRAPPED_SOL_MINT,
    analyze_transaction,
    analyze_token_balance_changes,
    analyze_native_sol_balance_changes,
    classify_token_balance_changes,
)


class TransactionAnalysisTests(unittest.TestCase):

    def test_successful_transaction(self):
        transaction = {
            "slot": 123,
            "transaction": {
                "message": {
                    "accountKeys": ["wallet_A", "wallet_B"],
                }
            },
            "meta": {
                "err": None,
                "fee": 5000,
            },
        }

        result = analyze_transaction(transaction)

        self.assertTrue(result["available"])
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["slot"], 123)
        self.assertEqual(result["fee"], 5000)
        self.assertEqual(result["account_count"], 2)
        self.assertIsNone(result["error"])

    def test_failed_transaction(self):
        transaction = {
            "slot": 456,
            "transaction": {
                "message": {
                    "accountKeys": ["wallet_A"],
                }
            },
            "meta": {
                "err": {"InstructionError": [0, "Custom"]},
                "fee": 5000,
            },
        }

        result = analyze_transaction(transaction)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["account_count"], 1)
        self.assertIsNotNone(result["error"])

    def test_preserves_program_and_instruction_context(self):
        transaction = {
            "slot": 99,
            "transaction": {
                "message": {
                    "accountKeys": ["wallet_A", "TOKEN_PROGRAM"],
                    "instructions": [{
                        "programIdIndex": 1,
                        "parsed": {"type": "transfer"},
                        "stackHeight": 1,
                    }],
                },
            },
            "meta": {
                "err": None,
                "fee": 5000,
                "innerInstructions": [{
                    "index": 0,
                    "instructions": [{
                        "programIdIndex": 1,
                        "parsed": {"type": "checked_transfer"},
                        "stackHeight": 2,
                    }],
                }],
            },
        }
        result = analyze_transaction(transaction)
        self.assertEqual(result["program_ids"], ["TOKEN_PROGRAM"])
        self.assertEqual(result["instructions"][0]["program_id"], "TOKEN_PROGRAM")
        self.assertEqual(result["instructions"][0]["parsed_type"], "transfer")
        self.assertEqual(result["instructions"][1]["source"], "inner")
        self.assertEqual(result["instructions"][1]["parsed_type"], "checked_transfer")

    def test_missing_metadata_returns_unavailable(self):
        result = analyze_transaction({"slot": 123})

        self.assertFalse(result["available"])
        self.assertIn("error", result)

    def test_invalid_input_is_rejected(self):
        with self.assertRaises(ValueError):
            analyze_transaction(None)

    def test_invalid_account_keys_are_handled(self):
        transaction = {
            "transaction": {
                "message": {
                    "accountKeys": "not-a-list",
                }
            },
            "meta": {
                "err": None,
                "fee": 100,
            },
        }

        result = analyze_transaction(transaction)

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["account_count"], 0)

    def test_transaction_without_account_keys(self):
        transaction = {
            "slot": 789,
            "transaction": {"message": {}},
            "meta": {"err": None, "fee": 5000},
        }

        result = analyze_transaction(transaction)

        self.assertTrue(result["available"])
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["account_count"], 0)

    def test_transaction_with_empty_account_keys(self):
        transaction = {
            "slot": 790,
            "transaction": {
                "message": {"accountKeys": []}
            },
            "meta": {"err": None, "fee": 5000},
        }

        result = analyze_transaction(transaction)

        self.assertEqual(result["account_count"], 0)

    def test_non_mapping_metadata_returns_unavailable(self):
        transaction = {"slot": 791, "meta": None}

        result = analyze_transaction(transaction)

        self.assertFalse(result["available"])
        self.assertIn("error", result)

    def test_token_balance_increase(self):
        meta = {
            "preTokenBalances": [{
                "accountIndex": 1,
                "mint": "TOKEN_A",
                "owner": "WALLET_A",
                "uiTokenAmount": {
                    "amount": "100",
                    "decimals": 2,
                },
            }],
            "postTokenBalances": [{
                "accountIndex": 1,
                "mint": "TOKEN_A",
                "owner": "WALLET_A",
                "uiTokenAmount": {
                    "amount": "175",
                    "decimals": 2,
                },
            }],
        }

        changes = analyze_token_balance_changes(meta)

        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["delta_amount"], "75")
        self.assertEqual(changes[0]["pre_amount"], "100")
        self.assertEqual(changes[0]["post_amount"], "175")

    def test_token_balance_decrease(self):
        meta = {
            "preTokenBalances": [{
                "accountIndex": 1,
                "mint": "TOKEN_A",
                "owner": "WALLET_A",
                "uiTokenAmount": {
                    "amount": "175",
                    "decimals": 2,
                },
            }],
            "postTokenBalances": [{
                "accountIndex": 1,
                "mint": "TOKEN_A",
                "owner": "WALLET_A",
                "uiTokenAmount": {
                    "amount": "100",
                    "decimals": 2,
                },
            }],
        }

        changes = analyze_token_balance_changes(meta)

        self.assertEqual(changes[0]["delta_amount"], "-75")

    def test_new_token_account_balance(self):
        meta = {
            "preTokenBalances": [],
            "postTokenBalances": [{
                "accountIndex": 2,
                "mint": "TOKEN_A",
                "owner": "WALLET_A",
                "uiTokenAmount": {
                    "amount": "250",
                    "decimals": 0,
                },
            }],
        }

        changes = analyze_token_balance_changes(meta)

        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["pre_amount"], "0")
        self.assertEqual(changes[0]["delta_amount"], "250")

    def test_closed_token_account_balance(self):
        meta = {
            "preTokenBalances": [{
                "accountIndex": 2,
                "mint": "TOKEN_A",
                "owner": "WALLET_A",
                "uiTokenAmount": {
                    "amount": "250",
                    "decimals": 0,
                },
            }],
            "postTokenBalances": [],
        }

        changes = analyze_token_balance_changes(meta)

        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["post_amount"], "0")
        self.assertEqual(changes[0]["delta_amount"], "-250")

    def test_unchanged_token_balance_is_omitted(self):
        entry = {
            "accountIndex": 1,
            "mint": "TOKEN_A",
            "owner": "WALLET_A",
            "uiTokenAmount": {
                "amount": "100",
                "decimals": 2,
            },
        }

        changes = analyze_token_balance_changes({
            "preTokenBalances": [entry],
            "postTokenBalances": [entry],
        })

        self.assertEqual(changes, [])

    def test_invalid_metadata_returns_empty_list(self):
        self.assertEqual(analyze_token_balance_changes(None), [])

    def test_invalid_balance_entries_are_ignored(self):
        meta = {
            "preTokenBalances": ["invalid", {"mint": "TOKEN_A"}],
            "postTokenBalances": None,
        }

        self.assertEqual(analyze_token_balance_changes(meta), [])

    def test_classifies_buy_candidate_for_monitored_wallet(self):
        wallet = "WALLET_A"

        changes = [
            {
                "owner": wallet,
                "mint": "TOKEN_A",
                "delta_amount": "500",
            },
            {
                "owner": wallet,
                "mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                "delta_amount": "-1000000",
            },
            {
                "owner": "WALLET_OTHER",
                "mint": "TOKEN_B",
                "delta_amount": "-500",
            },
        ]

        result = classify_token_balance_changes(changes, wallet)

        self.assertEqual(result["classification"], "BUY_CANDIDATE")
        self.assertEqual(result["asset_mints"], ["TOKEN_A"])

    def test_classifies_sell_candidate_for_monitored_wallet(self):
        wallet = "WALLET_A"

        changes = [
            {
                "owner": wallet,
                "mint": "TOKEN_A",
                "delta_amount": "-500",
            },
            {
                "owner": wallet,
                "mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                "delta_amount": "1000000",
            },
        ]

        result = classify_token_balance_changes(changes, wallet)

        self.assertEqual(result["classification"], "SELL_CANDIDATE")

    def test_classifies_buy_candidate_with_usdt_quote_token(self):
        wallet = "WALLET_A"

        changes = [
            {
                "owner": wallet,
                "mint": "TOKEN_A",
                "delta_amount": "1",
            },
            {
                "owner": wallet,
                "mint": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
                "delta_amount": "-100",
            },
        ]

        result = classify_token_balance_changes(changes, wallet)

        self.assertEqual(result["classification"], "BUY_CANDIDATE")

    def test_unknown_when_quote_token_is_missing(self):
        result = classify_token_balance_changes(
            [{
                "owner": "WALLET_A",
                "mint": "TOKEN_A",
                "delta_amount": "10",
            }],
            "WALLET_A",
        )

        self.assertEqual(result["classification"], "UNKNOWN")

    def test_unknown_when_only_other_wallet_changes_are_present(self):
        result = classify_token_balance_changes(
            [
                {
                    "owner": "WALLET_OTHER",
                    "mint": "TOKEN_A",
                    "delta_amount": "10",
                },
                {
                    "owner": "WALLET_OTHER",
                    "mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                    "delta_amount": "-20",
                },
            ],
            "WALLET_A",
        )

        self.assertEqual(result["classification"], "UNKNOWN")

    def test_ignores_native_sol_changes_owned_by_other_wallet(self):
        result = classify_token_balance_changes(
            [
                {
                    "owner": "WALLET_A",
                    "mint": "TOKEN_A",
                    "delta_amount": "12851247",
                    "decimals": 6,
                },
                {
                    "owner": "WALLET_OTHER",
                    "mint": WRAPPED_SOL_MINT,
                    "delta_amount": "-1538696",
                    "decimals": 9,
                    "source": "native_sol",
                    "fee_adjusted": True,
                },
            ],
            "WALLET_A",
        )

        self.assertEqual(result["classification"], "UNKNOWN")
        self.assertEqual(result["asset_mints"], [])
        self.assertEqual(result["quote_mints"], [])

    def test_unknown_when_balance_directions_are_ambiguous(self):
        result = classify_token_balance_changes(
            [
                {
                    "owner": "WALLET_A",
                    "mint": "TOKEN_A",
                    "delta_amount": "10",
                },
                {
                    "owner": "WALLET_A",
                    "mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                    "delta_amount": "20",
                },
            ],
            "WALLET_A",
        )

        self.assertEqual(result["classification"], "UNKNOWN")

    def test_unknown_for_invalid_input(self):
        self.assertEqual(
            classify_token_balance_changes(
                None,
                "WALLET_A",
            )["classification"],
            "UNKNOWN",
        )

        self.assertEqual(
            classify_token_balance_changes(
                [],
                "",
            )["classification"],
            "UNKNOWN",
        )

    def test_native_sol_change_adjusts_fee_payer_delta(self):
        transaction = {
            "transaction": {
                "message": {
                    "accountKeys": ["WALLET_A", "OTHER_ACCOUNT"],
                }
            },
            "meta": {
                "fee": 5000,
                "preBalances": [10_000_000_000, 1000],
                "postBalances": [9_997_995_000, 1000],
                "loadedAddresses": {
                    "writable": [],
                    "readonly": [],
                },
            },
        }

        changes = analyze_native_sol_balance_changes(transaction)

        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["owner"], "WALLET_A")
        self.assertEqual(changes[0]["raw_delta_amount"], "-2005000")
        self.assertEqual(changes[0]["delta_amount"], "-2000000")
        self.assertTrue(changes[0]["fee_adjusted"])
        self.assertEqual(changes[0]["decimals"], 9)

    def test_native_sol_change_supports_loaded_account_addresses(self):
        transaction = {
            "transaction": {
                "message": {
                    "accountKeys": [{
                        "pubkey": "FEE_PAYER",
                        "signer": True,
                        "writable": True,
                    }],
                }
            },
            "meta": {
                "fee": 5000,
                "preBalances": [5_000_000_000, 10_000_000_000],
                "postBalances": [4_999_995_000, 9_999_000_000],
                "loadedAddresses": {
                    "writable": ["WALLET_A"],
                    "readonly": [],
                },
            },
        }

        changes = analyze_native_sol_balance_changes(transaction)

        wallet_changes = [
            change for change in changes
            if change["owner"] == "WALLET_A"
        ]

        self.assertEqual(len(wallet_changes), 1)
        self.assertEqual(wallet_changes[0]["account_index"], 1)
        self.assertEqual(wallet_changes[0]["delta_amount"], "-1000000")
        self.assertFalse(wallet_changes[0]["fee_adjusted"])

    def test_classifies_native_sol_buy_candidate(self):
        wallet = "WALLET_A"

        changes = [
            {
                "owner": wallet,
                "mint": "TOKEN_A",
                "delta_amount": "12851247",
                "decimals": 6,
            },
            {
                "owner": wallet,
                "mint": WRAPPED_SOL_MINT,
                "delta_amount": "-200000000",
                "decimals": 9,
                "source": "native_sol",
                "fee_adjusted": True,
            },
        ]

        result = classify_token_balance_changes(changes, wallet)

        self.assertEqual(result["classification"], "BUY_CANDIDATE")
        self.assertEqual(result["asset_mints"], ["TOKEN_A"])
        self.assertEqual(result["quote_mints"], [WRAPPED_SOL_MINT])

    def test_native_sol_and_spl_quote_are_not_mixed(self):
        wallet = "WALLET_A"

        changes = [
            {
                "owner": wallet,
                "mint": "TOKEN_A",
                "delta_amount": "100",
            },
            {
                "owner": wallet,
                "mint": WRAPPED_SOL_MINT,
                "delta_amount": "-200000000",
                "decimals": 9,
                "source": "native_sol",
            },
            {
                "owner": wallet,
                "mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                "delta_amount": "-1000000",
            },
        ]

        result = classify_token_balance_changes(changes, wallet)

        self.assertEqual(result["classification"], "UNKNOWN")
        self.assertEqual(result["asset_mints"], [])
        self.assertEqual(result["quote_mints"], [])


if __name__ == "__main__":
    unittest.main()