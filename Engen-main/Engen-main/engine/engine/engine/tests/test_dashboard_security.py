import unittest
from unittest.mock import patch


class DashboardSecurityDefaultsTests(unittest.TestCase):
    def test_dashboard_binds_to_localhost_by_default(self):
        with patch.dict("os.environ", {}, clear=True):
            import importlib
            import dashboard.server as server

            importlib.reload(server)
            self.assertEqual(server.HOST, "127.0.0.1")

    def test_dashboard_host_can_be_explicitly_overridden(self):
        with patch.dict("os.environ", {"DASHBOARD_HOST": "0.0.0.0"}):
            import importlib
            import dashboard.server as server

            importlib.reload(server)
            self.assertEqual(server.HOST, "0.0.0.0")


if __name__ == "__main__":
    unittest.main()
