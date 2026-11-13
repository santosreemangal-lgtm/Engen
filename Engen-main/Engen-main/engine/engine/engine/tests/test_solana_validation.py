import json
import unittest
from pathlib import Path

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


    def test_shared_validation_golden_corpus_matches_rust_reference(self):
        project_root = Path(__file__).resolve().parents[4]
        fixture_path = (
            project_root / "rust-engine" / "tests" / "fixtures"
            / "solana_validation.json"
        )
        corpus = json.loads(fixture_path.read_text(encoding="utf-8"))

        for address in corpus["valid_addresses"]:
            with self.subTest(kind="valid_address", value=address):
                self.assertTrue(is_valid_solana_address(address))
                self.assertEqual(require_solana_address(address), address)
        for address in corpus["invalid_addresses"]:
            with self.subTest(kind="invalid_address", value=address):
                self.assertFalse(is_valid_solana_address(address))
                with self.assertRaises(ValueError):
                    require_solana_address(address)
        for signature in corpus["valid_signatures"]:
            with self.subTest(kind="valid_signature"):
                self.assertTrue(is_valid_transaction_signature(signature))
                self.assertEqual(require_transaction_signature(signature), signature)
        for signature in corpus["invalid_signatures"]:
            with self.subTest(kind="invalid_signature", value=signature):
                self.assertFalse(is_valid_transaction_signature(signature))
                with self.assertRaises(ValueError):
                    require_transaction_signature(signature)



if __name__ == "__main__":
    unittest.main()
