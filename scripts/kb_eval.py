#!/usr/bin/env python3
"""R3 measurement: knowledge retrieval + citation validity over the DEV
split (agentic mode). Held-out stays frozen for eval v2 (R7).
Writes evals/kb_metrics.json (deterministic).

Relevance proxy stated honestly: for the MVP reason codes the 'relevant'
chunk is the matching dispute_policy evidence section (delivery_evidence for
goods_not_received, description_evidence for not_as_described,
duplicate_evidence for duplicate). Citation validity is 100% BY CONSTRUCTION
for artifact-entering citations (they are code-extracted then verified) —
reported as such, not as a model achievement; the verifier's rejection power
is proven by the adversarial tests instead.
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
from app.kb import get_kb                                   # noqa: E402
from app.orchestrator import Orchestrator                   # noqa: E402
from app.policy.playbooks import load_playbooks             # noqa: E402
from app.store.models import CaseState                      # noqa: E402
from app.store.repo import Repository                       # noqa: E402
from app.tools.payments_adapter import SimulatorAdapter     # noqa: E402

DATA = ROOT / "data"
split = json.loads((DATA / "split.json").read_text())
pb = load_playbooks()
sim_now = datetime.fromisoformat(split["sim_now"])
RELEVANT = {"goods_not_received": "delivery_evidence",
            "not_as_described": "description_evidence",
            "duplicate": "duplicate_evidence"}

with tempfile.TemporaryDirectory() as tmp:
    db = Path(tmp) / "w.db"
    shutil.copy(DATA / "dataset.db", db)
    repo = Repository(db)
    orch = Orchestrator(repo, SimulatorAdapter(repo), ai_client=StubAIClient(),
                        playbooks=pb, now=sim_now, sleep=lambda s: None,
                        investigation_mode="agentic")
    m = {"kb_checksum": get_kb().checksum[:16], "cases": 0,
         "cases_reaching_investigation": 0, "cases_using_knowledge": 0,
         "knowledge_queries": 0, "retrieval_hits": 0, "relevant_top_hits": 0,
         "verified_citations_attached": 0, "invalid_citations_attached": 0,
         "completed_investigations": 0, "chains_invalid": 0,
         "deadline_violations": 0}
    for did in split["dev"]:
        r = orch.process_event({"event": "dispute.created", "dispute_id": did})
        m["cases"] += 1
        reason = repo.get_dispute(did).reason_code.value
        saw_agent = False
        for e in repo.read_audit(r.case.id):
            p = json.loads(e.payload_json)
            if e.step == "AGENT_PLAN":
                saw_agent = True
            if e.step == "TOOL_CALL" and p["tool"] == "search_knowledge":
                m["knowledge_queries"] += 1
                if p["ok"]:
                    m["retrieval_hits"] += 1
            if e.step == "AGENT_OBSERVATION" and p["tool"] == "search_knowledge" \
                    and p["ok"] and RELEVANT.get(reason, "~") in p["observation"]:
                m["relevant_top_hits"] += 1
            if e.step == "AGENT_COMPLETE":
                m["completed_investigations"] += 1
                m["verified_citations_attached"] += p["kb_citations_verified"]
            if e.step == "DRAFT_CREATED" and "kb_citations" in p:
                pass   # counted via AGENT_COMPLETE; drafts re-validated in tests
        if saw_agent:
            m["cases_reaching_investigation"] += 1
            m["cases_using_knowledge"] += int(any(
                json.loads(e.payload_json).get("tool") == "search_knowledge"
                for e in repo.read_audit(r.case.id) if e.step == "TOOL_CALL"))
        if not verify_audit_chain(repo, r.case.id).valid:
            m["chains_invalid"] += 1
        hours = (datetime.fromisoformat(repo.get_dispute(did).respond_by)
                 - sim_now).total_seconds() / 3600
        if repo.get_action_by_idempotency_key(did) and hours <= 0:
            m["deadline_violations"] += 1
    repo.close()

m["retrieval_hit_rate"] = round(m["retrieval_hits"]
                                / max(1, m["knowledge_queries"]), 3)
m["relevant_top_hit_rate"] = round(m["relevant_top_hits"]
                                   / max(1, m["retrieval_hits"]), 3)
m["knowledge_queries_per_investigated_case"] = round(
    m["knowledge_queries"] / max(1, m["cases_reaching_investigation"]), 2)
m["citation_validity_rate_by_construction"] = 1.0 if \
    m["invalid_citations_attached"] == 0 else round(
        m["verified_citations_attached"]
        / (m["verified_citations_attached"]
           + m["invalid_citations_attached"]), 3)
(ROOT / "evals" / "kb_metrics.json").write_text(json.dumps(m, indent=1))
for k, v in m.items():
    print(f"{k:<42} {v}")
print("-> evals/kb_metrics.json")
