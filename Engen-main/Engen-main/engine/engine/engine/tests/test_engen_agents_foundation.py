"""Offline tests for the optional Engen agent foundation."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from engen_agents.config import AgentConfigurationError, load_settings
from engen_agents import tools


class AgentConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.env = {
            "ENGEN_AGENTS_ENABLED": "true",
            "ENGINE_MODE": "read_only",
            "LIVE_TRADING_ENABLED": "false",
            "OPENAI_API_KEY": "test-placeholder-not-a-real-key",
        }

    def test_valid_configuration_uses_bounded_defaults(self):
        settings = load_settings(self.env)
        self.assertEqual(settings.model, "gpt-6.1-sol")
        self.assertEqual(settings.max_turns, 8)

    def test_runtime_is_opt_in(self):
        env = dict(self.env)
        env["ENGEN_AGENTS_ENABLED"] = "false"
        with self.assertRaises(AgentConfigurationError):
            load_settings(env)

    def test_api_key_must_be_present(self):
        env = dict(self.env)
        env.pop("OPENAI_API_KEY")
        with self.assertRaises(AgentConfigurationError):
            load_settings(env)

    def test_non_read_only_mode_is_rejected(self):
        env = dict(self.env)
        env["ENGINE_MODE"] = "live"
        with self.assertRaises(AgentConfigurationError):
            load_settings(env)

    def test_live_trading_flag_is_rejected(self):
        env = dict(self.env)
        env["LIVE_TRADING_ENABLED"] = "true"
        with self.assertRaises(AgentConfigurationError):
            load_settings(env)

    def test_turn_limit_is_enforced(self):
        env = dict(self.env)
        env["ENGEN_AGENT_MAX_TURNS"] = "13"
        with self.assertRaises(AgentConfigurationError):
            load_settings(env)


class AgentReadOnlyToolTests(unittest.TestCase):
    def test_reads_allowlisted_source_file(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sample.py").write_text("VALUE = 7\n", encoding="utf-8")
            with patch.object(tools, "PROJECT_ROOT", root):
                self.assertEqual(tools.read_project_file("sample.py"), "VALUE = 7\n")

    def test_blocks_parent_path_traversal(self):
        with self.assertRaises(ValueError):
            tools._safe_relative_path("../outside.py")

    def test_blocks_environment_file(self):
        with self.assertRaises(ValueError):
            tools._safe_relative_path(".env")

    def test_blocks_directory_as_file_path(self):
        with self.assertRaises(ValueError):
            tools._safe_relative_path(".")

    def test_blocks_runtime_data_directory(self):
        with self.assertRaises(ValueError):
            tools._safe_relative_path("data/private.py")

    def test_blocks_unapproved_file_types(self):
        with self.assertRaises(ValueError):
            tools._safe_relative_path("secrets.json")

    def test_redacts_secret_like_assignments(self):
        sample = (
            "OPENAI_API_KEY=sk-fake-example-key-value-123456\n"
            "print('safe')\n"
        )
        redacted = tools._redact_secret_like_text(sample)
        self.assertNotIn("fake-example", redacted)
        self.assertIn("[REDACTED]", redacted)
        self.assertIn("print('safe')", redacted)

    def test_resolved_symlink_into_runtime_data_is_blocked(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "data"
            data_dir.mkdir()
            target = data_dir / "private.py"
            target.write_text("PRIVATE = True\n", encoding="utf-8")
            (root / "source_link.py").symlink_to(target)
            with patch.object(tools, "PROJECT_ROOT", root):
                with self.assertRaises(ValueError):
                    tools.read_project_file("source_link.py")

    def test_reads_repository_root_architecture_document(self):
        content = tools.read_project_file("ARCHITECTURE_DECISIONS.md")
        self.assertIn("read-only", content.lower())

    def test_blocks_oversized_files(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "large.py").write_text(
                "x" * (tools.MAX_FILE_BYTES + 1), encoding="utf-8"
            )
            with patch.object(tools, "PROJECT_ROOT", root):
                with self.assertRaises(ValueError):
                    tools.read_project_file("large.py")


if __name__ == "__main__":
    unittest.main()
