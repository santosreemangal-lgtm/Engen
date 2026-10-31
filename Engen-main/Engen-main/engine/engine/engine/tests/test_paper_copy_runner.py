import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

from engine.paper_copy_runner import queue_paper_orders, execute_pending_paper_orders
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