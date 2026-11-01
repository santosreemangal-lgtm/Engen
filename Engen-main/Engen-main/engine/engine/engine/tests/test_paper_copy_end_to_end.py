import tempfile
import time
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from dashboard.monitor_bridge import create_scan_function
from engine.paper_copy_runner import execute_pending_paper_orders as execute_real_paper_orders
from engine.paper_simulator import PaperSimulator
from engine.pnl import MarketPrice
from engine.profile_store import ProfileStore
from engine.signature_store import SignatureStore


WALLET = "11111111111111111111111111111111"
ASSET = "So11111111111111111111111111111111111111112"
QUOTE = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SIGNATURE = "1" * 64


class PaperCopyEndToEndTests(unittest.TestCase):
    def _recover_source_refs_after_restart(self, root, simulator, profile_id):
        # A fresh process reconstructs both stores. The source-reference ledger
        # is deliberately still empty to model a crash before its write.
        restarted_profiles = ProfileStore(root / "profiles.db")
        restarted_simulator = PaperSimulator(db_path=root / "paper_trading.db")
        self.assertEqual(restarted_profiles.list_source_trade_refs(profile_id), [])

        signatures = SignatureStore(root / "signatures.db")
        with (
            patch("dashboard.monitor_bridge.SignatureStore", return_value=signatures),
            patch("dashboard.monitor_bridge.monitor_wallet", return_value=[]),
            patch("dashboard.monitor_bridge.execute_pending_paper_orders", return_value=[]),
        ):
            scan = create_scan_function(
                side_getter=lambda: "BOTH",
                settings_getter=lambda: {},
                simulator_getter=lambda: restarted_simulator,
                profile_id=profile_id,
                profile_store=restarted_profiles,
            )
            scan(WALLET)

        return restarted_profiles, restarted_simulator

    def test_restart_recovers_reference_after_crash_between_order_and_ref_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profiles = ProfileStore(root / "profiles.db")
            profile_id = profiles.create_profile("Crash recovery")
            profiles.associate_source_wallet(profile_id, WALLET)
            simulator = PaperSimulator(db_path=root / "paper_trading.db")
            source_time = int(time.time())
            order = simulator.create_order(
                signal_id="wallet-copy:" + WALLET + ":" + SIGNATURE,
                side="BUY", asset_mint=ASSET, quantity="1", quote_mint="USD",
                created_at=datetime.now(timezone.utc).isoformat(),
                details={
                    "origin": "AUTOMATIC_WALLET_COPY",
                    "source_wallet": WALLET,
                    "source_signature": SIGNATURE,
                    "source_slot": 321,
                    "source_block_time": source_time,
                },
            )
            self.assertEqual(order["status"], "CREATED")
            # No call to save_source_trade_ref: simulate process death here.
            restarted_profiles, restarted_simulator = self._recover_source_refs_after_restart(
                root, simulator, profile_id,
            )

            refs = restarted_profiles.list_source_trade_refs(profile_id)
            self.assertEqual(len(refs), 1)
            self.assertEqual(refs[0]["order_id"], order["order_id"])
            self.assertEqual(refs[0]["source_wallet_address"], WALLET)
            self.assertEqual(refs[0]["source_signature"], SIGNATURE)
            self.assertEqual(refs[0]["side"], "BUY")
            self.assertEqual(refs[0]["asset_mint"], ASSET)
            self.assertEqual(refs[0]["source_slot"], 321)
            self.assertEqual(refs[0]["source_block_time"], source_time)
            self.assertEqual(len(restarted_simulator.report()["orders"]), 1)

    def test_restart_recovers_source_reference_from_archived_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profiles = ProfileStore(root / "profiles.db")
            profile_id = profiles.create_profile("Archived recovery")
            profiles.associate_source_wallet(profile_id, WALLET)
            simulator = PaperSimulator(db_path=root / "paper_trading.db")
            # A 64-character base58 signature made only of leading '1's
            # decodes to exactly 64 bytes and is valid for this isolated test.
            source_signature = "1" * 64
            source_time = int(time.time())
            order = simulator.create_order(
                signal_id="wallet-copy:" + WALLET + ":" + source_signature,
                side="SELL", asset_mint=ASSET, quantity="1", quote_mint="USD",
                created_at=datetime.now(timezone.utc).isoformat(),
                details={
                    "origin": "AUTOMATIC_WALLET_COPY",
                    "source_wallet": WALLET,
                    "source_signature": source_signature,
                    "source_slot": 654,
                    "source_block_time": source_time,
                },
            )
            simulator.reset_session("100", "USD")
            self.assertEqual(simulator.report()["orders"], [])
            self.assertEqual(len(simulator.report()["archived_sessions"]), 1)

            restarted_profiles, restarted_simulator = self._recover_source_refs_after_restart(
                root, simulator, profile_id,
            )
            refs = restarted_profiles.list_source_trade_refs(profile_id)
            self.assertEqual(len(refs), 1)
            self.assertEqual(refs[0]["order_id"], order["order_id"])
            self.assertEqual(refs[0]["source_wallet_address"], WALLET)
            self.assertEqual(refs[0]["source_signature"], source_signature)
            self.assertEqual(refs[0]["side"], "SELL")
            self.assertEqual(refs[0]["asset_mint"], ASSET)
            self.assertEqual(refs[0]["source_slot"], 654)
            self.assertEqual(refs[0]["source_block_time"], source_time)
            self.assertEqual(len(restarted_simulator.report()["archived_sessions"]), 1)

    def test_monitor_to_paper_fill_history_and_process_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile_db = root / "profiles.db"
            signatures_db = root / "signatures.db"
            paper_db = root / "paper_trading.db"

            profile_store = ProfileStore(profile_db)
            profile_id = profile_store.create_profile("End-to-end paper profile")
            profile_store.associate_source_wallet(profile_id, WALLET)

            signature_store = SignatureStore(signatures_db)
            signature_store.save(WALLET, SIGNATURE, slot=123456)
            simulator = PaperSimulator(db_path=paper_db)
            simulator.set_initial_virtual_balance("1000", "USD")

            block_time = int(time.time())
            monitor_result = {
                "signature": SIGNATURE,
                "available": True,
                "status": "success",
                "metadata_complete": True,
                "metadata_status": "available",
                "signers": [WALLET],
                "signature_observation": {
                    "confirmation_status": "finalized",
                    "slot": 123456,
                    "block_time": block_time,
                },
                "trade_signal": {
                    "classification": "BUY_CANDIDATE",
                    "reason": "Synthetic test swap",
                    "confidence": "high",
                    "asset_mints": [ASSET],
                    "quote_mints": [QUOTE],
                },
                "token_balance_changes": [
                    {
                        "owner": WALLET, "mint": ASSET, "decimals": 9,
                        "delta_amount": "2500000000",
                        "pre_amount": "0", "post_amount": "2500000000",
                    },
                    {
                        "owner": WALLET, "mint": QUOTE, "decimals": 6,
                        "delta_amount": "-10000000",
                    },
                ],
            }
            observed_at = datetime.now(timezone.utc).isoformat()

            def price_provider(mint):
                self.assertEqual(mint, ASSET)
                return MarketPrice(
                    mint=ASSET,
                    price=Decimal("2"),
                    currency="USD",
                    source="offline-e2e-test",
                    observed_at=observed_at,
                    liquidity_usd=Decimal("50000"),
                    liquidity_source="offline-e2e-test",
                    liquidity_observed_at=observed_at,
                )

            def execute_with_test_market_data(wallet_address, simulator=None):
                # The source link must be durable before execution begins.
                self.assertEqual(
                    len(profile_store.list_source_trade_refs(profile_id)), 1
                )
                return execute_real_paper_orders(
                    wallet_address, simulator=simulator,
                    price_provider=price_provider,
                )

            with (
                patch("dashboard.monitor_bridge.SignatureStore", return_value=signature_store),
                patch("dashboard.monitor_bridge.monitor_wallet", return_value=[monitor_result]),
                patch(
                    "dashboard.monitor_bridge.execute_pending_paper_orders",
                    side_effect=execute_with_test_market_data,
                ),
            ):
                scan = create_scan_function(
                    side_getter=lambda: "BOTH",
                    settings_getter=lambda: {
                        "sizing_mode": "FIXED_USD",
                        "sizing_value": "10",
                    },
                    simulator_getter=lambda: simulator,
                    profile_id=profile_id,
                    profile_store=profile_store,
                )
                results = scan(WALLET)

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["trade_signal"]["classification"], "BUY_CANDIDATE")
            self.assertEqual(results[0]["paper_copy_order"]["side"], "BUY")
            self.assertEqual(results[0]["paper_copy_order"]["execution_status"], "FILLED")
            self.assertEqual(signature_store.get_status(WALLET, SIGNATURE), "processed")

            report = simulator.report()
            self.assertEqual(report["orders"][0]["status"], "FILLED")
            self.assertEqual(len(report["fills"]), 1)
            self.assertLess(Decimal(report["balances"]["USD"]), Decimal("1000"))
            self.assertGreater(Decimal(report["balances"][ASSET]), Decimal("0"))
            self.assertFalse(report["real_trading"])
            self.assertFalse(report["transaction_broadcast"])

            source_refs = profile_store.list_source_trade_refs(profile_id)
            self.assertEqual(len(source_refs), 1)
            self.assertEqual(source_refs[0]["source_signature"], SIGNATURE)
            self.assertEqual(source_refs[0]["order_id"], report["orders"][0]["order_id"])

            # Simulate application restart by rebuilding every persistent store
            # from the temporary test databases, without touching user data.
            restarted_signatures = SignatureStore(signatures_db)
            restarted_profiles = ProfileStore(profile_db)
            restarted_simulator = PaperSimulator(db_path=paper_db)

            self.assertEqual(restarted_signatures.get_status(WALLET, SIGNATURE), "processed")
            self.assertEqual(
                restarted_profiles.list_source_trade_refs(profile_id)[0]["source_signature"],
                SIGNATURE,
            )
            restarted_report = restarted_simulator.report()
            self.assertEqual(len(restarted_report["orders"]), 1)
            self.assertEqual(restarted_report["orders"][0]["status"], "FILLED")
            self.assertEqual(len(restarted_report["fills"]), 1)
            self.assertEqual(restarted_report["balances"], report["balances"])


if __name__ == "__main__":
    unittest.main()
