"""Basic health and status checks."""
import platform
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REQUIRED_FILES = (
    "main.py",
    "engine/__init__.py",
    "engine/config.py",
    "engine/logger.py",
    "engine/health.py",
)


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str


def get_status(config):
    return {
        "status": "ok",
        "stage": 1,
        "engine_mode": config.engine_mode,
        "live_trading_enabled": config.live_trading_enabled,
        "python_version": platform.python_version(),
        "time_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def run_checks(config, project_root=None):
    """Runs all Stage 1 checks and returns a list of CheckResult."""
    root = Path(project_root) if project_root else Path(__file__).resolve().parent.parent
    missing = [f for f in REQUIRED_FILES if not (root / f).is_file()]

    return [
        CheckResult(
            "Python version is 3.8 or newer",
            sys.version_info >= (3, 8),
            platform.python_version(),
        ),
        CheckResult(
            "Required project files exist",
            not missing,
            "all present" if not missing else "missing: " + ", ".join(missing),
        ),
        CheckResult(
            "Engine mode is read_only",
            config.engine_mode == "read_only",
            config.engine_mode,
        ),
        CheckResult(
            "Live trading is disabled",
            config.live_trading_enabled is False,
            str(config.live_trading_enabled),
        ),
    ]
