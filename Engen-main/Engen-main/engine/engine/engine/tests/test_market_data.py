import json
import unittest
from decimal import Decimal
from unittest.mock import patch
from urllib.error import URLError

from engine import market_data


TOKEN = "TokenMint1111111111111111111111111111111111"
OTHER = "TokenMint2222222222222222222222222222222222"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit=-1):
        return self.payload[:limit]


def pair(*, chain="solana", base=TOKEN, quote=USDC, price_usd="2.5",
         price_native="2", liquidity="5000"):
    return {
        "chainId": chain,
        "baseToken": {"address": base},
        "quoteToken": {"address": quote},
        "priceUsd": price_usd,
        "priceNative": price_native,
        "liquidity": {"usd": liquidity},
    }


class MarketDataTests(unittest.TestCase):
    def setUp(self):
        market_data.clear_market_price_cache()
        self.addCleanup(market_data.clear_market_price_cache)
        self.valid_address = patch(
            "engine.market_data.is_valid_solana_address", return_value=True
        )
        self.valid_address.start()
        self.addCleanup(self.valid_address.stop)

    def call_with_payload(self, payload, mint=TOKEN):
        raw = json.dumps(payload).encode("utf-8")
        with patch("engine.market_data.urlopen", return_value=FakeResponse(raw)):
            return market_data.fetch_market_price(mint)

    def test_base_token_price_is_returned(self):
        result = self.call_with_payload([pair(price_usd="2.5")])
        self.assertIsNotNone(result)
        self.assertEqual(result.mint, TOKEN)
        self.assertEqual(result.price, Decimal("2.5"))
        self.assertEqual(result.liquidity_usd, Decimal("5000"))

    def test_successful_price_snapshot_is_cached_for_repeated_requests(self):
        raw = json.dumps([pair(price_usd="2.5")]).encode("utf-8")
        with patch("engine.market_data.urlopen", return_value=FakeResponse(raw)) as mocked:
            first = market_data.fetch_market_price(TOKEN)
            second = market_data.fetch_market_price(TOKEN)
        self.assertIsNotNone(first)
        self.assertIs(first, second)
        mocked.assert_called_once()

    def test_highest_liquidity_pool_is_selected(self):
        result = self.call_with_payload([
            pair(price_usd="2", liquidity="5000"),
            pair(price_usd="3", liquidity="9000"),
        ])
        self.assertEqual(result.price, Decimal("3"))
        self.assertEqual(result.liquidity_usd, Decimal("9000"))

    def test_non_solana_pair_is_ignored(self):
        result = self.call_with_payload([pair(chain="ethereum")])
        self.assertIsNone(result)

    def test_wrong_token_address_is_ignored(self):
        result = self.call_with_payload([pair(base=OTHER)])
        self.assertIsNone(result)

    def test_non_list_payload_is_ignored(self):
        result = self.call_with_payload({"pairs": [pair()]})
        self.assertIsNone(result)

    def test_empty_payload_returns_none(self):
        self.assertIsNone(self.call_with_payload([]))

    def test_invalid_or_zero_price_is_ignored(self):
        result = self.call_with_payload([
            pair(price_usd="NaN"),
            pair(price_usd="0"),
            pair(price_usd="-1"),
        ])
        self.assertIsNone(result)

    def test_invalid_liquidity_is_ignored(self):
        result = self.call_with_payload([
            pair(liquidity="NaN"),
            pair(liquidity="-1"),
            pair(liquidity="0"),
        ])
        self.assertIsNone(result)

    def test_wrapped_sol_quote_price_is_derived_from_usd_and_native_price(self):
        result = self.call_with_payload(
            [pair(base=OTHER, quote=market_data.WRAPPED_SOL_MINT,
                  price_usd="200", price_native="2", liquidity="10000")],
            mint=market_data.WRAPPED_SOL_MINT,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.mint, market_data.WRAPPED_SOL_MINT)
        self.assertEqual(result.price, Decimal("100"))
        self.assertEqual(result.liquidity_usd, Decimal("10000"))

    def test_arbitrary_quote_token_is_not_mispriced_from_price_native(self):
        result = self.call_with_payload(
            [pair(base=OTHER, quote=TOKEN, price_usd="200",
                  price_native="2", liquidity="10000")],
            mint=TOKEN,
        )
        self.assertIsNone(result)

    def test_invalid_timeout_is_rejected(self):
        with self.assertRaises(ValueError):
            market_data.fetch_market_price(TOKEN, timeout=0)

    def test_network_error_returns_none(self):
        with patch("engine.market_data.urlopen", side_effect=URLError("offline")):
            self.assertIsNone(market_data.fetch_market_price(TOKEN))

    def test_invalid_address_skips_network_request(self):
        with patch("engine.market_data.is_valid_solana_address", return_value=False):
            with patch("engine.market_data.urlopen") as mocked:
                self.assertIsNone(market_data.fetch_market_price("invalid"))
                mocked.assert_not_called()


if __name__ == "__main__":
    unittest.main()
