import json
import os
import urllib.error
import unittest
from unittest.mock import patch

import engine.rpc_policy as rpc_policy
from engine.rpc_policy import RPCHTTPError, post_json_rpc


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class RPCPolicyTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "ENGEN_RPC_RETRIES": "1",
            "ENGEN_RPC_BACKOFF_SECONDS": "0",
            "ENGEN_RPC_MIN_INTERVAL_SECONDS": "0",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.cooldown_state = patch.multiple(
            rpc_policy, _rate_limit_streak=0, _cooldown_until=0.0
        )
        self.cooldown_state.start()
        self.addCleanup(self.cooldown_state.stop)

    def test_rpc_url_requires_https_for_remote_hosts(self):
        from engine.rpc_policy import validate_rpc_url
        self.assertEqual(
            validate_rpc_url("https://rpc.example/path"),
            "https://rpc.example/path",
        )
        self.assertEqual(
            validate_rpc_url("http://127.0.0.1:8899"),
            "http://127.0.0.1:8899",
        )
        for value in (
            "http://rpc.example",
            "file:///etc/passwd",
            "https://user:password@rpc.example",
            "https://rpc.example/#fragment",
            "",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_rpc_url(value)

    def test_rpc_timeout_must_be_finite_and_bounded(self):
        from engine.rpc_policy import validate_rpc_timeout
        self.assertEqual(validate_rpc_timeout(10), 10.0)
        for value in (0, -1, 61, float("inf"), float("nan"), True, "bad"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_rpc_timeout(value)

    @patch("engine.rpc_policy._pace_requests")
    @patch("engine.rpc_policy.urllib.request.urlopen")
    def test_rejects_insecure_remote_url_before_network_request(self, mock_urlopen, _pace):
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            post_json_rpc({"method": "getHealth"}, "http://rpc.example", 2)
        mock_urlopen.assert_not_called()

    def test_invalid_rpc_retry_configuration_fails_clearly(self):
        for name, value in (
            ("ENGEN_RPC_RETRIES", "many"),
            ("ENGEN_RPC_RETRIES", "-1"),
            ("ENGEN_RPC_BACKOFF_SECONDS", "NaN"),
            ("ENGEN_RPC_MIN_INTERVAL_SECONDS", "61"),
        ):
            with self.subTest(name=name, value=value):
                with patch.dict(os.environ, {name: value}):
                    with self.assertRaises(ValueError):
                        post_json_rpc(
                            {"method": "getHealth"},
                            "https://rpc.example",
                            2,
                        )

    @patch("engine.rpc_policy._pace_requests")
    @patch("engine.rpc_policy.urllib.request.urlopen")
    def test_returns_json_rpc_response(self, mock_urlopen, _pace):
        mock_urlopen.return_value = _Response({"result": "ok"})
        result = post_json_rpc(
            {"method": "getHealth"},
            "https://rpc.example",
            2,
        )
        self.assertEqual(result, {"result": "ok"})
        mock_urlopen.assert_called_once()

    @patch("engine.rpc_policy._pace_requests")
    @patch("engine.rpc_policy.urllib.request.urlopen")
    def test_retries_transient_network_error(self, mock_urlopen, _pace):
        mock_urlopen.side_effect = [
            urllib.error.URLError("temporary"),
            _Response({"result": "ok"}),
        ]
        result = post_json_rpc(
            {"method": "getHealth"},
            "https://rpc.example",
            2,
        )
        self.assertEqual(result, {"result": "ok"})
        self.assertEqual(mock_urlopen.call_count, 2)

    @patch("engine.rpc_policy._pace_requests")
    @patch("engine.rpc_policy.urllib.request.urlopen")
    def test_http_429_enters_shared_cooldown_without_retrying(self, mock_urlopen, _pace):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "https://rpc.example", 429, "rate limited", {"Retry-After": "45"}, None
        )
        with self.assertRaises(RPCHTTPError) as caught:
            post_json_rpc({"method": "getSlot"}, "https://rpc.example", 2)
        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(caught.exception.diagnostics["failure_kind"], "http_429")
        self.assertEqual(caught.exception.diagnostics["retry_after"], "45")
        self.assertGreaterEqual(
            caught.exception.diagnostics["cooldown_seconds"], 45
        )
        self.assertEqual(mock_urlopen.call_count, 1)

        # The process-wide cooldown blocks the next request before the provider
        # is contacted again.
        with self.assertRaises(RPCHTTPError) as cooldown:
            post_json_rpc({"method": "getSlot"}, "https://rpc.example", 2)
        self.assertEqual(
            cooldown.exception.diagnostics["failure_kind"], "http_429_cooldown"
        )
        self.assertEqual(mock_urlopen.call_count, 1)

    @patch("engine.rpc_policy._pace_requests")
    @patch("engine.rpc_policy.urllib.request.urlopen")
    def test_final_error_does_not_expose_rpc_url(self, mock_urlopen, _pace):
        mock_urlopen.side_effect = urllib.error.URLError(
            "https://rpc.example/?api-key=SECRET"
        )
        with self.assertRaisesRegex(RuntimeError, "request failed") as caught:
            post_json_rpc(
                {"method": "getHealth"},
                "https://rpc.example/?api-key=SECRET",
                2,
            )
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertNotIn("rpc.example", str(caught.exception))
        self.assertEqual(mock_urlopen.call_count, 2)

    @patch("engine.rpc_policy._pace_requests")
    @patch("engine.rpc_policy.urllib.request.urlopen")
    def test_retry_count_is_capped_even_if_environment_is_unreasonable(
        self, mock_urlopen, _pace,
    ):
        with patch.dict(os.environ, {
            "ENGEN_RPC_RETRIES": "999999",
            "ENGEN_RPC_BACKOFF_SECONDS": "0",
            "ENGEN_RPC_MIN_INTERVAL_SECONDS": "0",
        }):
            mock_urlopen.side_effect = urllib.error.URLError("offline")
            with self.assertRaises(RuntimeError):
                post_json_rpc({"method": "getHealth"}, "https://rpc.example", 1)
        self.assertEqual(mock_urlopen.call_count, 6)

    @patch("engine.rpc_policy._pace_requests")
    @patch("engine.rpc_policy.urllib.request.urlopen")
    def test_invalid_json_response_is_not_retried(self, mock_urlopen, _pace):
        response = _Response({"result": "unused"})
        response.read = lambda: b"not-json"
        mock_urlopen.return_value = response
        with self.assertRaisesRegex(RuntimeError, "invalid response"):
            post_json_rpc(
                {"method": "getHealth"},
                "https://rpc.example",
                2,
            )
        mock_urlopen.assert_called_once()


if __name__ == "__main__":
    unittest.main()
