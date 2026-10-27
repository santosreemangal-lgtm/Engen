"""Compatibility entry point for the canonical nested Engen project.

The maintained application lives in Engen-main/Engen-main/main.py.
"""
from pathlib import Path
import runpy

PROJECT_ROOT = Path(__file__).resolve().parent / "Engen-main" / "Engen-main"
ENTRY_POINT = PROJECT_ROOT / "main.py"

if not ENTRY_POINT.is_file():
    raise SystemExit("Engen entry point is missing: %s" % ENTRY_POINT)

runpy.run_path(str(ENTRY_POINT), run_name="__main__")
