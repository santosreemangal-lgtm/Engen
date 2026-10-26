"""Repository-root entry point for the nested Engen project.

The actual application lives in Engen-main/Engen-main. Keep this wrapper
small so the root command runs the same read-only health check as that project.
"""
from pathlib import Path
import runpy
import sys


PROJECT_ROOT = Path(__file__).resolve().parent / "Engen-main" / "Engen-main"
PROJECT_ENTRY = PROJECT_ROOT / "main.py"

if not PROJECT_ENTRY.is_file():
    raise SystemExit("Engen project entry point was not found.")

sys.path.insert(0, str(PROJECT_ROOT))
runpy.run_path(str(PROJECT_ENTRY), run_name="__main__")
