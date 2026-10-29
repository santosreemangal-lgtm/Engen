import ast
import unittest
from pathlib import Path

from engine import paper_trader
from dataclasses import replace

from engine.safety import (
    EMERGENCY_STOP,
    EmergencyStopLatch,
    LiveExecutionDisabledError,
    block_live_execution,
)


class EmergencyStopTests(unittest.TestCase):
    def test_default_latch_is_engaged_after_startup(self):
        self.assertTrue(EMERGENCY_STOP.engaged)
        self.assertTrue(EMERGENCY_STOP.reason)

    def test_live_execution_is_blocked_by_default(self):
        with self.assertRaises(LiveExecutionDisabledError):
            block_live_execution()

    def test_latch_has_no_public_disarm_method(self):
        self.assertFalse(hasattr(EMERGENCY_STOP, "disarm"))
        self.assertFalse(hasattr(EMERGENCY_STOP, "release"))

    def test_even_disengaged_instance_cannot_authorize_live_execution(self):
        attempted_override = replace(
            EmergencyStopLatch(),
            engaged=False,
            reason="test override",
        )
        with self.assertRaisesRegex(
            LiveExecutionDisabledError,
            "disabled by project policy",
        ):
            attempted_override.require_execution_permitted()

    def test_paper_trader_has_no_network_or_signing_imports(self):
        source = Path(paper_trader.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".")[0])

        self.assertTrue(imported_roots.isdisjoint({
            "solana", "solders", "urllib", "requests", "httpx"
        }))
        self.assertNotIn("sendTransaction", source)
        self.assertNotIn("send_raw_transaction", source)
        self.assertNotIn("sign_transaction", source)


if __name__ == "__main__":
    unittest.main()
