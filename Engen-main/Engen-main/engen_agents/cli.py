"""Command-line entry point for Engen software agents."""

import argparse
import asyncio
import sys

from .config import AgentConfigurationError
from .runner import run_task


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m engen_agents",
        description="Run the opt-in Engen software engineering coordinator.",
    )
    parser.add_argument(
        "task",
        nargs="+",
        help="One software engineering task for the coordinator.",
    )
    args = parser.parse_args(argv)

    try:
        output = asyncio.run(run_task(" ".join(args.task)))
    except AgentConfigurationError as exc:
        print(f"Configuration blocked: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Agent run interrupted by user.", file=sys.stderr)
        return 130
    except Exception as exc:
        # Do not print exception text: provider/client errors may contain request details.
        print(
            f"Agent run failed ({type(exc).__name__}). "
            "Check local configuration and provider status without sharing secrets.",
            file=sys.stderr,
        )
        return 1

    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
