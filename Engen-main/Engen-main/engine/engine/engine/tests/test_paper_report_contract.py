import unittest

from dashboard.server import calculate_portfolio_value_usd


class PortfolioValueContractTests(unittest.TestCase):
    def test_missing_virtual_cash_is_not_reported_as_zero(self):
        self.assertIsNone(calculate_portfolio_value_usd(None, []))

    def test_known_cash_without_holdings_is_the_portfolio_value(self):
        self.assertEqual(calculate_portfolio_value_usd("125.50", []), "125.50")

    def test_fresh_holding_values_are_added_exactly(self):
        self.assertEqual(
            calculate_portfolio_value_usd("100.10", ["0.20", "3.45"]),
            "103.75",
        )

    def test_missing_holding_value_makes_total_unavailable(self):
        self.assertIsNone(calculate_portfolio_value_usd("100", [None]))

    def test_invalid_or_non_finite_values_are_unavailable(self):
        self.assertIsNone(calculate_portfolio_value_usd("NaN", []))
        self.assertIsNone(calculate_portfolio_value_usd("100", ["Infinity"]))


if __name__ == "__main__":
    unittest.main()
