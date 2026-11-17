"""Safe process configuration for the optional Codex workspace tool."""

from pathlib import Path
from typing import Mapping, Optional
import os
import tempfile


# Only these non-secret variables may reach shell commands started by Codex.
# In particular, OPENAI_API_KEY, CODEX_API_KEY, GitHub tokens, and bot tokens
# are deliberately excluded.
SHELL_ENV_INCLUDE_ONLY = (
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "TERM",
    "PYTHONPATH",
    "VIRTUAL_ENV",
    "CARGO_HOME",
    "RUSTUP_HOME",
    "CARGO_TARGET_DIR",
)


def build_codex_options(
    environ: Optional[Mapping[str, str]] = None,
    home_path: Optional[Path] = None,
) -> dict:
    """Return codex_tool options with API credentials withheld from shell children.

    Codex itself receives CODEX_API_KEY (or the app's OPENAI_API_KEY fallback)
    to authenticate. Codex's shell_environment_policy then passes only the
    explicit non-secret allowlist to commands it launches.
    """
    env = os.environ if environ is None else environ
    api_key = str(env.get("CODEX_API_KEY") or env.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise ValueError("A private Codex or OpenAI API key is required.")

    if home_path is None:
        uid = getattr(os, "getuid", lambda: 0)()
        home_path = Path(tempfile.gettempdir()) / f"engen-agent-codex-home-{uid}"

    home = Path(home_path).expanduser().resolve()
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(home, 0o700)
    except OSError:
        pass

    include_only = ", ".join(f'"{name}"' for name in SHELL_ENV_INCLUDE_ONLY)
    config_content = (
        'approval_policy = "on-request"\n'
        'sandbox_mode = "workspace-write"\n'
        'allow_login_shell = false\n'
        '\n'
        '[shell_environment_policy]\n'
        'inherit = "core"\n'
        'ignore_default_excludes = false\n'
        f'include_only = [{include_only}]\n'
    )
    config_path = home / "config.toml"
    config_path.write_text(config_content, encoding="utf-8")
    try:
        os.chmod(config_path, 0o600)
    except OSError:
        pass

    codex_env = {
        "PATH": str(env.get("PATH", "/usr/local/bin:/usr/bin:/bin")),
        "HOME": str(home),
        "CODEX_HOME": str(home),
        "CODEX_API_KEY": api_key,
        "TMPDIR": str(env.get("TMPDIR", tempfile.gettempdir())),
        "TERM": str(env.get("TERM", "dumb")),
    }

    # Pass through only known build-tool paths/settings. Never copy the whole
    # parent environment into the Codex subprocess.
    for name in ("LANG", "LC_ALL", "USER", "PYTHONPATH", "VIRTUAL_ENV",
                 "CARGO_HOME", "RUSTUP_HOME", "CARGO_TARGET_DIR"):
        value = str(env.get(name, "")).strip()
        if value:
            codex_env[name] = value

    return {"env": codex_env}
