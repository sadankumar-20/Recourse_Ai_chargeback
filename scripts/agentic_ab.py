#!/usr/bin/env python3
"""R2 A/B: fixed pipeline vs agentic investigation on the DEV split.

The held-out 40 stay frozen for eval v2 (R7); this comparison uses only
development disputes. Writes evals/agentic_ab.json (deterministic).

Honesty note baked into the artifact: v1 ground-truth labels for missing_pod
(action ESCALATE, outcomes mostly 'lost') encode the FIXED pipeline's
capability assumption — they were authored for a world where the POD is
unreachable. So this A/B measures investigation capability (escalations,
evidence completeness, tool efficiency, safety invariants), and does NOT
claim recovered rupees on those cases against outcome labels written for a
POD-less world.
"""
import json
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.ai.client import StubAIClient                      # noqa: E402
from app.audit.chain import verify_audit_chain              # noqa: E402
from app.orchestrator import Orchestrator                   # noqa: E402
from app.policy.playbooks import load_playbooks             # noqa: E402
from app.store.models import CaseState                      # noqa: E402
from app.store.repo import Repository                       # noqa: E402
from app.tools.payments_adapter import SimulatorAdapter     # noqa: E402

DATA = ROOT / "data"
split = json.loads((DATA / "split.json").read_text())
gt = json.loads((DATA / "ground_truth.json").read_text())["labels"]
pb = load_playbooks()
sim_now = datetime.fromisoformat(split["sim_now"])
dev = [d for d in split["dev"]]


def run(mode: str) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "w.db"
        shutil.copy(DATA / "dataset.db", db)
        repo = Repository(db)
        orch = Orchestrator(repo, SimulatorAdapter(repo),
                            ai_client=StubAIClient(), playbooks=pb,
                            now=sim_now, sleep=lambda s: None,
                            investigation_mode=mode)
        stats = {"mode": mode, "cases": 0, "closed": 0, "escalated": 0,
                 "needs_input_asks": 0, "recovered_missing_pod_cases": 0,
                 "tool_calls_total": 0, "tool_calls_max": 0,
                 "invalid_tool_requests": 0, "budget_violations": 0,
                 "deadline_violations": 0, "chains_invalid": 0,
                 "evidence_admitted_total": 0}
        for did in dev:
            r = orch.process_event({"event": "dispute.created",
                                    "dispute_id": did})
            stats["cases"] += 1
            if r.final_state is CaseState.CLOSED:
                stats["closed"] += 1
            elif r.final_state is CaseState.ESCALATED:
                stats["escalated"] += 1
            evidence = repo.list_evidence_for_case(r.case.id)
            stats["evidence_admitted_total"] += sum(
                1 for e in evidence if e.gate_verdict
                and e.gate_verdict.value == "PASS")
            if (gt[did]["scenario"] == "missing_pod"
                    and r.final_state is CaseState.CLOSED):
                stats["recovered_missing_pod_cases"] += 1
            hours = (datetime.fromisoformat(
                repo.get_dispute(did).respond_by) - sim_now
            ).total_seconds() / 3600
            if repo.get_action_by_idempotency_key(did) and hours <= 0:
                stats["deadline_violations"] += 1
            if not verify_audit_chain(repo, r.case.id).valid:
                stats["chains_invalid"] += 1
            for e in repo.read_audit(r.case.id):
                if e.step == "AGENT_COMPLETE":
                    p = json.loads(e.payload_json)
                    stats["tool_calls_total"] += p["tool_calls"]
                    stats["tool_calls_max"] = max(stats["tool_calls_max"],
                                                  p["tool_calls"])
                    stats["invalid_tool_requests"] += p["invalid_tool_requests"]
                    if p["termination"] == "BUDGET_EXHAUSTED":
                        stats["budget_violations"] += 1
                if e.step == "AGENT_NEEDS_INPUT":
                    stats["needs_input_asks"] += 1
        stats["tool_calls_avg"] = round(
            stats["tool_calls_total"] / stats["cases"], 2)
        repo.close()
        return stats


fixed, agentic = run("fixed"), run("agentic")
result = {
    "note": "dev split only; held-out frozen for eval v2. Missing_pod v1 "
            "outcome labels encode the fixed pipeline's capability "
            "assumption, so recovered cases are counted as investigation "
            "capability, not as realized rupees.",
    "fixed": fixed, "agentic": agentic,
    "delta": {"escalations": agentic["escalated"] - fixed["escalated"],
              "missing_pod_recovered":
                  agentic["recovered_missing_pod_cases"]
                  - fixed["recovered_missing_pod_cases"],
              "evidence_admitted":
                  agentic["evidence_admitted_total"]
                  - fixed["evidence_admitted_total"]}}
(ROOT / "evals" / "agentic_ab.json").write_text(json.dumps(result, indent=1))
w = max(len(k) for k in fixed)
print(f"{'metric':<{w}}  fixed  agentic")
for k in fixed:
    if k != "mode":
        print(f"{k:<{w}}  {fixed[k]!s:>5}  {agentic[k]!s:>7}")
print("-> evals/agentic_ab.json")
