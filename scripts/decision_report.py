#!/usr/bin/env python3
"""Print the decision-engine report over the generated dataset (dev split).

Usage: python3 scripts/decision_report.py [--data-dir data] [--split dev]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.evals.decision_report import format_decision_report, run_decision_report  # noqa: E402

p = argparse.ArgumentParser()
p.add_argument("--data-dir", default=str(Path(__file__).resolve().parents[1] / "data"))
p.add_argument("--split", default="dev", choices=["dev", "held_out"])
args = p.parse_args()
print(format_decision_report(run_decision_report(args.data_dir, args.split)))
