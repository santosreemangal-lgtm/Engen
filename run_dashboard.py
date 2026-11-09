#!/usr/bin/env python3
"""Run the Engen paper dashboard with pending-copy status enabled.

Usage (from repo root):
  python run_dashboard.py

Paper only: no keys, no signing, no broadcast.
"""
from pathlib import Path
import runpy
import sys

HERE = Path(__file__).resolve().parent
CANDIDATES = [
    HERE / "Engen-main" / "Engen-main" / "dashboard" / "server_with_pending.py",
    HERE / "dashboard" / "server_with_pending.py",
]
entry = next((p for p in CANDIDATES if p.is_file()), None)
if entry is None:
    raise SystemExit("dashboard/server_with_pending.py not found")
root = entry.parent.parent
if str(root) not in sys.path:
    sys.path.insert(0, str(root))
sys.argv[0] = str(entry)
runpy.run_path(str(entry), run_name="__main__")
