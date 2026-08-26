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
# R6: one live interactive case for the cockpit demo — courier blinded so
# the agent asks the merchant for the POD (needs_input, upload, resume).
from app.intake import submit_intake
for did2 in split["dev"]:
    if gt[did2]["scenario"] != datagen.MISSING_POD:
        continue
    d2 = repo2 = None
    from app.store.repo import Repository as _R
    repo2 = _R(DB)
    d2 = repo2.get_dispute(did2)
    if d2.reason_code.value not in pb.reason_codes \
            or repo2.get_case_by_dispute(did2):
        repo2.close(); continue
    order2 = repo2.get_order_by_payment(d2.payment_id)
    with repo2.conn:
        repo2.conn.execute("UPDATE shipments SET status='in_transit' "
                           "WHERE order_id = ?", (order2.id,))
    res2 = submit_intake(
        repo2, f"The customer says they never received order "
               f"#{order2.id.removeprefix('ord_')}, but we dispatched it.",
        get_client(), now=datetime.fromisoformat(split["sim_now"]))
    from app.orchestrator import Orchestrator as _O
    from app.tools.payments_adapter import SimulatorAdapter as _S
    r6 = _O(repo2, _S(repo2), ai_client=get_client(), playbooks=pb,
            now=datetime.fromisoformat(split["sim_now"]),
            sleep=lambda s: None,
            investigation_mode="agentic").run_case(res2.case.id)
    print(f"  {res2.dispute.id}  interactive_needs_input     -> "
          f"{r6.final_state.value}")
    repo2.close()
    break

print("\nNow run: python3 scripts/serve.py")
