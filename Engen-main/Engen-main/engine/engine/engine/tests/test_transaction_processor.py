import unittest
from unittest import mock

from engine.solana_public import SolanaRPCError
from engine.transaction_analysis import WRAPPED_SOL_MINT
from engine.transaction_processor import process_transaction_signature


class TransactionProcessorTests(unittest.TestCase):

    def test_handles_transaction_not_available(self):
        with mock.patch(
            "engine.transaction_processor.get_transaction",
            return_value=None,
        ):
            result = process_transaction_signature("signature123")

        self.assertFalse(result["available"])
        self.assertEqual(result["signature"], "signature123")
        self.assertIn("error", result)

    def test_invalid_transaction_data_raises_rpc_error(self):
        with mock.patch(
            "engine.transaction_processor.get_transaction",
            return_value={"meta": None},
        ):
            with self.assertRaises(SolanaRPCError):
                process_transaction_signature("signature123")

    def test_processes_failed_transaction(self):
        transaction = {
            "slot": 456,
            "transaction": {
                "message": {"accountKeys": ["wallet_A"]}
            },
            "meta": {
                "err": {"InstructionError": [0, "Custom"]},
                "fee": 5000,
                "preBalances": [10_000_000_000],
                "postBalances": [9_999_995_000],
                "preTokenBalances": [],
                "postTokenBalances": [],
            },
        }

        with mock.patch(
            "engine.transaction_processor.get_transaction",
            return_value=transaction,
        ):
            result = process_transaction_signature("signature456")

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["signature"], "signature456")
        self.assertEqual(result["token_balance_changes"], [])

    def test_processes_successful_transaction(self):
        transaction = {
            "slot": 123,
            "transaction": {
                "message": {
                    "accountKeys": ["wallet_A", "wallet_B"]
                }
            },
            "meta": {
                "err": None,
                "fee": 5000,
                "preTokenBalances": [],
                "postTokenBalances": [],
            },
        }

        with mock.patch(
            "engine.transaction_processor.get_transaction",
            return_value=transaction,
        ):
            result = process_transaction_signature("signature123")

        self.assertTrue(result["available"])
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["slot"], 123)
        self.assertEqual(result["fee"], 5000)
        self.assertEqual(result["account_count"], 2)
        self.assertEqual(result["token_balance_changes"], [])

    def test_rejects_empty_signature(self):
        with self.assertRaises(ValueError):
            process_transaction_signature("")

    def test_uses_custom_rpc_settings(self):
        transaction = {
            "transaction": {"message": {"accountKeys": []}},
            "meta": {
                "err": None,
                "fee": 100,
                "preTokenBalances": [],
                "postTokenBalances": [],
            },
        }

        with mock.patch(
            "engine.transaction_processor.get_transaction",
            return_value=transaction,
        ) as mocked_get:
            process_transaction_signature(
                "signature123",
                rpc_url="https://example.invalid",
                timeout=3,
            )

        mocked_get.assert_called_once_with(
            signature="signature123",
            rpc_url="https://example.invalid",
            timeout=3,
        )

    def test_includes_token_balance_changes(self):
        transaction = {
            "slot": 789,
            "transaction": {
                "message": {"accountKeys": ["wallet_A"]}
            },
            "meta": {
                "err": None,
                "fee": 5000,
                "preTokenBalances": [{
                    "accountIndex": 1,
                    "mint": "TOKEN_A",
                    "owner": "wallet_A",
                    "uiTokenAmount": {
                        "amount": "100",
                        "decimals": 2,
                    },
                }],
                "postTokenBalances": [{
                    "accountIndex": 1,
                    "mint": "TOKEN_A",
                    "owner": "wallet_A",
                    "uiTokenAmount": {
                        "amount": "175",
                        "decimals": 2,
                    },
                }],
            },
        }

        with mock.patch(
            "engine.transaction_processor.get_transaction",
            return_value=transaction,
        ):
            result = process_transaction_signature("signature789")

        self.assertEqual(len(result["token_balance_changes"]), 1)
        change = result["token_balance_changes"][0]
        self.assertEqual(change["mint"], "TOKEN_A")
        self.assertEqual(change["owner"], "wallet_A")
        self.assertEqual(change["delta_amount"], "75")

    def test_includes_native_sol_changes_for_successful_transaction(self):
        transaction = {
            "slot": 789,
            "transaction": {
                "message": {
                    "accountKeys": ["wallet_A", "wallet_B"]
                }
            },
            "meta": {
                "err": None,
                "fee": 5000,
                "preBalances": [10_000_000_000, 200_000_000],
                "postBalances": [9_999_795_000, 200_000_000],
                "preTokenBalances": [],
                "postTokenBalances": [],
                "loadedAddresses": {
                    "writable": [],
                    "readonly": [],
                },
            },
        }

        with mock.patch(
            "engine.transaction_processor.get_transaction",
            return_value=transaction,
        ):
            result = process_transaction_signature("native-sol-signature")

        self.assertEqual(result["status"], "success")
        self.assertEqual(len(result["token_balance_changes"]), 1)

        change = result["token_balance_changes"][0]
        self.assertEqual(change["mint"], WRAPPED_SOL_MINT)
        self.assertEqual(change["owner"], "wallet_A")
        self.assertEqual(change["delta_amount"], "-200000")
        self.assertEqual(change["source"], "native_sol")
        self.assertTrue(change["fee_adjusted"])

    def test_failed_transaction_does_not_include_native_sol_changes(self):
        transaction = {
            "slot": 790,
            "transaction": {
                "message": {
                    "accountKeys": ["wallet_A"]
                }
            },
            "meta": {
                "err": {"InstructionError": [0, "Custom"]},
                "fee": 5000,
                "preBalances": [10_000_000_000],
                "postBalances": [9_999_995_000],
                "preTokenBalances": [],
                "postTokenBalances": [],
            },
        }

        with mock.patch(
            "engine.transaction_processor.get_transaction",
            return_value=transaction,
        ):
            result = process_transaction_signature("failed-native-sol-signature")

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["token_balance_changes"], [])


if __name__ == "__main__":
    unittest.main()