import tempfile
import unittest
from pathlib import Path

from dashboard import server


PROFILE_ID = "7a6a1c5e-9c61-4f13-aab2-6ef2d1f8b345"


class MonitorPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_state_file = server.MONITOR_STATE_FILE
        server.MONITOR_STATE_FILE = Path(self.temp_dir.name) / "monitoring_state.json"

    def tearDown(self):
        server.MONITOR_STATE_FILE = self.original_state_file
        self.temp_dir.cleanup()

    def test_explicit_monitor_opt_ins_persist_and_reload(self):
        saved = server.save_monitor_state({
            "global_enabled": True,
            "profile_ids": [PROFILE_ID, PROFILE_ID],
        })
        self.assertEqual(saved, {
            "global_enabled": True,
            "profile_ids": [PROFILE_ID],
        })
        self.assertEqual(server.load_monitor_state(), saved)

    def test_missing_or_invalid_state_fails_closed(self):
        self.assertEqual(
            server.load_monitor_state(),
            {"global_enabled": False, "profile_ids": []},
        )
        server.MONITOR_STATE_FILE.write_text("{invalid", encoding="utf-8")
        self.assertEqual(
            server.load_monitor_state(),
            {"global_enabled": False, "profile_ids": []},
        )

    def test_invalid_profile_id_is_rejected_before_persistence(self):
        with self.assertRaises(ValueError):
            server.save_monitor_state({
                "global_enabled": False,
                "profile_ids": ["../../not-a-profile"],
            })
        self.assertFalse(server.MONITOR_STATE_FILE.exists())

    def test_remote_bind_requires_trusted_proxy_and_strong_token(self):
        server.validate_server_config("127.0.0.1", "")
        with self.assertRaises(ValueError):
            server.validate_server_config("0.0.0.0", "")
        with self.assertRaises(ValueError):
            server.validate_server_config("0.0.0.0", "x" * 32)
        server.validate_server_config("0.0.0.0", "x" * 32, trust_proxy=True)

    def test_short_auth_token_is_rejected_even_on_localhost(self):
        with self.assertRaises(ValueError):
            server.validate_server_config("127.0.0.1", "too-short")


if __name__ == "__main__":
    unittest.main()
