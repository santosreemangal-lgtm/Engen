import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

from engine.config import ConfigError, load_max_source_age_seconds
from engine.paper_copy_runner import queue_paper_orders, execute_pending_paper_orders, source_trade_is_fresh
from engine.paper_simulator import PaperSimulator
from engine.pnl import MarketPrice


WALLET = "11111111111111111111111111111111"
ASSET = "TokenMint1111111111111111111111111111111111"
QUOTE = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


class FakeSimulator:
    def __init__(self):
        self.orders = {}
        self.balances = {"USD": Decimal("1000"), ASSET: Decimal("10")}

    def create_order(self, signal_id, side, asset_mint, quantity, quote_mint, *, created_at, details=None):
        if signal_id in self.orders:
            return {"status": "DUPLICATE", "signal_id": signal_id}
        order = {
            "order_id": "paper-order-1",
            "signal_id": signal_id,
            "side": side,
            "asset_mint": asset_mint,
            "quantity": quantity,
            "quote_mint": quote_mint,
            "status": "CREATED",
            "details": details or {},
        }
        self.orders[signal_id] = order
        return order


def candidate_result(*, confirmation="finalized", classification="BUY_CANDIDATE"):
    asset_delta = "2500000" if classification == "BUY_CANDIDATE" else "-2500000"
    quote_delta = "-10000000" if classification == "BUY_CANDIDATE" else "10000000"
    asset_pre = "0" if classification == "BUY_CANDIDATE" else "10000000"
    asset_post = "2500000" if classification == "BUY_CANDIDATE" else "7500000"
    return {
        "signature": "sig-test-1",
        "status": "success",
        "metadata_complete": True,
        "signers": [WALLET],
        "signature_observation": {
            "confirmation_status": confirmation,
            "slot": 123,
            "block_time": int(datetime.now(timezone.utc).timestamp()),
        },
        "trade_signal": {
            "classification": classification,
            "asset_mints": [ASSET],
            "quote_mints": [QUOTE],
        },
        "token_balance_changes": [
            {
                "owner": WALLET, "mint": ASSET, "decimals": 6,
                "delta_amount": asset_delta,
                "pre_amount": asset_pre,
                "post_amount": asset_post,
            },
            {
                "owner": WALLET, "mint": QUOTE, "decimals": 6,
                "delta_amount": quote_delta,
            },
        ],
    }


