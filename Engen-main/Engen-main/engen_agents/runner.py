"""Bounded task runner for the optional Engen software agents."""

from .config import load_settings
from .registry import build_agent_system


async def run_task(task: str) -> str:
    """Run one user-requested engineering task through the coordinator."""
    if not isinstance(task, str) or not task.strip():
        raise ValueError("Provide a non-empty engineering task.")

    settings = load_settings()

    try:
        from agents import Runner
    except ImportError as exc:
        raise RuntimeError(
            "OpenAI Agents SDK is unavailable. Install requirements-agents.txt."
        ) from exc

    system = build_agent_system(settings)
    result = await Runner.run(
        system.coordinator,
        input=task.strip(),
        max_turns=settings.max_turns,
    )
    return str(result.final_output)
