import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dashboard import server


class DashboardWalletValidationTests(unittest.TestCase):
    def test_accepts_valid_32_byte_base58_public_key(self):
        self.assertTrue(
            server.is_valid_solana_address(
                "11111111111111111111111111111111"
            )
        )

    def test_rejects_wrong_decoded_length(self):
        self.assertFalse(
            server.is_valid_solana_address(
                "1111111111111111111111111111111"
            )
        )

    def test_rejects_invalid_base58_alphabet(self):
        self.assertFalse(
            server.is_valid_solana_address(
                "0OIl11111111111111111111111111111"
            )
        )

    def test_rejects_non_string_values(self):
        for value in (None, 123, [], {}):
            with self.subTest(value=value):
                self.assertFalse(server.is_valid_solana_address(value))

    def test_load_wallets_filters_invalid_entries_and_caps_count(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wallets.json"
            valid = "11111111111111111111111111111111"
            path.write_text(
                json.dumps([valid, "not-a-public-key", valid]),
                encoding="utf-8",
            )
            with patch.object(server, "WALLETS_FILE", path):
                self.assertEqual(server.load_wallets(), [valid])


if __name__ == "__main__":
    unittest.main()