class PaperCopyRunnerTests(unittest.TestCase):
    def test_confirmed_candidate_creates_deduplicated_paper_order(self):
        simulator = FakeSimulator()
        queued = queue_paper_orders(WALLET, [candidate_result()], simulator)

        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["side"], "BUY")
        self.assertEqual(queued[0]["quantity"], "1")
        self.assertEqual(queued[0]["target_notional_usd"], "10")
        self.assertEqual(queued[0]["sizing_mode"], "SOURCE_PERCENT")
        self.assertEqual(queued[0]["sizing_value"], "5")
        self.assertEqual(queued[0]["source_quote_mint"], QUOTE)
        self.assertEqual(queued[0]["execution_state"], "AWAITING_VERIFIED_MARKET_DATA")
        self.assertEqual(len(simulator.orders), 1)
        duplicate = queue_paper_orders(WALLET, [candidate_result()], simulator)
        self.assertEqual(duplicate, [])
        order = next(iter(simulator.orders.values()))
        self.assertEqual(order["details"]["origin"], "AUTOMATIC_WALLET_COPY")
        self.assertEqual(order["details"]["source_quote_mint"], QUOTE)
        self.assertEqual(order["quote_mint"], "USD")
        self.assertFalse(order["details"]["real_trading"])
        self.assertFalse(order["details"]["transaction_broadcast"])

    def test_source_percentage_scales_source_token_quantity(self):
        simulator = FakeSimulator()
        queued = queue_paper_orders(
            WALLET, [candidate_result()], simulator,
            copy_settings={"sizing_mode": "SOURCE_PERCENT", "sizing_value": 20, "max_buy_usd": 1000},
        )
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["quantity"], "1")
        self.assertEqual(queued[0]["target_notional_usd"], "200")
        self.assertEqual(queued[0]["sizing_mode"], "SOURCE_PERCENT")

    def test_fixed_usd_size_is_recorded_for_verified_price_conversion(self):
        simulator = FakeSimulator()
        queued = queue_paper_orders(
            WALLET, [candidate_result()], simulator,
            copy_settings={"sizing_mode": "FIXED_USD", "sizing_value": "25", "max_buy_usd": 1000},
        )
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["sizing_mode"], "FIXED_USD")
        self.assertEqual(queued[0]["target_notional_usd"], "25")
        order = next(iter(simulator.orders.values()))
        self.assertEqual(order["details"]["target_notional_usd"], "25")

    def test_fixed_usd_amount_ignores_mirror_only_max_buy_cap(self):
        simulator = FakeSimulator()
        queued = queue_paper_orders(
            WALLET, [candidate_result()], simulator,
            copy_settings={"sizing_mode": "FIXED_USD", "sizing_value": "25", "max_buy_usd": "5"},
        )
        self.assertEqual(len(queued), 1)
        order = next(iter(simulator.orders.values()))
        self.assertEqual(order["details"]["target_notional_usd"], "25")
        self.assertIsNone(order["details"]["max_buy_usd"])

    def test_mirror_amount_respects_max_buy_cap(self):
        simulator = FakeSimulator()
        queued = queue_paper_orders(
            WALLET, [candidate_result()], simulator,
            copy_settings={"sizing_mode": "MIRROR_AMOUNT", "sizing_value": "10", "max_buy_usd": "5"},
        )
        self.assertEqual(len(queued), 1)
        order = next(iter(simulator.orders.values()))
        self.assertEqual(queued[0]["target_notional_usd"], "5")
        self.assertEqual(order["details"]["max_buy_usd"], "5")

    def test_aggregates_multiple_asset_accounts_for_one_swap(self):
        result = candidate_result()
        result["token_balance_changes"][0] = {
            "owner": WALLET, "mint": ASSET, "decimals": 6,
            "delta_amount": "1250000", "pre_amount": "0", "post_amount": "1250000",
        }
        result["token_balance_changes"].append({
            "owner": WALLET, "mint": ASSET, "decimals": 6,
            "delta_amount": "1250000", "pre_amount": "0", "post_amount": "1250000",
        })

        simulator = FakeSimulator()
        queued = queue_paper_orders(WALLET, [result], simulator)

        self.assertEqual(len(queued), 1)
        order = next(iter(simulator.orders.values()))
        self.assertEqual(order["details"]["source_asset_quantity"], "2.5")
        self.assertEqual(order["details"]["source_asset_pre_amount"], "0")
        self.assertEqual(order["details"]["source_asset_post_amount"], "2500000")

    def test_sell_fraction_aggregates_pre_balance_across_asset_accounts(self):
        result = candidate_result(classification="SELL_CANDIDATE")
        result["token_balance_changes"][0] = {
            "owner": WALLET, "mint": ASSET, "decimals": 6,
            "delta_amount": "-1500000", "pre_amount": "6000000", "post_amount": "4500000",
        }
        result["token_balance_changes"].append({
            "owner": WALLET, "mint": ASSET, "decimals": 6,
            "delta_amount": "-1000000", "pre_amount": "4000000", "post_amount": "3000000",
        })

        simulator = FakeSimulator()
        queued = queue_paper_orders(WALLET, [result], simulator)

        self.assertEqual(len(queued), 1)
        order = next(iter(simulator.orders.values()))
        self.assertEqual(Decimal(order["details"]["source_sell_fraction"]), Decimal("0.25"))
        self.assertEqual(order["details"]["source_asset_pre_amount"], "10000000")
        self.assertEqual(order["details"]["source_asset_post_amount"], "7500000")

    def test_wrapped_sol_quote_prefers_direction_matching_token_delta_without_double_counting(self):
        from engine.paper_copy_runner import WRAPPED_SOL_MINT

        result = candidate_result()
        result["trade_signal"]["quote_mints"] = [WRAPPED_SOL_MINT]
        result["token_balance_changes"][1] = {
            "owner": WALLET, "mint": WRAPPED_SOL_MINT, "decimals": 9,
            "delta_amount": "-500000000",
        }
        result["token_balance_changes"].append({
            "owner": WALLET, "mint": WRAPPED_SOL_MINT, "decimals": 9,
            "delta_amount": "-1000000000", "source": "native_sol",
        })

        simulator = FakeSimulator()
        queued = queue_paper_orders(WALLET, [result], simulator)

        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["source_quote_mint"], WRAPPED_SOL_MINT)
        self.assertEqual(queued[0]["target_notional_usd"], "10")
        self.assertEqual(
            next(iter(simulator.orders.values()))["details"]["source_quote_amount"],
            "0.5",
        )

    def test_wrapped_sol_quote_falls_back_to_native_sol_when_token_delta_is_absent(self):
        from engine.paper_copy_runner import WRAPPED_SOL_MINT

        result = candidate_result()
        result["trade_signal"]["quote_mints"] = [WRAPPED_SOL_MINT]
        result["token_balance_changes"] = [
            result["token_balance_changes"][0],
            {
                "owner": WALLET, "mint": WRAPPED_SOL_MINT, "decimals": 9,
                "delta_amount": "-1000000000", "source": "native_sol",
            },
        ]

        simulator = FakeSimulator()
        queued = queue_paper_orders(WALLET, [result], simulator)

        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["source_quote_mint"], WRAPPED_SOL_MINT)
        self.assertEqual(next(iter(simulator.orders.values()))["details"]["source_quote_amount"], "1")

    def test_invalid_sizing_settings_are_rejected(self):
        with self.assertRaises(ValueError):
            queue_paper_orders(
                WALLET, [candidate_result()], FakeSimulator(),
                copy_settings={"sizing_mode": "SOURCE_PERCENT", "sizing_value": 101},
            )
        with self.assertRaises(ValueError):
            queue_paper_orders(
                WALLET, [candidate_result()], FakeSimulator(),
                copy_settings={"sizing_mode": "UNSUPPORTED", "sizing_value": 10},
            )

    def test_source_trade_freshness_uses_60_second_window_and_five_second_future_skew(self):
        stale = candidate_result()
        stale["signature_observation"]["block_time"] = 138
        missing_time = candidate_result()
        missing_time["signature_observation"].pop("block_time")

        fresh = {"signature_observation": {"block_time": 140}}
        self.assertTrue(source_trade_is_fresh(fresh, now=200))
        self.assertTrue(source_trade_is_fresh(
            {"signature_observation": {"block_time": 205}}, now=200
        ))
        self.assertFalse(source_trade_is_fresh(
            {"signature_observation": {"block_time": 206}}, now=200
        ))
        self.assertFalse(source_trade_is_fresh(
            {"signature_observation": {"block_time": 139}}, now=200
        ))
        self.assertFalse(source_trade_is_fresh(
            {"signature_observation": {}}, now=200
        ))

        simulator = FakeSimulator()
        self.assertEqual(queue_paper_orders(WALLET, [stale, missing_time], simulator), [])
        self.assertEqual(simulator.orders, {})

    def test_history_backfill_is_never_queued_as_a_copy_trade(self):
        simulator = FakeSimulator()
        historical = candidate_result()
        historical["history_backfill"] = True
        historical["copy_eligible"] = False
        self.assertEqual(queue_paper_orders(WALLET, [historical], simulator), [])
        self.assertEqual(simulator.orders, {})

    def test_source_age_environment_setting_defaults_and_fails_closed(self):
        self.assertEqual(load_max_source_age_seconds({}), 60)
        self.assertEqual(
            load_max_source_age_seconds({"ENGEN_MAX_SOURCE_AGE_SECONDS": "75"}),
            75,
        )
        for value in ("", "abc", "0", "-1", "1.5"):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError):
                    load_max_source_age_seconds({
                        "ENGEN_MAX_SOURCE_AGE_SECONDS": value
                    })

    def test_queued_order_is_rejected_when_source_expires_before_execution(self):
        simulator = PaperSimulator(initial_balances={"USD": "1000"})
        source = candidate_result()
        source_time = source["signature_observation"]["block_time"]
        queued = queue_paper_orders(WALLET, [source], simulator)
        self.assertEqual(len(queued), 1)

        def unexpected_price_lookup(_mint):
            self.fail("Expired source order must be rejected before market lookup.")

        with patch("engine.paper_copy_runner.time.time", return_value=source_time + 61):
            outcome = execute_pending_paper_orders(
                WALLET, simulator, price_provider=unexpected_price_lookup
            )

        self.assertEqual(outcome[0]["status"], "EXPIRED")
        self.assertEqual(outcome[0]["execution_state"], "EXPIRED_SOURCE")
        order = simulator.report()["orders"][0]
        self.assertEqual(order["status"], "EXPIRED")
        self.assertEqual(order["details"]["execution_state"], "EXPIRED_SOURCE")
        self.assertIn("older than the configured 60-second window", order["reason"])

        # A rejected stale order is terminal and cannot be retried on later scans.
        with patch("engine.paper_copy_runner.time.time", return_value=source_time + 120):
            self.assertEqual(
                execute_pending_paper_orders(
                    WALLET, simulator, price_provider=unexpected_price_lookup
                ),
                [],
            )

        self.assertEqual(simulator.report()["fills"], [])
        self.assertFalse(simulator.report()["real_trading"])
        self.assertFalse(simulator.report()["transaction_broadcast"])

    def test_order_expiring_during_market_lookup_is_rejected_before_fill(self):
        simulator = PaperSimulator(initial_balances={"USD": "1000"})
        source = candidate_result()
        source_time = source["signature_observation"]["block_time"]
        queued = queue_paper_orders(WALLET, [source], simulator)
        self.assertEqual(len(queued), 1)

        with patch(
            "engine.paper_copy_runner.time.time",
            side_effect=[source_time, source_time + 61],
        ):
            outcome = execute_pending_paper_orders(
                WALLET, simulator, price_provider=lambda _mint: None
            )

        self.assertEqual(outcome[0]["status"], "EXPIRED")
        self.assertEqual(outcome[0]["execution_state"], "EXPIRED_SOURCE")
        self.assertEqual(simulator.report()["orders"][0]["status"], "EXPIRED")
        self.assertEqual(simulator.report()["fills"], [])

    def test_legacy_queued_order_without_source_timestamp_is_rejected(self):
        simulator = PaperSimulator(initial_balances={"USD": "1000"})
        simulator.create_order(
            signal_id="wallet-copy:" + WALLET + ":legacy-no-time",
            side="BUY",
            asset_mint=ASSET,
            quantity="1",
            quote_mint="USD",
            created_at=datetime.now(timezone.utc).isoformat(),
            details={
                "origin": "AUTOMATIC_WALLET_COPY",
                "source_wallet": WALLET,
                "source_signature": "legacy-no-time",
                "source_block_time": None,
            },
        )

        outcome = execute_pending_paper_orders(
            WALLET, simulator,
            price_provider=lambda _mint: self.fail(
                "Missing source timestamp must be rejected before market lookup."
            ),
        )

        self.assertEqual(outcome[0]["status"], "EXPIRED")
        self.assertEqual(outcome[0]["execution_state"], "EXPIRED_SOURCE")
        stored = simulator.report()["orders"][0]
        self.assertEqual(stored["status"], "EXPIRED")
        self.assertEqual(stored["details"]["execution_state"], "EXPIRED_SOURCE")
        self.assertEqual(simulator.report()["fills"], [])

    def test_unconfirmed_or_unknown_transaction_is_not_copied(self):
        simulator = FakeSimulator()
        unconfirmed = candidate_result(confirmation="processed")
        unknown = candidate_result(classification="UNKNOWN")
        queued = queue_paper_orders(WALLET, [unconfirmed, unknown], simulator)

        self.assertEqual(queued, [])
        self.assertEqual(simulator.orders, {})

    def test_sell_candidate_maps_to_sell_order(self):
        simulator = FakeSimulator()
        queued = queue_paper_orders(
            WALLET, [candidate_result(classification="SELL_CANDIDATE")], simulator
        )
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["side"], "SELL")

    def test_global_maximum_caps_fixed_and_percentage_buys(self):
        simulator = FakeSimulator()
        queued = queue_paper_orders(
            WALLET, [candidate_result()], simulator,
            copy_settings={"sizing_mode": "SOURCE_PERCENT", "sizing_value": 20, "max_buy_usd": 10},
        )
        self.assertEqual(queued[0]["target_notional_usd"], "10")
        self.assertEqual(queued[0]["sizing_mode"], "SOURCE_PERCENT")

    def test_mirror_amount_uses_stable_quote_notional(self):
        simulator = FakeSimulator()
        queued = queue_paper_orders(
            WALLET, [candidate_result()], simulator,
            copy_settings={"sizing_mode": "MIRROR_AMOUNT", "sizing_value": 10},
        )
        self.assertEqual(queued[0]["target_notional_usd"], "10")

    def test_mirror_amount_queues_sol_quote_and_converts_with_fresh_sol_usd_price(self):
        from engine.paper_copy_runner import WRAPPED_SOL_MINT

        result = candidate_result()
        result["trade_signal"]["quote_mints"] = [WRAPPED_SOL_MINT]
        result["token_balance_changes"][1] = {
            "owner": WALLET, "mint": WRAPPED_SOL_MINT, "decimals": 9,
            "delta_amount": "-500000000",
        }
        simulator = PaperSimulator()
        simulator.add_virtual_balance("USD", "1000")
        queued = queue_paper_orders(
            WALLET, [result], simulator,
            copy_settings={"sizing_mode": "MIRROR_AMOUNT", "sizing_value": 10, "max_buy_usd": 1000},
        )
        self.assertEqual(len(queued), 1)
        self.assertIsNone(queued[0]["target_notional_usd"])
        self.assertEqual(queued[0]["execution_state"], "AWAITING_VERIFIED_MARKET_DATA")

        observed_at = datetime.now(timezone.utc).isoformat()

        def provider(mint):
            if mint == WRAPPED_SOL_MINT:
                return MarketPrice(
                    mint=mint, price=Decimal("200"), currency="USD",
                    source="test-sol-usd", observed_at=observed_at,
                    liquidity_usd=Decimal("50000"), liquidity_source="test-provider",
                    liquidity_observed_at=observed_at,
                )
            self.assertEqual(mint, ASSET)
            return MarketPrice(
                mint=mint, price=Decimal("4"), currency="USD",
                source="test-token-usd", observed_at=observed_at,
                liquidity_usd=Decimal("20000"), liquidity_source="test-provider",
                liquidity_observed_at=observed_at,
            )

        outcomes = execute_pending_paper_orders(WALLET, simulator, provider)
        self.assertEqual(outcomes[0]["status"], "FILLED")
        report = simulator.report()
        self.assertEqual(report["orders"][0]["status"], "FILLED")
        self.assertEqual(
            Decimal(report["orders"][0]["details"]["target_notional_usd"]),
            Decimal("100"),
        )
        self.assertGreater(Decimal(report["balances"][ASSET]), Decimal("0"))
        self.assertFalse(report["real_trading"])
        self.assertFalse(report["transaction_broadcast"])

    def test_sol_quote_mirror_order_waits_when_verified_sol_usd_price_is_missing(self):
        from engine.paper_copy_runner import WRAPPED_SOL_MINT

        result = candidate_result()
        result["trade_signal"]["quote_mints"] = [WRAPPED_SOL_MINT]
        result["token_balance_changes"][1] = {
            "owner": WALLET, "mint": WRAPPED_SOL_MINT, "decimals": 9,
            "delta_amount": "-500000000",
        }
        simulator = PaperSimulator()
        simulator.add_virtual_balance("USD", "1000")
        queue_paper_orders(
            WALLET, [result], simulator,
            copy_settings={"sizing_mode": "MIRROR_AMOUNT", "sizing_value": 10, "max_buy_usd": 1000},
        )
        outcomes = execute_pending_paper_orders(
            WALLET, simulator, price_provider=lambda mint: None
        )
        self.assertEqual(outcomes[0]["status"], "WAITING_FOR_MARKET_DATA")
        self.assertEqual(simulator.report()["orders"][0]["status"], "CREATED")
        self.assertFalse(simulator.report()["real_trading"])
        self.assertFalse(simulator.report()["transaction_broadcast"])

    def test_sell_uses_source_position_fraction_not_buy_sizing(self):
        simulator = FakeSimulator()
        simulator.balances[ASSET] = Decimal("10")
        queued = queue_paper_orders(
            WALLET, [candidate_result(classification="SELL_CANDIDATE")], simulator,
            copy_settings={"sizing_mode": "FIXED_USD", "sizing_value": 100, "max_buy_usd": 5},
        )
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["side"], "SELL")
        self.assertEqual(Decimal(queued[0]["quantity"]), Decimal("1"))
        order = next(iter(simulator.orders.values()))
        self.assertEqual(Decimal(order["details"]["source_sell_fraction"]), Decimal("0.25"))
        self.assertEqual(queued[0]["sizing_mode"], "SOURCE_POSITION_REDUCTION")
        self.assertIsNone(queued[0]["target_notional_usd"])

    def test_sell_without_paper_position_fails_instead_of_waiting_forever(self):
        simulator = PaperSimulator()
        queued = queue_paper_orders(
            WALLET, [candidate_result(classification="SELL_CANDIDATE")], simulator
        )
        self.assertEqual(len(queued), 1)

        outcomes = execute_pending_paper_orders(
            WALLET, simulator, price_provider=lambda mint: None
        )

        self.assertEqual(outcomes[0]["status"], "REJECTED")
        order = simulator.report()["orders"][0]
        self.assertEqual(order["status"], "REJECTED")
        self.assertIn("no position", order["reason"].lower())
        self.assertEqual(simulator.report()["fills"], [])

    def test_sell_remains_preparing_when_an_earlier_copied_buy_is_unresolved(self):
        simulator = PaperSimulator()
        simulator.add_virtual_balance("USD", "1000")
        earlier_buy = candidate_result()
        earlier_buy["signature"] = "sig-earlier-buy"
        earlier_buy["signature_observation"]["slot"] = 122
        later_sell = candidate_result(classification="SELL_CANDIDATE")
        later_sell["signature"] = "sig-later-sell"
        later_sell["signature_observation"]["slot"] = 123

        queued = queue_paper_orders(WALLET, [earlier_buy, later_sell], simulator)
        self.assertEqual([item["side"] for item in queued], ["BUY", "SELL"])

        outcomes = execute_pending_paper_orders(
            WALLET, simulator, price_provider=lambda mint: None
        )

        self.assertEqual(outcomes[0]["status"], "WAITING_FOR_MARKET_DATA")
        self.assertEqual(outcomes[1]["status"], "WAITING_FOR_POSITION")
        sell_order = next(order for order in simulator.report()["orders"] if order["side"] == "SELL")
        self.assertIn("earlier copied BUY", sell_order["reason"])
        self.assertEqual(sell_order["status"], "CREATED")
        self.assertEqual(simulator.report()["fills"], [])

    def test_sell_execution_uses_fraction_of_current_paper_position(self):
        simulator = PaperSimulator(initial_balances={"USD": "1000", ASSET: "10"})
        queued = queue_paper_orders(
            WALLET, [candidate_result(classification="SELL_CANDIDATE")], simulator,
            copy_settings={"sizing_mode": "FIXED_USD", "sizing_value": 100, "max_buy_usd": 5},
        )
        self.assertEqual(len(queued), 1)
        observed_at = datetime.now(timezone.utc).isoformat()

        def provider(mint):
            return MarketPrice(
                mint=mint, price=Decimal("4"), currency="USD",
                source="test-provider", observed_at=observed_at,
                liquidity_usd=Decimal("20000"), liquidity_source="test-provider",
                liquidity_observed_at=observed_at,
            )

        outcomes = execute_pending_paper_orders(WALLET, simulator, provider)
        self.assertEqual(outcomes[0]["status"], "FILLED")
        self.assertEqual(Decimal(simulator.report()["fills"][0]["quantity"]), Decimal("2.5"))
        self.assertEqual(Decimal(simulator.report()["balances"][ASSET]), Decimal("7.5"))
        self.assertFalse(simulator.report()["real_trading"])
        self.assertFalse(simulator.report()["transaction_broadcast"])

    def test_sell_without_known_source_pre_balance_is_skipped(self):
        simulator = FakeSimulator()
        result = candidate_result(classification="SELL_CANDIDATE")
        result["token_balance_changes"][0].pop("pre_amount")
        queued = queue_paper_orders(WALLET, [result], simulator)
        self.assertEqual(queued, [])

    def test_buy_only_mode_does_not_queue_sell_orders(self):
        simulator = FakeSimulator()
        queued = queue_paper_orders(
            WALLET,
            [candidate_result(classification="SELL_CANDIDATE")],
            simulator,
            allowed_sides="BUY",
        )
        self.assertEqual(queued, [])
        self.assertEqual(simulator.orders, {})

    def test_sell_only_mode_does_not_queue_buy_orders(self):
        simulator = FakeSimulator()
        queued = queue_paper_orders(
            WALLET,
            [candidate_result(classification="BUY_CANDIDATE")],
            simulator,
            allowed_sides="SELL",
        )
        self.assertEqual(queued, [])
        self.assertEqual(simulator.orders, {})

    def test_both_mode_allows_buy_and_sell_orders(self):
        buy_simulator = FakeSimulator()
        sell_simulator = FakeSimulator()
        buy = queue_paper_orders(
            WALLET, [candidate_result(classification="BUY_CANDIDATE")],
            buy_simulator, allowed_sides="BOTH",
        )
        sell = queue_paper_orders(
            WALLET, [candidate_result(classification="SELL_CANDIDATE")],
            sell_simulator, allowed_sides="BOTH",
        )
        self.assertEqual([item["side"] for item in buy], ["BUY"])
        self.assertEqual([item["side"] for item in sell], ["SELL"])

    def test_invalid_copy_direction_is_rejected(self):
        with self.assertRaises(ValueError):
            queue_paper_orders(WALLET, [], FakeSimulator(), allowed_sides="SOMETHING")

    def test_pending_copy_order_fills_only_with_fresh_liquid_market_data(self):
        simulator = PaperSimulator()
        simulator.add_virtual_balance("USD", "1000")
        queued = queue_paper_orders(WALLET, [candidate_result()], simulator)
        self.assertEqual(len(queued), 1)

        observed_at = datetime.now(timezone.utc).isoformat()

        def provider(mint):
            self.assertEqual(mint, ASSET)
            return MarketPrice(
                mint=mint,
                price=Decimal("4"),
                currency="USD",
                source="test-provider",
                observed_at=observed_at,
                liquidity_usd=Decimal("20000"),
                liquidity_source="test-provider",
                liquidity_observed_at=observed_at,
            )

        outcomes = execute_pending_paper_orders(WALLET, simulator, provider)
        self.assertEqual(outcomes[0]["status"], "FILLED")
        report = simulator.report()
        self.assertEqual(report["orders"][0]["status"], "FILLED")
        self.assertEqual(report["balances"][ASSET], "2.5")
        self.assertLess(Decimal(report["balances"]["USD"]), Decimal("1000"))
        self.assertFalse(report["real_trading"])
        self.assertFalse(report["transaction_broadcast"])

    def test_fixed_usd_paper_order_uses_fresh_price_to_calculate_quantity(self):
        simulator = PaperSimulator()
        simulator.add_virtual_balance("USD", "1000")
        queued = queue_paper_orders(
            WALLET, [candidate_result()], simulator,
            copy_settings={"sizing_mode": "FIXED_USD", "sizing_value": "25", "max_buy_usd": 1000},
        )
        self.assertEqual(len(queued), 1)
        observed_at = datetime.now(timezone.utc).isoformat()

        def provider(mint):
            return MarketPrice(
                mint=mint, price=Decimal("4"), currency="USD",
                source="test-provider", observed_at=observed_at,
                liquidity_usd=Decimal("20000"), liquidity_source="test-provider",
                liquidity_observed_at=observed_at,
            )

        outcomes = execute_pending_paper_orders(WALLET, simulator, provider)
        self.assertEqual(outcomes[0]["status"], "FILLED")
        report = simulator.report()
        self.assertEqual(report["orders"][0]["quantity"], "6.25")
        self.assertEqual(report["balances"][ASSET], "6.25")
        self.assertFalse(report["real_trading"])
        self.assertFalse(report["transaction_broadcast"])

    def test_buy_waits_for_virtual_balance_then_fills_without_losing_signal(self):
        simulator = PaperSimulator()
        queued = queue_paper_orders(WALLET, [candidate_result()], simulator)

        self.assertEqual(len(queued), 1)
        self.assertIsNone(queued[0]["target_notional_usd"])
        self.assertEqual(queued[0]["execution_state"], "AWAITING_VIRTUAL_BALANCE")

        waiting = execute_pending_paper_orders(
            WALLET, simulator, price_provider=lambda mint: None
        )
        self.assertEqual(waiting[0]["status"], "WAITING_FOR_VIRTUAL_BALANCE")
        self.assertEqual(simulator.report()["orders"][0]["status"], "CREATED")

        simulator.add_virtual_balance("USD", "1000")
        observed_at = datetime.now(timezone.utc).isoformat()

        def provider(mint):
            return MarketPrice(
                mint=mint, price=Decimal("4"), currency="USD",
                source="test-provider", observed_at=observed_at,
                liquidity_usd=Decimal("20000"), liquidity_source="test-provider",
                liquidity_observed_at=observed_at,
            )

        outcomes = execute_pending_paper_orders(WALLET, simulator, provider)
        self.assertEqual(outcomes[0]["status"], "FILLED")
        report = simulator.report()
        self.assertEqual(report["orders"][0]["status"], "FILLED")
        self.assertLess(Decimal(report["balances"]["USD"]), Decimal("1000"))
        self.assertGreater(Decimal(report["balances"][ASSET]), Decimal("0"))
        self.assertFalse(report["real_trading"])
        self.assertFalse(report["transaction_broadcast"])

    def test_missing_market_data_leaves_copy_order_queued_with_visible_reason(self):
        simulator = PaperSimulator()
        simulator.add_virtual_balance("USD", "1000")
        queue_paper_orders(WALLET, [candidate_result()], simulator)
        outcomes = execute_pending_paper_orders(
            WALLET, simulator, price_provider=lambda mint: None
        )
        self.assertEqual(outcomes[0]["status"], "WAITING_FOR_MARKET_DATA")
        report_order = simulator.report()["orders"][0]
        self.assertEqual(report_order["status"], "CREATED")
        self.assertIn("No usable public USD price", report_order["reason"])
        self.assertEqual(report_order["details"]["execution_state"], "WAITING_FOR_MARKET_DATA")

    def test_small_paper_order_fills_with_five_thousand_dollar_liquidity(self):
        simulator = PaperSimulator()
        simulator.add_virtual_balance("USD", "1000")
        queued = queue_paper_orders(
            WALLET, [candidate_result()], simulator,
            copy_settings={"sizing_mode": "FIXED_USD", "sizing_value": "10", "max_buy_usd": 100},
        )
        self.assertEqual(len(queued), 1)
        observed_at = datetime.now(timezone.utc).isoformat()

        def provider(mint):
            return MarketPrice(
                mint=mint, price=Decimal("4"), currency="USD",
                source="test-provider", observed_at=observed_at,
                liquidity_usd=Decimal("5000"), liquidity_source="test-provider",
                liquidity_observed_at=observed_at,
            )

        outcomes = execute_pending_paper_orders(WALLET, simulator, provider)
        self.assertEqual(outcomes[0]["status"], "FILLED")
        self.assertEqual(simulator.report()["orders"][0]["status"], "FILLED")

    def test_buy_sized_to_entire_virtual_balance_reserves_fee_and_slippage(self):
        simulator = PaperSimulator()
        simulator.add_virtual_balance("USD", "10")
        queued = queue_paper_orders(
            WALLET, [candidate_result()], simulator,
            copy_settings={"sizing_mode": "FIXED_USD", "sizing_value": "10", "max_buy_usd": 10},
        )
        self.assertEqual(len(queued), 1)
        observed_at = datetime.now(timezone.utc).isoformat()

        def provider(mint):
            return MarketPrice(
                mint=mint, price=Decimal("4"), currency="USD",
                source="test-provider", observed_at=observed_at,
                liquidity_usd=Decimal("5000"), liquidity_source="test-provider",
                liquidity_observed_at=observed_at,
            )

        outcomes = execute_pending_paper_orders(WALLET, simulator, provider)
        self.assertEqual(outcomes[0]["status"], "FILLED")
        report = simulator.report()
        self.assertEqual(report["orders"][0]["status"], "FILLED")
        self.assertGreaterEqual(Decimal(report["balances"]["USD"]), Decimal("0"))

    def test_large_paper_order_requires_liquidity_relative_to_order_size(self):
        simulator = PaperSimulator()
        simulator.add_virtual_balance("USD", "5000")
        queued = queue_paper_orders(
            WALLET, [candidate_result()], simulator,
            copy_settings={"sizing_mode": "FIXED_USD", "sizing_value": "1000", "max_buy_usd": 1000},
        )
        self.assertEqual(len(queued), 1)
        observed_at = datetime.now(timezone.utc).isoformat()

        def provider(mint):
            return MarketPrice(
                mint=mint, price=Decimal("4"), currency="USD",
                source="test-provider", observed_at=observed_at,
                liquidity_usd=Decimal("5000"), liquidity_source="test-provider",
                liquidity_observed_at=observed_at,
            )

        outcomes = execute_pending_paper_orders(WALLET, simulator, provider)
        self.assertEqual(outcomes[0]["status"], "WAITING_FOR_LIQUIDITY")
        order = simulator.report()["orders"][0]
        self.assertIn("below the configured USD minimum", order["reason"])
        self.assertEqual(order["status"], "CREATED")
        self.assertEqual(order["details"]["execution_state"], "WAITING_FOR_LIQUIDITY")

    def test_unknown_quote_mint_queues_mirror_order_awaiting_verified_usd_price(self):
        result = candidate_result()
        unknown_quote = "UnknownQuoteMint111111111111111111111111"
        result["trade_signal"]["quote_mints"] = [unknown_quote]
        result["token_balance_changes"][1]["mint"] = unknown_quote

        simulator = FakeSimulator()
        queued = queue_paper_orders(
            WALLET, [result], simulator,
            copy_settings={"sizing_mode": "SOURCE_PERCENT", "sizing_value": 5, "max_buy_usd": 10},
        )
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["target_notional_usd"], "10")

        mirror_simulator = FakeSimulator()
        mirrored = queue_paper_orders(
            WALLET, [result], mirror_simulator,
            copy_settings={"sizing_mode": "MIRROR_AMOUNT", "sizing_value": 10, "max_buy_usd": 1000},
        )
        self.assertEqual(len(mirrored), 1)
        self.assertIsNone(mirrored[0]["target_notional_usd"])
        self.assertEqual(mirrored[0]["execution_state"], "AWAITING_VERIFIED_MARKET_DATA")
        self.assertEqual(len(mirror_simulator.orders), 1)


    def test_fill_commit_rechecks_source_age_and_does_not_change_balances(self):
        simulator = PaperSimulator(initial_balances={"USD": "1000"})
        source_time = int(datetime.now(timezone.utc).timestamp())
        order = simulator.create_order(
            signal_id="wallet-copy:" + WALLET + ":commit-age",
            side="BUY",
            asset_mint=ASSET,
            quantity="1",
            quote_mint="USD",
            created_at=datetime.now(timezone.utc).isoformat(),
            details={
                "origin": "AUTOMATIC_WALLET_COPY",
                "source_wallet": WALLET,
                "source_signature": "1" * 64,
                "source_block_time": source_time,
                "source_slot": 123,
                "target_notional_usd": "10",
            },
        )
        observed_at = datetime.now(timezone.utc).isoformat()
        market = MarketPrice(
            mint=ASSET, price=Decimal("2"), currency="USD",
            source="test-provider", observed_at=observed_at,
            liquidity_usd=Decimal("5000"), liquidity_source="test-provider",
            liquidity_observed_at=observed_at,
        )
        before = dict(simulator.balances)
        with patch("engine.paper_simulator.time.time", return_value=source_time + 61):
            result = simulator.execute_order(
                order["order_id"], market, now=observed_at,
                min_liquidity_usd=0, max_liquidity_age_seconds=60,
                require_liquidity=True,
            )
        self.assertEqual(result["status"], "EXPIRED")
        self.assertEqual(simulator.orders[order["order_id"]]["status"], "EXPIRED")
        self.assertEqual(simulator.balances, before)
        self.assertEqual(simulator.fills, [])

    def test_expired_buy_rejects_later_dependent_sell(self):
        simulator = PaperSimulator(initial_balances={"USD": "1000"})
        stale_time = int(datetime.now(timezone.utc).timestamp()) - 61
        buy = simulator.create_order(
            signal_id="wallet-copy:" + WALLET + ":expired-buy",
            side="BUY", asset_mint=ASSET, quantity="1", quote_mint="USD",
            created_at="2026-11-01T00:00:00+00:00",
            details={
                "origin": "AUTOMATIC_WALLET_COPY", "source_wallet": WALLET,
                "source_signature": "1" * 64, "source_block_time": stale_time,
                "source_slot": 100,
            },
        )
        sell = simulator.create_order(
            signal_id="wallet-copy:" + WALLET + ":dependent-sell",
            side="SELL", asset_mint=ASSET, quantity="1", quote_mint="USD",
            created_at="2026-11-01T00:00:01+00:00",
            details={
                "origin": "AUTOMATIC_WALLET_COPY", "source_wallet": WALLET,
                "source_signature": "2" * 64, "source_block_time": stale_time + 1,
                "source_slot": 101,
            },
        )
        outcomes = execute_pending_paper_orders(
            WALLET, simulator,
            price_provider=lambda _mint: self.fail("Expired dependent orders must not request a price."),
        )
        self.assertEqual(simulator.orders[buy["order_id"]]["status"], "EXPIRED")
        self.assertEqual(simulator.orders[sell["order_id"]]["status"], "REJECTED")
        self.assertEqual(
            simulator.orders[sell["order_id"]]["details"]["dependency_order_id"],
            buy["order_id"],
        )
        self.assertEqual(simulator.fills, [])

if __name__ == "__main__":
    unittest.main()
