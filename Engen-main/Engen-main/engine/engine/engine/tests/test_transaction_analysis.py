import unittest

from engine.transaction_analysis import (
    PUMP_FUN_PROGRAM_ID,
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

    def test_multiple_token_accounts_for_one_wallet_are_preserved(self):
        meta = {
            "preTokenBalances": [
                {"accountIndex": 1, "mint": "TOKEN_A", "owner": "WALLET_A",
                 "uiTokenAmount": {"amount": "100", "decimals": 0}},
                {"accountIndex": 2, "mint": "TOKEN_A", "owner": "WALLET_A",
                 "uiTokenAmount": {"amount": "50", "decimals": 0}},
            ],
            "postTokenBalances": [
                {"accountIndex": 1, "mint": "TOKEN_A", "owner": "WALLET_A",
                 "uiTokenAmount": {"amount": "125", "decimals": 0}},
                {"accountIndex": 2, "mint": "TOKEN_A", "owner": "WALLET_A",
                 "uiTokenAmount": {"amount": "60", "decimals": 0}},
            ],
        }
        changes = analyze_token_balance_changes(meta)
        self.assertEqual(len(changes), 2)
        self.assertEqual(
            sorted(change["delta_amount"] for change in changes),
            ["10", "25"],
        )
        signal = classify_token_balance_changes(
            changes + [{
                "owner": "WALLET_A",
                "mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                "delta_amount": "-100",
            }],
            "WALLET_A",
        )
        self.assertEqual(signal["classification"], "BUY_CANDIDATE")

    def test_balance_delta_reconciles_one_sided_missing_owner(self):
        wallet = "WALLET_A"
        meta = {
            "preTokenBalances": [{
                "accountIndex": 9,
                "mint": "TOKEN_A",
                "uiTokenAmount": {"amount": "100", "decimals": 2},
            }],
            "postTokenBalances": [{
                "accountIndex": 9,
                "mint": "TOKEN_A",
                "owner": wallet,
                "uiTokenAmount": {"amount": "175", "decimals": 2},
            }],
        }

        changes = analyze_token_balance_changes(meta)

        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["owner"], wallet)
        self.assertEqual(changes[0]["delta_amount"], "75")
        self.assertEqual(changes[0]["pre_amount"], "100")
        self.assertEqual(changes[0]["post_amount"], "175")

    def test_balance_delta_is_ignored_when_owner_changes(self):
        meta = {
            "preTokenBalances": [{
                "accountIndex": 9,
                "mint": "TOKEN_A",
                "owner": "WALLET_A",
                "uiTokenAmount": {"amount": "100", "decimals": 2},
            }],
            "postTokenBalances": [{
                "accountIndex": 9,
                "mint": "TOKEN_A",
                "owner": "WALLET_B",
                "uiTokenAmount": {"amount": "175", "decimals": 2},
            }],
        }

        self.assertEqual(analyze_token_balance_changes(meta), [])

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


    def test_classification_records_evidence_and_confidence(self):
        from engine.transaction_analysis import classify_transaction
        wallet = "WALLET_A"
        changes = [
            {"owner": wallet, "mint": "TOKEN_A", "delta_amount": "2500", "decimals": 3},
            {"owner": wallet, "mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
             "delta_amount": "-1000000", "decimals": 6},
        ]
        result = classify_transaction(None, wallet, changes)
        self.assertEqual(result["classification"], "BUY_CANDIDATE")
        self.assertEqual(result["confidence"], "medium")
        self.assertEqual(result["evidence"]["wallet_balance_changes"][0]["raw_delta_amount"], "2500")
        self.assertIn("limitations", result["evidence"])

    def test_failed_transaction_is_not_a_buy_or_sell(self):
        from engine.transaction_analysis import classify_transaction
        result = classify_transaction(
            {"meta": {"err": {"InstructionError": [0, "Custom"]}, "fee": 5000}},
            "WALLET_A",
            [{"owner": "WALLET_A", "mint": "TOKEN_A", "delta_amount": "100"}],
        )
        self.assertEqual(result["classification"], "FAILED_TRANSACTION")
        self.assertEqual(result["confidence"], "high")
        self.assertFalse(result["evidence"]["balance_changes_used"])

    def test_token_receipt_alone_remains_unknown(self):
        wallet = "WALLET_A"
        result = classify_token_balance_changes(
            [{"owner": wallet, "mint": "TOKEN_A", "delta_amount": "100", "decimals": 2}],
            wallet,
        )
        self.assertEqual(result["classification"], "UNKNOWN")

    def test_sol_decrease_without_asset_movement_is_not_a_sell(self):
        wallet = "WALLET_A"
        result = classify_token_balance_changes(
            [{"owner": wallet, "mint": WRAPPED_SOL_MINT, "delta_amount": "-5000",
              "decimals": 9, "source": "native_sol", "fee_adjusted": True}],
            wallet,
        )
        self.assertEqual(result["classification"], "UNKNOWN")

    def test_mismatched_decimals_do_not_create_a_delta(self):
        meta = {
            "preTokenBalances": [{
                "accountIndex": 1, "mint": "TOKEN_A", "owner": "WALLET_A",
                "uiTokenAmount": {"amount": "100", "decimals": 2},
            }],
            "postTokenBalances": [{
                "accountIndex": 1, "mint": "TOKEN_A", "owner": "WALLET_A",
                "uiTokenAmount": {"amount": "1000", "decimals": 3},
            }],
        }
        self.assertEqual(analyze_token_balance_changes(meta), [])

    def test_classification_keeps_raw_units_and_does_not_infer_market_value(self):
        from engine.transaction_analysis import classify_transaction
        wallet = "WALLET_A"
        changes = [
            {"owner": wallet, "mint": "TOKEN_A", "delta_amount": "123456789",
             "decimals": 8, "pre_amount": "0", "post_amount": "123456789"},
            {"owner": wallet, "mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
             "delta_amount": "-2500000", "decimals": 6},
        ]
        result = classify_transaction(None, wallet, changes)
        evidence = result["evidence"]["wallet_balance_changes"]
        self.assertEqual(evidence[0]["raw_delta_amount"], "123456789")
        self.assertEqual(evidence[0]["decimals"], 8)
        self.assertNotIn("market_value", evidence[0])

    def test_multiple_asset_mints_with_mixed_directions_remain_unknown(self):
        wallet = "WALLET_A"
        result = classify_token_balance_changes(
            [
                {"owner": wallet, "mint": "TOKEN_A", "delta_amount": "100"},
                {"owner": wallet, "mint": "TOKEN_B", "delta_amount": "-100"},
                {"owner": wallet, "mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                 "delta_amount": "-50"},
            ],
            wallet,
        )
        self.assertEqual(result["classification"], "UNKNOWN")


    def test_rent_or_transfer_like_native_sol_movement_is_not_auto_swap(self):
        from engine.transaction_analysis import classify_transaction
        wallet = "WALLET_A"
        transaction = {
            "transaction": {
                "message": {
                    "accountKeys": [wallet, "TOKEN_PROGRAM"],
                    "instructions": [{
                        "program": "system",
                        "programId": "11111111111111111111111111111111",
                        "parsed": {"type": "createAccount"},
                    }],
                }
            },
            "meta": {"err": None, "fee": 5000},
        }
        changes = [
            {"owner": wallet, "mint": "TOKEN_A", "delta_amount": "100",
             "decimals": 2},
            {"owner": wallet, "mint": WRAPPED_SOL_MINT, "delta_amount": "-1000000",
             "decimals": 9, "source": "native_sol", "fee_adjusted": True},
        ]
        result = classify_transaction(transaction, wallet, changes)
        self.assertEqual(result["classification"], "UNKNOWN")
        self.assertIn("transfer", result["reason"])


    def _pumpfun_v2_transaction(self, instruction_name):
        return {
            "transaction": {
                "message": {
                    "accountKeys": ["WALLET_A", PUMP_FUN_PROGRAM_ID],
                    "instructions": [{
                        "programId": PUMP_FUN_PROGRAM_ID,
                        "accounts": [
                            "GLOBAL",
                            "TOKEN_A",
                            "UNLISTED_QUOTE_MINT",
                            "TOKEN_PROGRAM",
                        ],
                    }],
                },
            },
            "meta": {
                "err": None,
                "fee": 5000,
                "logMessages": [
                    f"Program {PUMP_FUN_PROGRAM_ID} invoke [1]",
                    f"Program log: Instruction: {instruction_name}",
                    f"Program {PUMP_FUN_PROGRAM_ID} success",
                ],
            },
        }

    def test_pumpfun_v2_classifies_unlisted_quote_mint_buy(self):
        from engine.transaction_analysis import classify_transaction

        wallet = "WALLET_A"
        transaction = self._pumpfun_v2_transaction("BuyV2")
        changes = [
            {
                "owner": wallet,
                "mint": "TOKEN_A",
                "delta_amount": "13975216850689",
                "decimals": 6,
            },
            {
                "owner": wallet,
                "mint": "UNLISTED_QUOTE_MINT",
                "delta_amount": "-1098516683",
                "decimals": 9,
            },
            {
                "owner": wallet,
                "mint": WRAPPED_SOL_MINT,
                "delta_amount": "-60111",
                "decimals": 9,
                "source": "native_sol",
            },
        ]

        result = classify_transaction(transaction, wallet, changes)

        self.assertEqual(result["classification"], "BUY_CANDIDATE")
        self.assertEqual(result["asset_mints"], ["TOKEN_A"])
        self.assertEqual(result["quote_mints"], ["UNLISTED_QUOTE_MINT"])
        self.assertEqual(
            result["evidence"]["pumpfun_instruction_action"],
            "BUY",
        )
        self.assertEqual(result["evidence"]["pumpfun_base_mint"], "TOKEN_A")
        self.assertEqual(
            result["evidence"]["pumpfun_quote_mint"],
            "UNLISTED_QUOTE_MINT",
        )

    def test_pumpfun_v2_classifies_unlisted_quote_mint_sell(self):
        from engine.transaction_analysis import classify_transaction

        wallet = "WALLET_A"
        transaction = self._pumpfun_v2_transaction("SellV2")
        changes = [
            {
                "owner": wallet,
                "mint": "TOKEN_A",
                "delta_amount": "-2500",
                "decimals": 6,
            },
            {
                "owner": wallet,
                "mint": "UNLISTED_QUOTE_MINT",
                "delta_amount": "900",
                "decimals": 9,
            },
        ]

        result = classify_transaction(transaction, wallet, changes)

        self.assertEqual(result["classification"], "SELL_CANDIDATE")
        self.assertEqual(result["asset_mints"], ["TOKEN_A"])
        self.assertEqual(result["quote_mints"], ["UNLISTED_QUOTE_MINT"])
        self.assertEqual(
            result["evidence"]["pumpfun_instruction_action"],
            "SELL",
        )

    def test_pumpfun_v2_instruction_with_opposite_balance_direction_stays_unknown(self):
        from engine.transaction_analysis import classify_transaction

        wallet = "WALLET_A"
        transaction = self._pumpfun_v2_transaction("BuyV2")
        changes = [
            {
                "owner": wallet,
                "mint": "TOKEN_A",
                "delta_amount": "-2500",
                "decimals": 6,
            },
            {
                "owner": wallet,
                "mint": "UNLISTED_QUOTE_MINT",
                "delta_amount": "900",
                "decimals": 9,
            },
        ]

        result = classify_transaction(transaction, wallet, changes)

        self.assertEqual(result["classification"], "UNKNOWN")
        self.assertEqual(result["asset_mints"], [])
        self.assertEqual(result["quote_mints"], [])

    def test_classifies_parsed_system_and_token_transfers_as_transfer(self):
        from engine.transaction_analysis import classify_transaction

        wallet = "WALLET_A"
        for program, parsed_type in (
            ("system", "transfer"),
            ("spl-token", "transferChecked"),
        ):
            with self.subTest(program=program, parsed_type=parsed_type):
                transaction = {
                    "transaction": {
                        "message": {
                            "instructions": [{
                                "program": program,
                                "parsed": {"type": parsed_type, "info": {}},
                            }],
                        },
                    },
                    "meta": {"err": None, "fee": 5000},
                }
                result = classify_transaction(transaction, wallet, [])
                self.assertEqual(result["classification"], "TRANSFER")
                self.assertEqual(result["activity_type"], "TRANSFER")
                self.assertNotIn(result["classification"], ("BUY_CANDIDATE", "SELL_CANDIDATE"))

    def test_unknown_program_context_prevents_transfer_overclassification(self):
        from engine.transaction_analysis import classify_transaction

        transaction = {
            "transaction": {
                "message": {
                    "instructions": [
                        {
                            "program": "unknown_amm",
                            "programId": "UnrecognizedProgram111111111111111111111111111",
                        },
                        {
                            "program": "spl-token",
                            "programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                            "parsed": {"type": "transferChecked", "info": {}},
                        },
                    ],
                },
            },
            "meta": {"err": None, "fee": 5000},
        }
        result = classify_transaction(transaction, "WALLET_A", [])
        self.assertEqual(result["classification"], "UNKNOWN")
        self.assertNotEqual(result["activity_type"], "TRANSFER")

    def test_classifies_parsed_staking_as_non_swap_unknown(self):
        from engine.transaction_analysis import classify_transaction

        transaction = {
            "transaction": {
                "message": {
                    "instructions": [{
                        "program": "stake",
                        "programId": "Stake11111111111111111111111111111111111111",
                        "parsed": {"type": "delegate", "info": {}},
                    }],
                },
            },
            "meta": {"err": None, "fee": 5000},
        }
        result = classify_transaction(transaction, "WALLET_A", [])
        self.assertEqual(result["classification"], "UNKNOWN")
        self.assertEqual(result["activity_type"], "STAKING")
        self.assertFalse(result["evidence"]["activity_type"] in ("BUY_CANDIDATE", "SELL_CANDIDATE"))

    def test_classifies_associated_token_account_lifecycle(self):
        from engine.transaction_analysis import classify_transaction

        transaction = {
            "transaction": {
                "message": {
                    "instructions": [{
                        "program": "spl-associated-token-account",
                        "parsed": {"type": "create", "info": {}},
                    }],
                },
            },
            "meta": {"err": None, "fee": 5000},
        }
        result = classify_transaction(transaction, "WALLET_A", [])
        self.assertEqual(result["classification"], "UNKNOWN")
        self.assertEqual(result["activity_type"], "ACCOUNT_ACTIVITY")

    def test_classifies_recognized_liquidity_instruction(self):
        from engine.transaction_analysis import classify_transaction

        transaction = {
            "transaction": {
                "message": {
                    "instructions": [{
                        "program": "raydium_amm",
                        "parsed": {"type": "addLiquidity", "info": {}},
                    }],
                },
            },
            "meta": {"err": None, "fee": 5000},
        }
        result = classify_transaction(transaction, "WALLET_A", [])
        self.assertEqual(result["classification"], "LIQUIDITY_OPERATION")
        self.assertEqual(result["activity_type"], "LIQUIDITY_OPERATION")

    def test_transfer_instruction_inside_recognized_swap_context_is_not_relabelled(self):
        from engine.transaction_analysis import classify_transaction

        transaction = {
            "transaction": {
                "message": {
                    "instructions": [
                        {
                            "program": "raydium_amm",
                            "parsed": {"type": "swap", "info": {}},
                        },
                        {
                            "program": "spl-token",
                            "parsed": {"type": "transferChecked", "info": {}},
                        },
                    ],
                },
            },
            "meta": {"err": None, "fee": 5000},
        }
        result = classify_transaction(transaction, "WALLET_A", [])
        self.assertEqual(result["classification"], "UNKNOWN")
        self.assertNotEqual(result["activity_type"], "TRANSFER")

    def test_known_mainnet_swap_program_ids_preserve_balance_based_candidates(self):
        from engine.transaction_analysis import classify_transaction

        wallet = "WALLET_A"
        recognized_program_ids = (
            "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",
            "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
            "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",
            "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",
            "routeUGWgWzqBWFcrCfv8tritsqukccJPu3q5GPP3xS",
            "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",
        )
        changes = [
            {"owner": wallet, "mint": "TOKEN_A", "delta_amount": "1000", "decimals": 6},
            {
                "owner": wallet,
                "mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                "delta_amount": "-500",
                "decimals": 6,
            },
        ]
        for program_id in recognized_program_ids:
            with self.subTest(program_id=program_id):
                transaction = {
                    "transaction": {
                        "message": {
                            "instructions": [
                                {"programId": program_id},
                                {
                                    "program": "spl-token",
                                    "programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                                    "parsed": {"type": "transferChecked", "info": {}},
                                },
                            ],
                        },
                    },
                    "meta": {"err": None, "fee": 5000},
                }
                result = classify_transaction(transaction, wallet, changes)
                self.assertEqual(result["classification"], "BUY_CANDIDATE")
                self.assertEqual(result["asset_mints"], ["TOKEN_A"])

    def test_unrecognized_program_does_not_become_a_swap_from_transfer_deltas(self):
        from engine.transaction_analysis import classify_transaction

        wallet = "WALLET_A"
        transaction = {
            "transaction": {
                "message": {
                    "instructions": [
                        {"programId": "UnrecognizedProgram111111111111111111111111111"},
                        {
                            "program": "spl-token",
                            "programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                            "parsed": {"type": "transferChecked", "info": {}},
                        },
                    ],
                },
            },
            "meta": {"err": None, "fee": 5000},
        }
        changes = [
            {"owner": wallet, "mint": "TOKEN_A", "delta_amount": "1000", "decimals": 6},
            {
                "owner": wallet,
                "mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                "delta_amount": "-500",
                "decimals": 6,
            },
        ]
        result = classify_transaction(transaction, wallet, changes)
        self.assertEqual(result["classification"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()