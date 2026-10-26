import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import engine.paper_monitor_runner as runner
from engine.paper_trader import PaperTrader
from engine.signature_store import SignatureStore


USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
TEST_WALLET = "PublicWalletTestAddress"


class PaperMonitorRunnerTests(unittest.TestCase):

    def _validate_rpc_url(self, rpc_url):
        parser = runner.build_parser()
        args = parser.parse_args([
            "--wallet", TEST_WALLET,
            "--quote-mint", USDT_MINT,
            "--rpc-url", rpc_url,
        ])
        runner.validate_args(args, parser)

    def test_rpc_url_accepts_valid_https_endpoint(self):
        self._validate_rpc_url("https://api.mainnet-beta.solana.com")

    def test_rpc_url_accepts_local_http_endpoint(self):
        self._validate_rpc_url("http://127.0.0.1:8899")

    def test_rpc_url_rejects_missing_host(self):
        with self.assertRaises(SystemExit):
            self._validate_rpc_url("https:///rpc")

    def test_rpc_url_rejects_unsupported_scheme(self):
        with self.assertRaises(SystemExit):
            self._validate_rpc_url("ftp://rpc.example.com")

    def test_rpc_url_rejects_user_credentials(self):
        with self.assertRaises(SystemExit):
            self._validate_rpc_url("https://user:password@rpc.example.com")

    def test_rpc_url_rejects_fragments(self):
        with self.assertRaises(SystemExit):
            self._validate_rpc_url("https://rpc.example.com/rpc#fragment")

    def test_rpc_url_rejects_invalid_port(self):
        with self.assertRaises(SystemExit):
            self._validate_rpc_url("https://rpc.example.com:99999")


    def test_runner_starts_paper_only_with_persistent_usdt_balance(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "paper.db"
            signature_db_path = Path(directory) / "signatures.db"

            args = [
                "paper_monitor_runner",
                "--wallet",
                TEST_WALLET,
                "--quote-mint",
                USDT_MINT,
                "--paper-balance",
                "100",
                "--paper-db",
                str(db_path),
                "--signature-db",
                str(signature_db_path),
                "--interval",
                "5",
            ]

            output = io.StringIO()

            # Interrupt before any real RPC call.
            with (
                patch("sys.argv", args),
                patch.object(runner, "SignatureStore") as signature_store_mock,
                patch.object(
                    runner,
                    "monitor_wallet",
                    side_effect=KeyboardInterrupt,
                ),
                redirect_stdout(output),
            ):
                runner.main()

            printed = output.getvalue()

            self.assertIn('"mode": "paper_trading_only"', printed)
            self.assertIn('"real_trading": false', printed)
            self.assertIn('"wallet_signing": false', printed)
            self.assertTrue(db_path.exists())
            signature_store_mock.assert_called_once_with(
                db_path=str(signature_db_path)
            )

            restarted = PaperTrader(
                {USDT_MINT: "999"},
                db_path=db_path,
            )

            self.assertEqual(
                restarted.get_balances(),
                {USDT_MINT: "100"},
            )

    def _run_once_with_transaction(
        self,
        transaction,
        process_side_effect,
        events,
    ):
        """Run one scan with mocked RPC and trading dependencies."""
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "paper.db"

            args = [
                "paper_monitor_runner",
                "--wallet",
                TEST_WALLET,
                "--quote-mint",
                USDT_MINT,
                "--paper-balance",
                "100",
                "--paper-db",
                str(db_path),
                "--limit",
                "5",
                "--once",
            ]

            store = Mock()

            def mark_processed(wallet, signature):
                events.append("marked_processed")
                return True

            store.mark_processed.side_effect = mark_processed

            trader = Mock()
            trader.get_balances.return_value = {USDT_MINT: "100"}
            trader.trades = []
            trader.process.side_effect = process_side_effect

            output = io.StringIO()

            with (
                patch("sys.argv", args),
                patch.object(
                    runner,
                    "SignatureStore",
                    return_value=store,
                ),
                patch.object(
                    runner,
                    "PaperTrader",
                    return_value=trader,
                ),
                patch.object(
                    runner,
                    "monitor_wallet",
                    return_value=[transaction],
                ) as mock_monitor,
                redirect_stdout(output),
            ):
                exit_code = runner.main()

            return exit_code, store, trader, mock_monitor

    def test_signature_is_marked_processed_after_paper_processing(self):
        transaction = {
            "signature": "paper-test-signature",
            "available": True,
            "status": "success",
            "token_balance_changes": [],
            "trade_signal": {
                "classification": "UNKNOWN",
                "reason": "Test transaction",
                "asset_mints": [],
                "quote_mints": [],
            },
        }

        events = []

        def process_paper_transaction(result):
            events.append("paper_processed")
            return {
                "status": "SKIPPED",
                "reason": "Test signal is not tradable.",
            }

        (
            exit_code,
            store,
            trader,
            mock_monitor,
        ) = self._run_once_with_transaction(
            transaction,
            process_paper_transaction,
            events,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            events,
            ["paper_processed", "marked_processed"],
        )

        trader.process.assert_called_once_with(transaction)
        store.mark_processed.assert_called_once_with(
            TEST_WALLET,
            "paper-test-signature",
        )
        self.assertTrue(
            mock_monitor.call_args.kwargs["defer_processed"]
        )

    def test_processing_error_leaves_signature_pending(self):
        transaction = {
            "signature": "paper-error-signature",
            "available": True,
            "status": "success",
            "token_balance_changes": [],
            "trade_signal": {
                "classification": "UNKNOWN",
                "reason": "Test transaction",
                "asset_mints": [],
                "quote_mints": [],
            },
        }

        events = []

        def fail_paper_processing(result):
            events.append("paper_processing_started")
            raise RuntimeError("Simulated paper processing failure")

        (
            exit_code,
            store,
            trader,
            mock_monitor,
        ) = self._run_once_with_transaction(
            transaction,
            fail_paper_processing,
            events,
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(events, ["paper_processing_started"])
        trader.process.assert_called_once_with(transaction)
        store.mark_processed.assert_not_called()
        self.assertTrue(
            mock_monitor.call_args.kwargs["defer_processed"]
        )

    def test_runner_simulates_buy_and_persists_balance(self):
        """A synthetic BUY updates only the paper-trading database."""
        with tempfile.TemporaryDirectory() as directory:
            paper_db = Path(directory) / "paper.db"
            signature_db = Path(directory) / "signatures.db"

            signature = "synthetic-buy-signature"

            # Use a real signature store, but no live RPC connection.
            store = SignatureStore(db_path=signature_db)
            self.assertTrue(store.save(TEST_WALLET, signature))

            transaction = {
                "signature": signature,
                "available": True,
                "status": "success",
                "trade_signal": {
                    "classification": "BUY_CANDIDATE",
                    "asset_mints": ["TOKEN_A"],
                    "quote_mints": [USDT_MINT],
                },
                "token_balance_changes": [
                    {
                        "mint": "TOKEN_A",
                        "delta_amount": "500",
                        "decimals": 2,
                    },
                    {
                        "mint": USDT_MINT,
                        "delta_amount": "-1000000",
                        "decimals": 6,
                    },
                ],
            }

            args = [
                "paper_monitor_runner",
                "--wallet",
                TEST_WALLET,
                "--quote-mint",
                USDT_MINT,
                "--paper-balance",
                "100",
                "--paper-db",
                str(paper_db),
                "--limit",
                "5",
                "--once",
            ]

            output = io.StringIO()

            with (
                patch("sys.argv", args),
                patch.object(
                    runner,
                    "SignatureStore",
                    return_value=store,
                ),
                patch.object(
                    runner,
                    "monitor_wallet",
                    return_value=[transaction],
                ) as mock_monitor,
                redirect_stdout(output),
            ):
                exit_code = runner.main()

            self.assertEqual(exit_code, 0)
            mock_monitor.assert_called_once()
            self.assertTrue(
                mock_monitor.call_args.kwargs["defer_processed"]
            )

            # Processing succeeds before the signature is marked processed.
            self.assertEqual(
                store.get_status(TEST_WALLET, signature),
                "processed",
            )

            printed = output.getvalue()
            self.assertIn('"status": "PAPER_FILLED"', printed)
            self.assertIn('"real_trading": false', printed)

            # Reopen the actual paper database to verify persistence.
            restarted = PaperTrader(
                {USDT_MINT: "999"},
                db_path=paper_db,
            )

            self.assertEqual(
                restarted.get_balances(),
                {USDT_MINT: "99", "TOKEN_A": "5"},
            )
            self.assertEqual(len(restarted.trades), 1)


if __name__ == "__main__":
    unittest.main()