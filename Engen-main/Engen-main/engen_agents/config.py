"""Fail-closed configuration for the optional Engen agent runtime."""

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional
import os


class AgentConfigurationError(ValueError):
    """Raised when the agent runtime is not safely configured."""


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class AgentSettings:
    model: str
    max_turns: int
    codex_enabled: bool
    workspace_path: str


def _parse_bool(value: object, name: str) -> bool:
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    raise AgentConfigurationError(f"{name} must be true or false.")


def load_settings(environ: Optional[Mapping[str, str]] = None) -> AgentSettings:
    """Load settings without copying API credentials into the settings object.

    The main Agents SDK reads OPENAI_API_KEY from the application environment.
    The Codex subprocess receives its own explicit environment and applies a
    second allowlist before any shell command is launched.
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

    model = str(env.get("ENGEN_AGENT_MODEL", "gpt-5.6-sol")).strip()
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

    codex_enabled = _parse_bool(
        env.get("ENGEN_CODEX_ENABLED", "true"), "ENGEN_CODEX_ENABLED"
    )

    raw_workspace = str(env.get("ENGEN_CODEX_WORKDIR", str(REPOSITORY_ROOT))).strip()
    try:
        workspace = Path(raw_workspace).expanduser().resolve(strict=True)
        workspace.relative_to(REPOSITORY_ROOT.resolve())
    except (OSError, ValueError) as exc:
        raise AgentConfigurationError(
            "ENGEN_CODEX_WORKDIR must be an existing directory inside the Engen repository."
        ) from exc
    if not workspace.is_dir():
        raise AgentConfigurationError("ENGEN_CODEX_WORKDIR must be a directory.")

    if codex_enabled and not (
        str(env.get("CODEX_API_KEY", "")).strip()
        or str(env.get("OPENAI_API_KEY", "")).strip()
    ):
        raise AgentConfigurationError(
            "The workspace agent needs CODEX_API_KEY or OPENAI_API_KEY."
        )

    return AgentSettings(
        model=model,
        max_turns=max_turns,
        codex_enabled=codex_enabled,
        workspace_path=str(workspace),
    )
