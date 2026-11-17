"""Fail-closed configuration for the optional Engen agent runtime."""

from dataclasses import dataclass
from typing import Mapping, Optional
import os


class AgentConfigurationError(ValueError):
    """Raised when the agent runtime is not safely configured."""


@dataclass(frozen=True)
class AgentSettings:
    model: str
    max_turns: int


def load_settings(environ: Optional[Mapping[str, str]] = None) -> AgentSettings:
    """Load settings without reading or printing the API key.

    The SDK reads OPENAI_API_KEY from the process environment. This function
    only checks that it exists; the secret is never copied into AgentSettings.
    """
    env = os.environ if environ is None else environ

    enabled = str(env.get("ENGEN_AGENTS_ENABLED", "false")).strip().lower()
    if enabled != "true":
        raise AgentConfigurationError(
            "Agent runtime is disabled. Set ENGEN_AGENTS_ENABLED=true to opt in."
        )

    engine_mode = str(env.get("ENGINE_MODE", "read_only")).strip().lower()
    if engine_mode != "read_only":
        raise AgentConfigurationError(
            "Agent runtime requires ENGINE_MODE=read_only."
        )

    live_value = str(env.get("LIVE_TRADING_ENABLED", "false")).strip().lower()
    if live_value not in {"false", "0", "no", "off", ""}:
        raise AgentConfigurationError(
            "Agent runtime refuses to start unless LIVE_TRADING_ENABLED is false."
        )

    if not str(env.get("OPENAI_API_KEY", "")).strip():
        raise AgentConfigurationError(
            "OPENAI_API_KEY is missing. Configure it as a private environment secret; "
            "never paste it into chat or commit it."
        )

    model = str(env.get("ENGEN_AGENT_MODEL", "gpt-5.6")).strip()
    if not model or len(model) > 100:
        raise AgentConfigurationError("ENGEN_AGENT_MODEL must be a non-empty model ID.")

    raw_max_turns = str(env.get("ENGEN_AGENT_MAX_TURNS", "8")).strip()
    try:
        max_turns = int(raw_max_turns)
    except ValueError as exc:
        raise AgentConfigurationError(
            "ENGEN_AGENT_MAX_TURNS must be an integer from 1 to 12."
        ) from exc
    if not 1 <= max_turns <= 12:
        raise AgentConfigurationError(
            "ENGEN_AGENT_MAX_TURNS must be between 1 and 12."
        )

    return AgentSettings(model=model, max_turns=max_turns)
