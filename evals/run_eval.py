#!/usr/bin/env python3
"""Run the frozen 40-dispute held-out evaluation through the REAL orchestrator.

Usage:
    python3 data/generate.py --seed 42      # once, to build the world
    python3 evals/run_eval.py [--ablate-gate] [--data-dir data] [--out-dir evals]

The 40 held-out ids are frozen in data/split.json (Stage 3) and were never
used for tuning. The harness fails loudly on any split alteration. Ground
truth is consulted only AFTER each case reaches a terminal state.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.evals.harness import run_eval  # noqa: E402

p = argparse.ArgumentParser()
p.add_argument("--data-dir", default=str(ROOT / "data"))
p.add_argument("--out-dir", default=str(ROOT / "evals"))
p.add_argument("--ablate-gate", action="store_true",
               help="ALSO compute the gate-off ablation analysis (the "
                    "production pipeline keeps the gate on either way)")
args = p.parse_args()

result = run_eval(args.data_dir, args.out_dir, ablate_gate=args.ablate_gate)
m = result["metrics"]
print(f"Held-out evaluation complete -> {args.out_dir}/metrics.json, report.md")
print(f"  decision accuracy : {m['decision']['accuracy']*100:.1f}%")
print(f"  automation rate   : {m['automation']['automation_rate']*100:.1f}%")
print(f"  deadline breaches : {m['deadline_compliance']['violations']}")
print(f"  net recovered     : Rs.{m['money']['recourse']['net']:,} "
      f"(contest-all net Rs.{m['money']['baseline_contest_all']['net']:,})")
