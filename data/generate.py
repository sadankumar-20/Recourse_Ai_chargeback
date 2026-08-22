#!/usr/bin/env python3
"""CLI wrapper for the Recourse synthetic dataset generator.

Usage:
    python3 data/generate.py --seed 42

All logic lives in backend/app/datagen.py (importable + unit-tested); this
wrapper only parses arguments and prints the derived summary.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.datagen import format_summary, generate  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description="Generate the Recourse synthetic dataset")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--out-dir", default=str(Path(__file__).resolve().parent))
    p.add_argument("--orders", type=int, default=800)
    p.add_argument("--disputes", type=int, default=120)
    args = p.parse_args()
    stats = generate(seed=args.seed, out_dir=args.out_dir,
                     n_orders=args.orders, n_disputes=args.disputes)
    print(f"Generated dataset (seed={args.seed}) -> {args.out_dir}\n")
    print(format_summary(stats))


if __name__ == "__main__":
    main()
