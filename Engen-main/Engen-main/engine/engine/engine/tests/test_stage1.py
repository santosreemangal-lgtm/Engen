import io
import logging
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import main as app
from engine.config import ConfigError, load_config
from engine.health import CheckResult, get_status, run_checks
from engine.logger import redact, setup_logging


class ConfigTests(unittest.TestCase):
    def test_defaults_are_safe(self):
        cfg = load_config({})
        self.assertEqual(cfg.engine_mode, "read_only")
        self.assertEqual(cfg.log_level, "INFO")
        self.assertFalse(cfg.live_trading_enabled)

    def test_spaces_and_case_are_handled(self):
        cfg = load_config({"ENGINE_MODE": "  READ_ONLY ", "LOG_LEVEL": " debug "})
        self.assertEqual(cfg.engine_mode, "read_only")
        self.assertEqual(cfg.log_level, "DEBUG")

    def test_empty_values_use_defaults(self):
        cfg = load_config({"ENGINE_MODE": "", "LOG_LEVEL": "", "LIVE_TRADING_ENABLED": ""})
        self.assertEqual(cfg.engine_mode, "read_only")
        self.assertEqual(cfg.log_level, "INFO")

    def test_bad_mode_is_rejected(self):
        with self.assertRaises(ConfigError):
            load_config({"ENGINE_MODE": "live"})

    def test_paper_mode_not_available_yet(self):
        with self.assertRaises(ConfigError):
            load_config({"ENGINE_MODE": "paper"})

    def test_bad_log_level_is_rejected(self):
        with self.assertRaises(ConfigError):
            load_config({"LOG_LEVEL": "LOUD"})

    def test_live_trading_cannot_be_enabled(self):
        for value in ("true", "1", "yes", "on", "TRUE", "anything"):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError):
                    load_config({"LIVE_TRADING_ENABLED": value})

    def test_live_trading_off_values_are_accepted(self):
        for value in ("false", "FALSE", "0", "no", "off", ""):
            with self.subTest(value=value):
                self.assertFalse(load_config({"LIVE_TRADING_ENABLED": value}).live_trading_enabled)

    def test_all_problems_reported_together(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config({"ENGINE_MODE": "x", "LOG_LEVEL": "y", "LIVE_TRADING_ENABLED": "true"})
        message = str(ctx.exception)
        self.assertIn("ENGINE_MODE", message)
        self.assertIn("LOG_LEVEL", message)
        self.assertIn("Live trading", message)

    def test_config_is_immutable(self):
        cfg = load_config({})
        with self.assertRaises(Exception):
            cfg.live_trading_enabled = True

    def test_config_error_does_not_echo_untrusted_values(self):
        secret_like_value = "do-not-print-this-value"
        with self.assertRaises(ConfigError) as caught:
            load_config({
                "ENGINE_MODE": secret_like_value,
                "LOG_LEVEL": "also-not-valid",
                "LIVE_TRADING_ENABLED": "true",
            })
        self.assertNotIn(secret_like_value, str(caught.exception))
        self.assertNotIn("also-not-valid", str(caught.exception))


class HealthTests(unittest.TestCase):
    def test_status_fields(self):
        status = get_status(load_config({}))
        self.assertEqual(status["status"], "ok")
        self.assertEqual(status["stage"], 1)
        self.assertFalse(status["live_trading_enabled"])
        self.assertTrue(status["emergency_stop_engaged"])

    def test_all_checks_pass_by_default(self):
        results = run_checks(load_config({}))
        self.assertTrue(all(isinstance(r, CheckResult) for r in results))
        self.assertTrue(all(r.passed for r in results))

    def test_missing_files_are_detected(self):
        with tempfile.TemporaryDirectory() as empty_dir:
            results = run_checks(load_config({}), project_root=Path(empty_dir))
        failed = [r for r in results if not r.passed]
        self.assertEqual(len(failed), 1)
        self.assertIn("missing", failed[0].detail)


class LoggerTests(unittest.TestCase):
    def test_redaction(self):
        self.assertNotIn("abc123", redact("api_key=abc123"))
        self.assertNotIn("xyz789", redact("Token: xyz789"))
        self.assertNotIn("hunter2", redact("password = hunter2"))
        self.assertEqual(redact("normal message"), "normal message")
        self.assertNotIn("TOPSECRET", redact('GET /api?token=TOPSECRET HTTP/1.1'))

    def test_setup_is_repeatable_without_duplicate_handlers(self):
        setup_logging("INFO")
        setup_logging("DEBUG")
        logger = logging.getLogger("engine")
        handlers = [h for h in logger.handlers if getattr(h, "_engine_handler", False)]
        self.assertEqual(len(handlers), 1)
        self.assertEqual(logger.level, logging.DEBUG)

    def test_secret_is_hidden_in_real_output(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            logging.getLogger("engine").handlers.clear()
            setup_logging("INFO")
            logging.getLogger("engine.test").info("connecting with api_key=SUPERSECRET")
        self.assertNotIn("SUPERSECRET", buffer.getvalue())
        self.assertIn("[REDACTED]", buffer.getvalue())


class StartupTests(unittest.TestCase):
    def _run(self, env):
        buffer = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(buffer):
            code = app.main()
        return code, buffer.getvalue()

    def test_app_starts_cleanly(self):
        code, out = self._run({})
        self.assertEqual(code, 0)
        self.assertIn('"status": "ok"', out)
        self.assertIn("startup_ms", out)

    def test_app_refuses_unsafe_config(self):
        code, out = self._run({"LIVE_TRADING_ENABLED": "true"})
        self.assertEqual(code, 1)
        self.assertIn("CONFIG ERROR", out)

    def test_app_reports_failed_health(self):
        failing = [CheckResult("Fake check", False, "forced failure")]
        with mock.patch("main.run_checks", return_value=failing):
            code, out = self._run({})
        self.assertEqual(code, 1)
        self.assertIn('"status": "failed"', out)


if __name__ == "__main__":
    unittest.main()
