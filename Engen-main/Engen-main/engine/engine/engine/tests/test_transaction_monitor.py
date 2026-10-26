import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine.solana_public import DEFAULT_RPC_URL, SolanaRPCError
from engine.signature_store import SignatureStore
from engine.transaction_monitor import (
    monitor_wallet,
    process_new_signatures,
)


class TransactionMonitorTests(unittest.TestCase):

    @patch("engine.transaction_monitor.process_transaction_signature")
    def test_processes_valid_signatures(self, mock_process):
        mock_process.return_value = {
            "signature": "sig-123",
            "available": True,
        }

        results = process_new_signatures(
            signatures=[{"signature": "sig-123"}],
        )

        self.assertEqual(
            results,
            [{"signature": "sig-123", "available": True}],
        )
        mock_process.assert_called_once_with(
            signature="sig-123",
            rpc_url=DEFAULT_RPC_URL,
            timeout=10,
        )

    @patch("engine.transaction_monitor.process_transaction_signature")
    def test_skips_invalid_signature_records(self, mock_process):
        mock_process.return_value = {
            "signature": "valid-signature",
            "available": True,
        }

        results = process_new_signatures(
            signatures=[
                None,
                "invalid-record",
                {},
                {"signature": ""},
                {"signature": 123},
                {"signature": "valid-signature"},
            ],
        )

        self.assertEqual(
            results,
            [{
                "signature": "valid-signature",
                "available": True,
            }],
        )
        mock_process.assert_called_once_with(
            signature="valid-signature",
            rpc_url=DEFAULT_RPC_URL,
            timeout=10,
        )

    def test_rejects_non_list_input(self):
        with self.assertRaises(ValueError):
            process_new_signatures(
                signatures={"signature": "sig-123"},
            )

    @patch("engine.transaction_monitor.process_transaction_signature")
    def test_processes_multiple_signatures(self, mock_process):
        mock_process.side_effect = [
            {
                "signature": "sig-1",
                "available": True,
            },
            {
                "signature": "sig-2",
                "available": False,
                "error": "Transaction data is not available yet.",
            },
        ]

        results = process_new_signatures(
            signatures=[
                {"signature": "sig-1"},
                {"signature": "sig-2"},
            ],
        )

        self.assertEqual(
            results,
            [
                {
                    "signature": "sig-1",
                    "available": True,
                },
                {
                    "signature": "sig-2",
                    "available": False,
                    "error": "Transaction data is not available yet.",
                    "retryable": True,
                },
            ],
        )
        self.assertEqual(mock_process.call_count, 2)

    @patch("engine.transaction_monitor.process_transaction_signature")
    def test_custom_rpc_settings_are_forwarded(self, mock_process):
        mock_process.return_value = {
            "signature": "sig-custom",
            "available": True,
        }

        results = process_new_signatures(
            signatures=[{"signature": "sig-custom"}],
            rpc_url="https://example.com",
            timeout=5,
        )

        self.assertEqual(len(results), 1)
        mock_process.assert_called_once_with(
            signature="sig-custom",
            rpc_url="https://example.com",
            timeout=5,
        )

    def test_monitor_wallet_processes_new_signatures(self):
        with (
            patch("engine.transaction_monitor.SignatureStore") as mock_store_class,
            patch(
                "engine.transaction_monitor.get_and_store_new_wallet_signatures"
            ) as mock_get_new,
            patch(
                "engine.transaction_monitor.process_new_signatures"
            ) as mock_process_new,
        ):
            mock_store = mock_store_class.return_value
            mock_get_new.return_value = [
                {"signature": "new-signature"},
            ]
            mock_process_new.return_value = [
                {
                    "signature": "new-signature",
                    "available": True,
                },
            ]

            results = monitor_wallet(
                wallet_address="PublicWalletAddress",
                limit=5,
                rpc_url="https://example.com",
                timeout=7,
            )

            self.assertEqual(len(results), 1)
            mock_get_new.assert_called_once()

            process_kwargs = mock_process_new.call_args.kwargs

            self.assertEqual(
                process_kwargs["signatures"],
                [{"signature": "new-signature"}],
            )
            self.assertEqual(
                process_kwargs["rpc_url"],
                "https://example.com",
            )
            self.assertEqual(process_kwargs["timeout"], 7)
            self.assertEqual(
                process_kwargs["wallet_address"],
                "PublicWalletAddress",
            )
            self.assertIs(process_kwargs["store"], mock_store)

    def test_monitor_wallet_rejects_empty_address(self):
        with self.assertRaises(ValueError):
            monitor_wallet("")

    def test_monitor_wallet_returns_empty_when_no_new_signatures(self):
        with (
            patch("engine.transaction_monitor.SignatureStore") as mock_store_class,
            patch(
                "engine.transaction_monitor.get_and_store_new_wallet_signatures"
            ) as mock_get_new,
            patch(
                "engine.transaction_monitor.process_new_signatures"
            ) as mock_process_new,
        ):
            mock_store = mock_store_class.return_value
            mock_get_new.return_value = []
            mock_process_new.return_value = []

            results = monitor_wallet(
                wallet_address="PublicWalletAddress",
            )

            self.assertEqual(results, [])
            mock_get_new.assert_called_once()

            process_kwargs = mock_process_new.call_args.kwargs

            self.assertEqual(process_kwargs["signatures"], [])
            self.assertEqual(
                process_kwargs["rpc_url"],
                DEFAULT_RPC_URL,
            )
            self.assertEqual(process_kwargs["timeout"], 10)
            self.assertEqual(
                process_kwargs["wallet_address"],
                "PublicWalletAddress",
            )
            self.assertIs(process_kwargs["store"], mock_store)

    @patch("engine.transaction_monitor.process_transaction_signature")
    def test_filters_token_balance_changes_to_watched_wallet(
        self,
        mock_process,
    ):
        watched_wallet = "wallet-watched"
        own_change = {
            "owner": watched_wallet,
            "mint": "mint-own",
            "delta_amount": "100",
        }
        other_change = {
            "owner": "wallet-other",
            "mint": "mint-other",
            "delta_amount": "-100",
        }
        unknown_owner_change = {
            "mint": "mint-unknown",
            "delta_amount": "50",
        }

        mock_process.return_value = {
            "signature": "sig-123",
            "available": True,
            "token_balance_changes": [
                own_change,
                other_change,
                unknown_owner_change,
            ],
        }

        results = process_new_signatures(
            signatures=[{"signature": "sig-123"}],
            wallet_address=watched_wallet,
        )

        self.assertEqual(
            results[0]["token_balance_changes"],
            [own_change],
        )

    @patch("engine.transaction_monitor.process_transaction_signature")
    def test_classifies_successful_monitored_transaction(self, mock_process):
        wallet = "wallet-watched"
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
        ]

        mock_process.return_value = {
            "signature": "sig-buy",
            "available": True,
            "status": "success",
            "signers": [wallet],
            "token_balance_changes": changes,
        }

        results = process_new_signatures(
            signatures=[{"signature": "sig-buy", "confirmationStatus": "confirmed"}],
            wallet_address=wallet,
        )

        self.assertEqual(
            results[0]["trade_signal"]["classification"],
            "BUY_CANDIDATE",
        )

    @patch("engine.transaction_monitor.process_transaction_signature")
    def test_unknown_when_monitored_wallet_is_not_a_signer(
        self,
        mock_process,
    ):
        wallet = "wallet-watched"
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
        ]

        mock_process.return_value = {
            "signature": "sig-other-signer",
            "available": True,
            "status": "success",
            "signers": ["wallet-other"],
            "token_balance_changes": changes,
        }

        results = process_new_signatures(
            signatures=[{"signature": "sig-other-signer", "confirmationStatus": "confirmed"}],
            wallet_address=wallet,
        )

        self.assertEqual(
            results[0]["trade_signal"]["classification"],
            "UNKNOWN",
        )
        self.assertEqual(
            results[0]["trade_signal"]["asset_mints"],
            [],
        )
        self.assertEqual(
            results[0]["trade_signal"]["quote_mints"],
            [],
        )

    @patch("engine.transaction_monitor.process_transaction_signature")
    def test_unknown_when_signer_information_is_missing(
        self,
        mock_process,
    ):
        wallet = "wallet-watched"

        mock_process.return_value = {
            "signature": "sig-no-signer-data",
            "available": True,
            "status": "success",
            "token_balance_changes": [
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
            ],
        }

        results = process_new_signatures(
            signatures=[{"signature": "sig-no-signer-data", "confirmationStatus": "confirmed"}],
            wallet_address=wallet,
        )

        self.assertEqual(
            results[0]["trade_signal"]["classification"],
            "UNKNOWN",
        )

    @patch("engine.transaction_monitor.process_transaction_signature")
    def test_insufficient_confirmation_does_not_create_trade_candidate(
        self, mock_process,
    ):
        wallet = "wallet-watched"
        mock_process.return_value = {
            "signature": "sig-unconfirmed",
            "available": True,
            "status": "success",
            "signers": [wallet],
            "token_balance_changes": [
                {"owner": wallet, "mint": "TOKEN_A", "delta_amount": "10"},
                {
                    "owner": wallet,
                    "mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                    "delta_amount": "-20",
                },
            ],
        }
        result = process_new_signatures(
            signatures=[{"signature": "sig-unconfirmed", "confirmationStatus": "processed"}],
            wallet_address=wallet,
        )[0]
        self.assertEqual(result["trade_signal"]["classification"], "UNKNOWN")
        self.assertFalse(result["confirmation_sufficient"])
        self.assertTrue(result["retryable"])

    @patch("engine.transaction_monitor.process_transaction_signature")
    def test_rpc_failure_does_not_create_a_success_transaction_record(
        self, mock_process,
    ):
        mock_process.side_effect = SolanaRPCError("Public Solana RPC request failed.")
        result = process_new_signatures(
            signatures=[{"signature": "some-signature"}],
            wallet_address="wallet-watched",
        )[0]
        self.assertFalse(result["available"])
        self.assertEqual(result["metadata_status"], "rpc_error")
        self.assertNotIn("status", result)
        self.assertNotIn("token_balance_changes", result)
        self.assertTrue(result["retryable"])

    @patch("engine.transaction_monitor.process_transaction_signature")
    def test_incomplete_metadata_stays_pending_for_reprocessing(self, mock_process):
        wallet = "11111111111111111111111111111111"
        signature = "1" * 63 + "4"
        raw = {"slot": 700, "transaction": {"message": {"accountKeys": []}}}
        with tempfile.TemporaryDirectory() as directory:
            store = SignatureStore(Path(directory) / "signatures.db")
            store.save(wallet, signature, slot=700)
            mock_process.return_value = {
                "signature": signature,
                "available": True,
                "metadata_complete": False,
                "metadata_missing_fields": ["preBalances"],
                "status": "success",
                "token_balance_changes": [],
                "raw_transaction": raw,
            }
            result = process_new_signatures(
                signatures=[{
                    "signature": signature,
                    "confirmationStatus": "confirmed",
                }],
                wallet_address=wallet,
                store=store,
            )[0]
            self.assertFalse(result["available"])
            self.assertTrue(result["retryable"])
            self.assertEqual(store.get_status(wallet, signature), "pending")
            record = store.get_transaction_record(wallet, signature)
            self.assertEqual(record["status"], "incomplete")
            self.assertEqual(record["raw_transaction"], raw)

    @patch("engine.transaction_monitor.process_transaction_signature")
    def test_rpc_failures_are_bounded_and_move_to_review(self, mock_process):
        wallet = "11111111111111111111111111111111"
        signature = "1" * 63 + "2"
        with tempfile.TemporaryDirectory() as directory:
            store = SignatureStore(Path(directory) / "signatures.db")
            store.save(wallet, signature)
            mock_process.side_effect = SolanaRPCError(
                "Public Solana RPC request failed."
            )
            for _ in range(5):
                result = process_new_signatures(
                    signatures=[{"signature": signature}],
                    wallet_address=wallet,
                    store=store,
                )[0]
            self.assertEqual(result["processing_status"], "needs_review")
            self.assertEqual(
                store.get_status(wallet, signature),
                "needs_review",
            )
            self.assertEqual(
                store.get_error_record(wallet, signature)["attempts"],
                5,
            )

    @patch("engine.transaction_monitor.process_transaction_signature")
    def test_raw_transaction_is_preserved_idempotently(self, mock_process):
        wallet = "11111111111111111111111111111111"
        signature = "1" * 63 + "3"
        raw = {"slot": 500, "meta": {"fee": 5000}}
        with tempfile.TemporaryDirectory() as directory:
            store = SignatureStore(Path(directory) / "signatures.db")
            store.save(wallet, signature, slot=500)
            mock_process.return_value = {
                "signature": signature,
                "available": True,
                "metadata_complete": True,
                "status": "failed",
                "token_balance_changes": [],
                "raw_transaction": raw,
            }
            for _ in range(2):
                process_new_signatures(
                    signatures=[{
                        "signature": signature,
                        "confirmationStatus": "confirmed",
                    }],
                    wallet_address=wallet,
                    store=store,
                )
            record = store.get_transaction_record(wallet, signature)
            self.assertEqual(record["raw_transaction"], raw)
            self.assertEqual(record["status"], "processed")

    @patch("engine.transaction_monitor.process_transaction_signature")
    def test_unavailable_transaction_is_returned_for_visibility(
        self,
        mock_process,
    ):
        mock_process.return_value = {
            "signature": "pending-signature",
            "available": False,
            "error": "Transaction data is not available yet.",
        }

        results = process_new_signatures(
            signatures=[
                {"signature": "pending-signature"},
            ],
        )

        self.assertEqual(
            results,
            [
                {
                    "signature": "pending-signature",
                    "available": False,
                    "error": "Transaction data is not available yet.",
                    "retryable": True,
                },
            ],
        )


if __name__ == "__main__":
    unittest.main()