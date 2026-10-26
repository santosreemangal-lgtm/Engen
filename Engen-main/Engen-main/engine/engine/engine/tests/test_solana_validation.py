import unittest

from engine.solana_validation import (
    is_valid_solana_address,
    is_valid_transaction_signature,
    require_solana_address,
    require_transaction_signature,
)


class SolanaValidationTests(unittest.TestCase):
    def test_accepts_known_32_byte_public_address(self):
        self.assertTrue(is_valid_solana_address("11111111111111111111111111111111"))
        self.assertEqual(
            require_solana_address("11111111111111111111111111111111"),
            "11111111111111111111111111111111",
        )

    def test_rejects_invalid_public_addresses(self):
        for value in ("", "not-a-wallet", "1", None, 123):
            with self.subTest(value=value):
                self.assertFalse(is_valid_solana_address(value))
                with self.assertRaises(ValueError):
                    require_solana_address(value)

    def test_accepts_64_byte_base58_signature(self):
        signature = "1" * 63 + "2"
        self.assertTrue(is_valid_transaction_signature(signature))
        self.assertEqual(require_transaction_signature(signature), signature)

    def test_rejects_invalid_transaction_signatures(self):
        for value in ("", "short-signature", "1", None, 123):
            with self.subTest(value=value):
                self.assertFalse(is_valid_transaction_signature(value))
                with self.assertRaises(ValueError):
                    require_transaction_signature(value)


if __name__ == "__main__":
    unittest.main()
