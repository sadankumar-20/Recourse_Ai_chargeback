#!/usr/bin/env python3
"""Serve the Recourse API + dashboard.

Usage: python3 scripts/serve.py [--db demo.db] [--port 8000]
Requires: python3 scripts/demo_seed.py first (or point --db anywhere).
Clock is pinned to the synthetic world's sim_now (shown in /health).
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.api import create_app  # noqa: E402

p = argparse.ArgumentParser()
p.add_argument("--db", default=str(ROOT / "demo.db"))
p.add_argument("--port", type=int, default=8000)
args = p.parse_args()
if not Path(args.db).exists():
    sys.exit(f"{args.db} not found — run python3 scripts/demo_seed.py first")
app = create_app(args.db, data_dir=ROOT / "data")
print(f"Recourse dashboard: http://127.0.0.1:{args.port}")
app.run(port=args.port, debug=False)
