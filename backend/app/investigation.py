"""Investigation loop runner (R2) — coordination between mind and hands.

The planner (app.ai.investigator) decides; the R1 registry executes; this
module runs the loop between them and owns everything neither may:
- executing tool requests (the AI lane cannot import app.tools);
- materializing external-system observations as documents (tools are
  read-only; recording a courier confirmation is a coordination-layer write,
  exactly like the orchestrator persisting evidence);
- termination limits: the registry's call budget, an iteration cap, and
  no-progress detection (a repeated identical request twice ends the loop);
- audit: AGENT_PLAN / AGENT_OBSERVATION per step, AGENT_COMPLETE or
  AGENT_NEEDS_INPUT at the end — all in the case's hash chain, all
  operational fields only (no chain-of-thought exists to leak).

Termination reasons: SUFFICIENT_EVIDENCE, NEEDS_INPUT, BUDGET_EXHAUSTED,
ITERATIONS_EXHAUSTED, NO_PROGRESS. The runner NEVER decides the case: it
returns gathered documents; extraction, the gate, and the decision engine
proceed unchanged downstream.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .ai.errors import LowConfidence
from .ai.investigator import InvestigationContext, PlannerDecision, plan_next
from .store.models import Case, Document, DocumentType, Dispute, Provenance
from .store.repo import Repository, utc_now_iso
from .tools.investigation import (
    DEFAULT_TOOL_BUDGET,
    ToolBudgetExceeded,
    ToolRegistry,
)

MAX_ITERATIONS = 10
_OBS_LIMIT = 200


@dataclass
class InvestigationOutcome:
    termination: str                 # see module docstring
    documents: list[Document]
    request_to_user: str | None = None
    missing: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    kb_citations: list[dict] = field(default_factory=list)   # VERIFIED only


def run_investigation(repo: Repository, case: Case, dispute: Dispute, order,
                      playbook, client, *,
                      budget: int = DEFAULT_TOOL_BUDGET,
                      max_iterations: int = MAX_ITERATIONS
                      ) -> InvestigationOutcome:
    registry = ToolRegistry(repo, case.id, budget=budget)
    ctx = InvestigationContext(
        dispute={"id": dispute.id, "amount": dispute.amount,
                 "reason_code": dispute.reason_code.value,
                 "respond_by": dispute.respond_by,
                 "payment_id": dispute.payment_id},
        order={"id": order.id, "customer_email": order.customer_email,
               "address": order.address},
        checklist=[{"key": r.key, "description": r.description,
                    "required": True} for r in playbook.rules.values()],
        tool_specs=ToolRegistry.specs_for_model())
    repo.append_audit(case.id, "AGENT_PLAN", {
        "objective": f"establish the evidence checklist for "
                     f"{dispute.reason_code.value}",
        "checklist": [c["key"] for c in ctx.checklist],
        "tools_available": sorted(t["name"] for t in ctx.tool_specs),
        "limits": {"tool_budget": budget, "max_iterations": max_iterations},
        "planner": getattr(client, "provider", "stub")})

    history: list[dict] = []
    docs: dict[str, Document] = {}
    kb_citations: list[dict] = []
    for doc in repo.list_documents_for_case(case.id):
        if doc.provenance == Provenance.USER_UPLOAD.value:
            docs[doc.id] = doc
            history.append({"tool": "read_document",
                            "args": {"doc_id": doc.id}, "ok": True,
                            "data": {"id": doc.id, "type": doc.type.value,
                                     "provenance": doc.provenance},
                            "summary": f"merchant-uploaded {doc.type.value} "
                                       f"({doc.source})"})
    invalid_requests = 0
    last_request: tuple | None = None
    repeats = 0

    for iteration in range(1, max_iterations + 1):
        try:
            step = plan_next(ctx, history, client)
        except LowConfidence as e:
            return _finish(repo, case, "NO_PROGRESS", docs, history,
                           registry, invalid_requests, kb_citations,
                           note=f"planner low confidence: {e.reason}")
        d: PlannerDecision = step.decision

        if d.action == "complete":
            return _finish(repo, case, "SUFFICIENT_EVIDENCE", docs, history,
                           registry, invalid_requests, kb_citations, note=d.goal)
        if d.action == "needs_input":
            repo.append_audit(case.id, "AGENT_NEEDS_INPUT", {
                "goal": d.goal, "missing": d.missing,
                "request_to_user": d.request_to_user,
                "iteration": iteration})
            return InvestigationOutcome(
                termination="NEEDS_INPUT", documents=list(docs.values()),
                request_to_user=d.request_to_user, missing=d.missing,
                stats=_stats(registry, history, invalid_requests),
                kb_citations=kb_citations)

        request = (d.tool, json.dumps(d.args, sort_keys=True))
        repeats = repeats + 1 if request == last_request else 0
        last_request = request
        if repeats >= 2:
            return _finish(repo, case, "NO_PROGRESS", docs, history,
                           registry, invalid_requests, kb_citations,
                           note=f"repeated identical request {d.tool}")

        try:
            result = registry.execute(d.tool, d.args)
        except ToolBudgetExceeded:
            return _finish(repo, case, "BUDGET_EXHAUSTED", docs, history,
                           registry, invalid_requests, kb_citations)
        if not result.ok:
            invalid_requests += 1

        _collect_documents(repo, case, order, result, docs)
        if d.tool == "search_knowledge" and result.ok:
            kb_citations.extend(_verified_citations(result.data))
        summary = (json.dumps(result.data, default=str)[:_OBS_LIMIT]
                   if result.ok else result.error)
        history.append({"tool": d.tool, "args": d.args, "ok": result.ok,
                        "data": result.data if result.ok else None,
                        "summary": summary})
        repo.append_audit(case.id, "AGENT_OBSERVATION", {
            "iteration": iteration, "goal": d.goal, "tool": d.tool,
            "ok": result.ok, "observation": summary,
            "provenance": result.provenance,
            "documents_gathered": sorted(docs)})

    return _finish(repo, case, "ITERATIONS_EXHAUSTED", docs, history,
                   registry, invalid_requests)


def _collect_documents(repo: Repository, case: Case, order, result,
                       docs: dict) -> None:
    """Turn observations into the document set downstream stages consume."""
    if not result.ok:
        return
    if result.tool == "read_document":
        doc = repo.get_document(result.data["id"])
        if doc is not None:
            docs[doc.id] = doc
    elif result.tool == "fetch_tracking" and \
            result.data.get("status") == "delivered":
        # The courier's own delivery record, materialized as a document so
        # the UNCHANGED gate can verify quotes and fields against it. This
        # write belongs to the coordination layer, never to a tool. Format
        # mirrors merchant PODs so the unchanged extractor understands it.
        r = result.data
        doc_id = f"doc_track_{r['awb']}"
        existing = repo.get_document(doc_id)
        if existing is None:
            text = (f"PROOF OF DELIVERY\nCourier: {r['courier']}\n"
                    f"AWB: {r['awb']}\nDelivered: {r['delivered_at']}\n"
                    f"Receiver: {r['receiver']}\n"
                    f"Delivery OTP verified: NO\n"
                    f"Address: {r['address']}\n"
                    f"Source: courier tracking system (simulated), retrieved "
                    f"during investigation\n")
            existing = Document(
                id=doc_id, case_id=case.id, type=DocumentType.POD,
                raw_text=text, source=f"courier:{r['awb']}",
                fetched_at=utc_now_iso(),
                provenance=Provenance.SIMULATOR.value)
            repo.add_document(existing)
        docs[doc_id] = existing


def _stats(registry: ToolRegistry, history: list[dict],
           invalid_requests: int) -> dict:
    return {"tool_calls": registry.calls_used,
            "budget": registry.budget,
            "invalid_tool_requests": invalid_requests,
            "tools_used": sorted({h["tool"] for h in history})}


def _verified_citations(data: dict) -> list[dict]:
    """Construct citations from retrieval results and keep ONLY those that
    pass deterministic verbatim verification. The quote is code-extracted
    (first sentence of the chunk), then verified anyway — belt and braces;
    an LLM paraphrase can never enter this list."""
    from .kb import get_kb
    from .policy.kb_citations import verify_kb_citation
    out = []
    for r in (data.get("results") or [])[:2]:
        quote = r["text"].split(". ")[0].strip()
        if not quote.endswith("."):
            quote += "."
        verdict = verify_kb_citation(
            {"source_id": r["source_id"], "chunk_id": r["chunk_id"],
             "quote": quote}, get_kb())
        if verdict.valid:
            out.append({"source_id": r["source_id"],
                        "chunk_id": r["chunk_id"], "quote": quote,
                        "document_version": r["document_version"]})
    return out


def _finish(repo: Repository, case: Case, termination: str, docs: dict,
            history: list[dict], registry: ToolRegistry,
            invalid_requests: int, kb_citations: list[dict] | None = None,
            note: str = "") -> InvestigationOutcome:
    stats = _stats(registry, history, invalid_requests)
    repo.append_audit(case.id, "AGENT_COMPLETE", {
        "termination": termination, "note": note,
        "documents_gathered": sorted(docs),
        "kb_citations_verified": len(kb_citations or []), **stats})
    return InvestigationOutcome(termination=termination,
                                documents=list(docs.values()), stats=stats,
                                kb_citations=kb_citations or [])
