"""Read-only source inspection tools exposed to Engen agents.

No terminal, network, database, credential, or file-write tools are provided.
"""

from pathlib import Path
from typing import List
import re

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ALLOWED_SUFFIXES = {".py", ".rs", ".md", ".toml", ".yml", ".yaml", ".txt"}
BLOCKED_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__",
    "data", "logs", "target", "dist", "build",
}
BLOCKED_SUFFIXES = {
    ".db", ".sqlite", ".sqlite3", ".log", ".key", ".pem", ".seed", ".secret",
}
MAX_FILE_BYTES = 64 * 1024

_SECRET_ASSIGNMENT = re.compile(
    r"(?im)^([ \t]*(?:OPENAI_API_KEY|TELEGRAM_BOT_TOKEN|DASHBOARD_AUTH_TOKEN)"
    r"[ \t]*=[ \t]*).+$"
)
_OPENAI_KEY_LIKE = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")


def _safe_relative_path(relative_path: str) -> Path:
    if not isinstance(relative_path, str):
        raise ValueError("A relative project path is required.")
    value = relative_path.strip().replace("\\", "/")
    if not value or value.startswith("/") or "\x00" in value:
        raise ValueError("Only relative project paths are allowed.")

    parts = Path(value).parts
    if any(part in {"", ".."} for part in parts):
        raise ValueError("Path traversal is not allowed.")
    if any(part.lower() in BLOCKED_DIRS for part in parts):
        raise ValueError("This project directory is not available to agents.")

    lowered_name = parts[-1].lower()
    if lowered_name.startswith(".env") or lowered_name == "wallets.json":
        raise ValueError("Credential and runtime-state files are not available to agents.")

    suffix = Path(lowered_name).suffix
    if suffix not in ALLOWED_SUFFIXES or suffix in BLOCKED_SUFFIXES:
        raise ValueError("This file type is not available to agents.")

    return Path(*parts)


def _redact_secret_like_text(text: str) -> str:
    text = _SECRET_ASSIGNMENT.sub(r"\1[REDACTED]", text)
    return _OPENAI_KEY_LIKE.sub("[REDACTED_OPENAI_KEY]", text)


def read_project_file(relative_path: str) -> str:
    """Read one allowlisted source/documentation file below the Engen app root."""
    relative = _safe_relative_path(relative_path)
    root = PROJECT_ROOT.resolve()
    candidate = (root / relative).resolve(strict=True)

    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("The requested path resolves outside the project.") from exc

    if not candidate.is_file():
        raise ValueError("The requested project path is not a regular file.")

    size = candidate.stat().st_size
    if size > MAX_FILE_BYTES:
        raise ValueError(
            f"File exceeds the {MAX_FILE_BYTES}-byte agent inspection limit."
        )

    try:
        content = candidate.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("The requested file is not UTF-8 text.") from exc
    except OSError as exc:
        raise ValueError("The requested file could not be read.") from exc

    return _redact_secret_like_text(content)


def build_read_only_tools() -> List[object]:
    """Create SDK function tools lazily so offline tests need no SDK install."""
    try:
        from agents.decorators import tool
    except ImportError as exc:
        raise RuntimeError(
            "Install the optional agent dependency with "
            "python -m pip install -r requirements-agents.txt."
        ) from exc

    return [tool(read_project_file)]
