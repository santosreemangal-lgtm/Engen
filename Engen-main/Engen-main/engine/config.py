"""Loads and validates settings from environment variables.

Stage 1 is read-only: live trading is blocked on purpose.
All problems are collected and reported together, so you can fix
everything in one go.
"""
import os
from dataclasses import asdict, dataclass
from typing import Mapping, Optional

ALLOWED_MODES = ("read_only",)
VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
LIVE_OFF_VALUES = ("", "false", "0", "no", "off")


class ConfigError(Exception):
    """Raised when a setting is invalid or unsafe."""


@dataclass(frozen=True)
class Config:
    engine_mode: str
    log_level: str
    live_trading_enabled: bool

    def as_dict(self):
        return asdict(self)


def _read(env, name, default):
    """Reads a setting, trimming spaces. Missing or None uses the default."""
    value = env.get(name)
    if value is None:
        return default
    return str(value).strip()


def load_config(env: Optional[Mapping[str, str]] = None) -> Config:
    env = os.environ if env is None else env
    problems = []

    mode = _read(env, "ENGINE_MODE", "read_only").lower() or "read_only"
    if mode not in ALLOWED_MODES:
        problems.append(
            "ENGINE_MODE must be one of the supported safe modes: %s"
            % (list(ALLOWED_MODES),)
        )

    level = _read(env, "LOG_LEVEL", "INFO").upper() or "INFO"
    if level not in VALID_LOG_LEVELS:
        problems.append(
            "LOG_LEVEL must be one of the supported levels: %s"
            % (list(VALID_LOG_LEVELS),)
        )

    live = _read(env, "LIVE_TRADING_ENABLED", "false").lower()
    if live not in LIVE_OFF_VALUES:
        problems.append(
            "Live trading is not available in Stage 1. "
            "Remove LIVE_TRADING_ENABLED or set it to false."
        )

    if problems:
        raise ConfigError("\n".join("- " + p for p in problems))

    return Config(engine_mode=mode, log_level=level, live_trading_enabled=False)
