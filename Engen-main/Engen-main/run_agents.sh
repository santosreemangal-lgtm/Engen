#!/usr/bin/env bash
# Safe one-command launcher for the optional Engen software-agent team.
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$SCRIPT_DIR"

if [[ -z "${OPENAI_API_KEY:-}" ]]; then
  echo "Blocked: OPENAI_API_KEY is not available in this Codespace." >&2
  echo "Check the repository Codespaces secret; never print or paste the key." >&2
  exit 2
fi

export ENGEN_AGENTS_ENABLED=true
export ENGINE_MODE=read_only
export LIVE_TRADING_ENABLED=false

if ! python -c 'import agents' >/dev/null 2>&1; then
  echo "Installing the optional Engen Agents SDK dependency..."
  python -m pip install -r requirements-agents.txt
fi

if [[ "${1:-}" == "--preflight" ]]; then
  python - <<'PY'
from engen_agents.config import load_settings
from engen_agents.registry import build_agent_system

settings = load_settings()
system = build_agent_system(settings)
assert system.coordinator.name == "Engen Master Coordinator"
assert len(system.specialists) == 5
print("PREFLIGHT PASSED: configuration and all six agent definitions initialized.")
print("No OpenAI API request was made; no key value was displayed.")
PY
  exit 0
fi

if [[ "$#" -eq 0 ]]; then
  echo 'Usage: bash run_agents.sh "Describe one software-engineering task"'
  echo "For a no-API startup check, use: bash run_agents.sh --preflight"
  exit 2
fi

exec python -m engen_agents "$@"
