"""Compatibility entry point for the canonical nested Engen project."""
from pathlib import Path
import runpy
import sys

PROJECT_ROOT = Path(__file__).resolve().parent / "Engen-main" / "Engen-main"
ENTRY_POINT = PROJECT_ROOT / "main.py"

if not ENTRY_POINT.is_file():
    raise SystemExit("Engen entry point is missing: %s" % ENTRY_POINT)

sys.path.insert(0, str(PROJECT_ROOT))
runpy.run_path(str(ENTRY_POINT), run_name="__main__")
