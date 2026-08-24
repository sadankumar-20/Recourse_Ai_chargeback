#!/usr/bin/env python3
"""Seed demo.db with curated cases for the dashboard/demo (spec §30.10).

Usage: python3 data/generate.py --seed 42 && python3 scripts/demo_seed.py
Creates demo.db (a copy of the world) and runs real orchestrator cases:
a clean winnable fight, a Hinglish-admission fight, a pincode-mismatch
escalation, a hopeless auto-accept, an ambiguous-link escalation, a delayed
kill-switch escalation, and one duplicate webhook delivery.
"""
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app import datagen  # noqa: E402
from app.ai.client import get_client  # noqa: E402
from app.orchestrator import Orchestrator, format_timeline  # noqa: E402
from app.policy.playbooks import load_playbooks  # noqa: E402
from app.store.repo import Repository  # noqa: E402
from app.tools.payments_adapter import SimulatorAdapter  # noqa: E402

DATA = ROOT / "data"
DB = ROOT / "demo.db"

split = json.loads((DATA / "split.json").read_text())
gt = json.loads((DATA / "ground_truth.json").read_text())["labels"]
shutil.copy(DATA / "dataset.db", DB)
repo = Repository(DB)
pb = load_playbooks()
orch = Orchestrator(repo, SimulatorAdapter(
    repo, outcomes={d: g["gt_outcome_if_fought"] for d, g in gt.items()}),
    ai_client=get_client(), playbooks=pb,
    now=datetime.fromisoformat(split["sim_now"]), sleep=lambda s: None)

wanted = [datagen.CLEAN, datagen.HINGLISH, datagen.CONFLICT_PIN,
          datagen.HOPELESS, datagen.AMBIGUOUS, datagen.DELAYED]
seeded = []
for scenario in wanted:
    for did in split["dev"]:
        if gt[did]["scenario"] != scenario:
            continue
        reason = repo.get_dispute(did).reason_code.value
        if scenario != datagen.AMBIGUOUS and reason not in pb.reason_codes:
            continue
        res = orch.process_event({"event": "dispute.created", "dispute_id": did,
                                  "arrival": split["sim_now"]})
        seeded.append((scenario, did, res.final_state.value))
        if scenario == datagen.CLEAN:      # demonstrate duplicate delivery
            orch.process_event({"event": "dispute.created", "dispute_id": did})
        break

print(f"Seeded {DB} with {len(seeded)} cases:")
for scenario, did, state in seeded:
    print(f"  {did}  {scenario:<24} -> {state}")
print("\nSample timeline:")
print(format_timeline(repo, f"case_{seeded[1][1]}"))
repo.close()
print("\nNow run: python3 scripts/serve.py")
