#!/usr/bin/env python3
"""Print the Admissibility Gate report over the generated dataset.

Usage: python3 scripts/gate_report.py [--data-dir data] [--split dev]
Requires: python3 data/generate.py --seed 42 (to produce the dataset first)
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.evals.gate_report import format_gate_report, run_gate_report  # noqa: E402

p = argparse.ArgumentParser()
p.add_argument("--data-dir", default=str(Path(__file__).resolve().parents[1] / "data"))
p.add_argument("--split", default="dev", choices=["dev", "held_out"])
args = p.parse_args()
print(format_gate_report(run_gate_report(args.data_dir, args.split)))
