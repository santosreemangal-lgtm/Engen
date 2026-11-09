#!/usr/bin/env python3
"""Run paper dashboard with pending-copy from Engen-main/Engen-main."""
from pathlib import Path
import runpy
import sys

HERE = Path(__file__).resolve().parent
entry = HERE / "dashboard" / "server_with_pending.py"
if not entry.is_file():
    raise SystemExit("dashboard/server_with_pending.py not found")
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
sys.argv[0] = str(entry)
runpy.run_path(str(entry), run_name="__main__")
