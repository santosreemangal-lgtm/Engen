import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dashboard import server
from engine.paper_trader import PaperTrader


USDT = server.USDT_MINT


class CopyDashboardIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        folder = Path(self.tempdir.name)
        self.settings_path = folder / "copy_settings.json"
        self.ledger = PaperTrader({USDT: "10000"}, db_path=folder / "paper.db")
        self.settings_patch = patch.object(server, "COPY_SETTINGS_FILE", self.settings_path)
        self.ledger_patch = patch.object(server, "copy_paper_trader", self.ledger)
        self.settings_patch.start()
        self.ledger_patch.start()
        self.addCleanup(self.settings_patch.stop)
        self.addCleanup(self.ledger_patch.stop)

    def result(self, quote_mint=USDT, side="BUY_CANDIDATE"):
        if side == "BUY_CANDIDATE":
            asset_raw, quote_raw = "500", "-1000000"
        else:
            asset_raw, quote_raw = "-500", "1000000"
        return {
            "signature": "signature-" + side,
            "available": True,
            "status": "success",
            "trade_signal": {
                "classification": side,
                "asset_mints": ["TOKEN_A"],
                "quote_mints": [quote_mint],
            },
            "token_balance_changes": [
                {"mint": "TOKEN_A", "delta_amount": asset_raw, "decimals": 2},
                {"mint": quote_mint, "delta_amount": quote_raw, "decimals": 6},
            ],
        }

    def test_percentage_setting_scales_virtual_buy(self):
        self.settings_path.write_text('{"mode":"percentage","value":"50"}', encoding="utf-8")
        result = server.handle_monitored_result("public-wallet", self.result())
        self.assertEqual(result["paper_result"]["status"], "PAPER_FILLED")
        self.assertEqual(self.ledger.get_balances()[USDT], "9999.5")
        self.assertEqual(self.ledger.get_balances()["TOKEN_A"], "2.5")
        self.assertFalse(result["real_trading"])

    def test_fixed_usdt_setting_scales_trade_to_configured_quote_amount(self):
        self.settings_path.write_text('{"mode":"fixed","value":"10"}', encoding="utf-8")
        result = server.handle_monitored_result("public-wallet", self.result())
        self.assertEqual(result["paper_result"]["status"], "PAPER_FILLED")
        self.assertEqual(self.ledger.get_balances()[USDT], "9990")
        self.assertEqual(self.ledger.get_balances()["TOKEN_A"], "50")

    def test_unsupported_quote_token_is_skipped(self):
        result = server.handle_monitored_result("public-wallet", self.result(quote_mint="USDC"))
        self.assertEqual(result["status"], "SKIPPED")
        self.assertEqual(self.ledger.get_balances(), {USDT: "10000"})

    def test_duplicate_signature_cannot_double_copy(self):
        transaction = self.result()
        first = server.handle_monitored_result("public-wallet", transaction)
        second = server.handle_monitored_result("public-wallet", transaction)
        self.assertEqual(first["paper_result"]["status"], "PAPER_FILLED")
        self.assertEqual(second["paper_result"]["status"], "DUPLICATE")
        self.assertEqual(len(self.ledger.trades), 1)

    def test_fixed_setting_is_actual_usdt_size_not_raw_token_units(self):
        self.settings_path.write_text('{"mode":"fixed","value":"10"}', encoding="utf-8")
        result = server.handle_monitored_result("public-wallet", self.result())
        self.assertEqual(result["paper_result"]["quote_amount"], "10")


if __name__ == "__main__":
    unittest.main()
