"""The Recourse orchestrator (spec §8, §30.6).

Coordination ONLY. This module owns: the state-machine walk, deadline
guards, the retry loop, duplicate-webhook idempotency, and escalation
summaries. It deliberately owns nothing else:
- interpretation (linking, extraction, drafting) lives in app.ai and is
  called through its untrusted-output boundary;
- judgment (admission, decision math, citation validity) lives in app.policy;
- money lives in app.tools.executor, behind persisted idempotency.

Escalation is an internal control-flow signal (_Escalate) so every failure
mode funnels through ONE exit that writes the merchant-facing summary and
the CASE_ESCALATED audit entry. The orchestrator never resumes a terminal
case: ESCALATED -> ACTED is reserved for an explicit human-actor approval
path, never autonomous code (ADR-010).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime

from . import config
from .ai.client import get_client
from .ai.draft_representment import draft_representment
from .ai.errors import LowConfidence
from .ai.extract_evidence import extract_evidence
from .ai.link_order import link_order
from .policy.decide import DecisionOutcome, decide
from .policy.gate import GateContext, admit_all, case_preconditions
from .policy.playbooks import PlaybookError, PlaybookSet, load_playbooks
from .store.models import Actor, Case, CaseState, Dispute, GateVerdict
from .store.repo import Repository
from .tools.executor import execute_action
from .tools.payments_adapter import PaymentsAdapter, TransientPaymentsError


class _Escalate(Exception):
    """Internal signal: unify every escalation path through one exit."""

    def __init__(self, reason: str, details: list[str] | None = None,
                 extra: dict | None = None):
        super().__init__(reason)
        self.reason = reason
        self.details = details or []
        self.extra = extra or {}


@dataclass
class CaseResult:
    case: Case
    final_state: CaseState
    escalation_summary: str | None = None


class Orchestrator:
    def __init__(self, repo: Repository, adapter: PaymentsAdapter,
                 ai_client=None, playbooks: PlaybookSet | None = None,
                 now: datetime | None = None, sleep=time.sleep,
                 backoff_base_s: float = 1.0):
        self.repo = repo
        self.adapter = adapter
        self.ai = ai_client or get_client()
        self.playbooks = playbooks or load_playbooks()
        self._now = now                      # fixed clock for tests/eval
        self.sleep = sleep
        self.backoff_base_s = backoff_base_s

    # -- clock / deadline helpers ---------------------------------------------------

    def now(self) -> datetime:
        return self._now or datetime.now().astimezone()

    def hours_left(self, dispute: Dispute) -> float:
        respond_by = datetime.fromisoformat(dispute.respond_by)
        return (respond_by - self.now()).total_seconds() / 3600.0

    def _deadline_guard(self, dispute: Dispute, *, acting: bool) -> None:
        """Deterministic deadline rules (spec §18): after respond_by all
        money actions are hard-blocked; before DECIDE, T-24h force-escalates.
        An already-approved action may still execute inside the last 24h —
        but never after the deadline."""
        hours = self.hours_left(dispute)
        if hours <= 0:
            raise _Escalate(
                f"deadline passed {abs(hours):.1f}h ago ({dispute.respond_by}) "
                f"— all money actions are prohibited")
        if not acting and hours < config.DEADLINE_ESCALATE_HOURS:
            raise _Escalate(
                f"only {hours:.1f}h left (< {config.DEADLINE_ESCALATE_HOURS}h "
                f"kill-switch) — a human must handle last-minute cases")

    # -- intake -----------------------------------------------------------------------

    def handle_event(self, event: dict) -> Case:
        """Webhook intake (spec §8 steps 1-2). Duplicate deliveries are
        idempotent: same case, no restarted workflow, audited."""
        if event.get("event") != "dispute.created" or "dispute_id" not in event:
            raise ValueError(f"invalid webhook event: {event!r}")
        dispute_id = event["dispute_id"]
        dispute = self.repo.get_dispute(dispute_id)
        if dispute is None:
            raise ValueError(f"webhook references unknown dispute {dispute_id!r}")

        existing = self.repo.get_case_by_dispute(dispute_id)
        if existing is not None:
            self.repo.append_audit(existing.id, "WEBHOOK_DUPLICATE", {
                "dispute_id": dispute_id,
                "arrival": event.get("arrival"),
                "case_state": existing.state.value,
                "note": "duplicate delivery — workflow not restarted, no new "
                        "case, no new money action possible (idempotency key "
                        "is the dispute id)"})
            return existing

        case = Case(id=f"case_{dispute_id}", dispute_id=dispute_id)
        self.repo.add_case(case)
        self.repo.append_audit(case.id, "CASE_CREATED", {
            "dispute_id": dispute_id, "payment_id": dispute.payment_id,
            "amount": dispute.amount, "reason_code": dispute.reason_code.value,
            "respond_by": dispute.respond_by,
            "hours_left": round(self.hours_left(dispute), 1),
            "arrival": event.get("arrival")})
        return case

    def process_event(self, event: dict) -> CaseResult:
        return self.run_case(self.handle_event(event).id)

    # -- the state machine walk ---------------------------------------------------------

    def run_case(self, case_id: str) -> CaseResult:
        case = self.repo.get_case(case_id)
        if case is None:
            raise KeyError(f"no case {case_id!r}")
        if case.state in (CaseState.CLOSED, CaseState.ESCALATED,
                          CaseState.ACTED):
            # Terminal for autonomy: escalated cases belong to humans.
            self.repo.append_audit(case.id, "RUN_REFUSED", {
                "state": case.state.value,
                "note": "orchestrator never resumes a terminal case"})
            return CaseResult(case=case, final_state=case.state)
        dispute = self.repo.get_dispute(case.dispute_id)
        try:
            return self._process(case, dispute)
        except _Escalate as e:
            return self._escalate(case, dispute, e)
        except LowConfidence as e:
            return self._escalate(case, dispute, _Escalate(
                f"AI low confidence in {e.task}: {e.reason}",
                extra={"ai_records": [r.to_dict() for r in e.records]}))

    def _process(self, case: Case, dispute: Dispute) -> CaseResult:
        # reason-code coverage is a config-level fact — check before any work
        try:
            playbook = self.playbooks.for_reason(dispute.reason_code)
        except PlaybookError as e:
            raise _Escalate(f"unsupported reason code: {e}")

        # LINK ------------------------------------------------------------------
        self._deadline_guard(dispute, acting=False)
        case = self.repo.update_case_state(case.id, CaseState.LINKING)
        order = self._link(case, dispute)

        # GATHER ----------------------------------------------------------------
        self._deadline_guard(dispute, acting=False)
        case = self.repo.update_case_state(case.id, CaseState.GATHERING)
        docs = self._gather(case, order)

        # EXTRACT + GATE --------------------------------------------------------
        self._deadline_guard(dispute, acting=False)
        candidates = self._extract(case, dispute, docs, playbook)
        ctx = GateContext(dispute=dispute, order=order,
                          shipments=self.repo.list_shipments_for_order(order.id),
                          refunds=self.repo.list_refunds_for_order(order.id),
                          documents={d.id: d for d in docs},
                          playbooks=self.playbooks, now=self.now())
        verdicts, admitted = self._gate(case, candidates, ctx)
        case = self.repo.update_case_state(case.id, CaseState.GATED)

        # DECIDE ----------------------------------------------------------------
        outcome = decide(
            dispute=dispute, playbook=playbook,
            playbook_version=self.playbooks.version, verdicts=verdicts,
            now=self.now(), has_shipment=bool(ctx.shipments),
            preconditions_ok=all(c.passed for c in case_preconditions(ctx)))
        self.repo.add_decision(outcome.to_decision(f"dec_{case.id}", case.id))
        self.repo.append_audit(case.id, "DECISION_MADE", outcome.to_dict())
        case = self.repo.update_case_state(case.id, CaseState.DECIDED)

        if outcome.action.value == "ESCALATE":
            raise _Escalate("policy decision: " + "; ".join(outcome.reasons),
                            details=[f"missing required '{k}': {why}"
                                     for k, why in outcome.missing_required],
                            extra={"rule_fired": outcome.rule_fired})

        # DRAFT (contest only) ---------------------------------------------------
        bundle: dict = {"decision": outcome.to_dict()}
        if outcome.action.value == "FIGHT":
            draft = draft_representment(admitted, dispute, order, self.ai)
            self.repo.append_audit(case.id, "DRAFT_CREATED", {
                "display_map": draft.display_map,
                "chars": len(draft.text),
                "ai_calls": [r.to_dict() for r in draft.records]})
            self.repo.append_audit(case.id, "DRAFT_VALIDATED", {
                "admitted_ids": sorted(draft.display_map.values()),
                "validator": "policy.citations", "violations": 0})
            bundle["representment"] = draft.text
            bundle["evidence"] = sorted(draft.display_map.values())

        # ACT ---------------------------------------------------------------------
        self._deadline_guard(dispute, acting=True)   # hard block after deadline
        action_type = "contest" if outcome.action.value == "FIGHT" else "accept"
        self._act_with_retries(case, dispute, action_type, bundle, outcome)
        case = self.repo.update_case_state(case.id, CaseState.ACTED)

        # CLOSED --------------------------------------------------------------------
        case = self.repo.update_case_state(case.id, CaseState.CLOSED)
        self.repo.append_audit(case.id, "CASE_CLOSED", {
            "action": action_type,
            "dispute_status": self.repo.get_dispute(dispute.id).status.value})
        return CaseResult(case=case, final_state=CaseState.CLOSED)

    # -- steps ------------------------------------------------------------------------

    def _link(self, case: Case, dispute: Dispute):
        """Deterministic exact match first; AI only for genuine ambiguity;
        below the confidence floor the orchestrator never guesses."""
        order = self.repo.get_order_by_payment(dispute.payment_id)
        if order is not None:
            self.repo.set_case_link(case.id, order.id, 1.0)
            self.repo.append_audit(case.id, "LINK_COMPLETED", {
                "method": "exact_payment_id", "order_id": order.id,
                "confidence": 1.0})
            return order

        candidates = self._candidate_orders(dispute)
        if not candidates:
            raise _Escalate(
                f"order unresolvable: payment_id {dispute.payment_id} matches "
                f"nothing and no candidate orders share the disputed amount")
        result = link_order(dispute, candidates, self.ai)
        p = result.proposal
        if p.confidence < config.LINK_CONFIDENCE_FLOOR:
            raise _Escalate(
                f"ambiguous order link: AI confidence {p.confidence:.2f} < "
                f"{config.LINK_CONFIDENCE_FLOOR} floor — the system never "
                f"guesses which order was disputed",
                details=[f"candidate {o.id}: \u20b9{o.amount}, "
                         f"{o.customer_email}, created {o.created_at}"
                         for o in candidates] + [f"AI reasoning: {p.reasoning}"],
                extra={"confidence": p.confidence,
                       "ai_records": [r.to_dict() for r in result.records]})
        order = next(o for o in candidates if o.id == p.order_id)
        self.repo.set_case_link(case.id, order.id, p.confidence)
        self.repo.append_audit(case.id, "LINK_COMPLETED", {
            "method": "ai_ranked", "order_id": order.id,
            "confidence": p.confidence, "reasoning": p.reasoning,
            "ai_calls": [r.to_dict() for r in result.records]})
        return order

    def _candidate_orders(self, dispute: Dispute) -> list:
        rows = self.repo.conn.execute(
            "SELECT id FROM orders WHERE amount = ? ORDER BY created_at LIMIT 8",
            (dispute.amount,)).fetchall()
        return [self.repo.get_order(r["id"]) for r in rows]

    def _gather(self, case: Case, order) -> list:
        self.repo.append_audit(case.id, "GATHER_STARTED", {"order_id": order.id})
        docs = []
        for ship in self.repo.list_shipments_for_order(order.id):
            if ship.pod_doc_id:
                docs.append(self.repo.get_document(ship.pod_doc_id))
        for row in self.repo.conn.execute(
                "SELECT id FROM documents WHERE source = ? AND type = 'email'",
                (f"mailbox:{order.customer_email}",)).fetchall():
            docs.append(self.repo.get_document(row["id"]))
        self.repo.append_audit(case.id, "GATHER_COMPLETED", {
            "documents": [{"id": d.id, "type": d.type.value, "source": d.source}
                          for d in docs]})
        return docs

    def _extract(self, case: Case, dispute: Dispute, docs, playbook) -> list:
        result = extract_evidence(case.id, dispute, docs, playbook, self.ai)
        for ev in result.candidates:
            ev.id = f"{case.id}-{ev.id}"         # global ids for persistence
        self.repo.append_audit(case.id, "EVIDENCE_EXTRACTED", {
            "count": len(result.candidates),
            "keys": sorted({e.evidence_key for e in result.candidates}),
            "ai_calls": [r.to_dict() for r in result.records]})
        return result.candidates

    def _gate(self, case: Case, candidates, ctx):
        """Gate every candidate; persist ALL of them with verdicts — failed
        evidence is preserved and shown, never discarded."""
        verdicts = admit_all(candidates, ctx)
        admitted, rejected = [], []
        for ev, v in zip(candidates, verdicts):
            self.repo.add_evidence(ev)
            if v.status is GateVerdict.PASS:
                self.repo.set_evidence_verdict(ev.id, GateVerdict.PASS)
                ev.gate_verdict = GateVerdict.PASS
                admitted.append(ev)
            else:
                self.repo.set_evidence_verdict(ev.id, GateVerdict.FAIL,
                                               v.failure_reason)
                ev.gate_verdict = GateVerdict.FAIL
                ev.fail_reason = v.failure_reason
                rejected.append((ev, v))
        if admitted:
            self.repo.append_audit(case.id, "EVIDENCE_ADMITTED", {
                "ids": [e.id for e in admitted],
                "keys": sorted({e.evidence_key for e in admitted}),
                "playbook_version": ctx.playbooks.version})
        if rejected:
            self.repo.append_audit(case.id, "EVIDENCE_REJECTED", {
                "items": [{"id": e.id, "key": e.evidence_key,
                           "reason": v.failure_reason}
                          for e, v in rejected],
                "note": "rejected evidence is preserved and visible"})
        return verdicts, admitted

    def _act_with_retries(self, case: Case, dispute: Dispute,
                          action_type: str, bundle: dict,
                          outcome: DecisionOutcome) -> None:
        """Exponential backoff, same idempotency key on every attempt (the
        key IS the dispute id, so a retry can never mint a second action)."""
        last_error: TransientPaymentsError | None = None
        for attempt in range(1, config.MAX_SUBMIT_RETRIES + 1):
            try:
                execute_action(self.repo, self.adapter, case_id=case.id,
                               dispute_id=dispute.id, action_type=action_type,
                               payload=bundle, actor=Actor.AGENT,
                               decision_meta=outcome.to_dict())
                return
            except TransientPaymentsError as e:
                last_error = e
                if attempt < config.MAX_SUBMIT_RETRIES:
                    self.sleep(self.backoff_base_s * 2 ** (attempt - 1))
        raise _Escalate(
            f"payment execution failed after {config.MAX_SUBMIT_RETRIES} "
            f"attempts ({last_error}) — the prepared {action_type} is ready "
            f"for manual submission",
            details=[f"attempted action: {action_type}",
                     f"idempotency key: {dispute.id}",
                     f"retries: {config.MAX_SUBMIT_RETRIES}",
                     f"last adapter error: {last_error}",
                     f"prepared bundle: {sorted(bundle)}"],
            extra={"prepared_bundle": {k: v for k, v in bundle.items()
                                       if k != "representment"},
                   "has_draft": "representment" in bundle})

    # -- escalation ------------------------------------------------------------------

    def _escalate(self, case: Case, dispute: Dispute,
                  e: _Escalate) -> CaseResult:
        summary = self._merchant_summary(dispute, e)
        case = self.repo.update_case_state(case.id, CaseState.ESCALATED)
        self.repo.append_audit(case.id, "CASE_ESCALATED", {
            "reason": e.reason, "details": e.details,
            "hours_left": round(self.hours_left(dispute), 1),
            "merchant_summary": summary,
            "money_action_taken": self.repo.get_action_by_idempotency_key(
                dispute.id) is not None,
            **e.extra})
        return CaseResult(case=case, final_state=CaseState.ESCALATED,
                          escalation_summary=summary)

    def _merchant_summary(self, dispute: Dispute, e: _Escalate) -> str:
        acted = self.repo.get_action_by_idempotency_key(dispute.id)
        lines = [
            f"Dispute #{dispute.id}",
            f"Amount: \u20b9{dispute.amount:,}",
            f"Hours remaining: {max(0, round(self.hours_left(dispute)))}",
            "",
            "Recommended action: HUMAN REVIEW",
            "",
            "Reason:",
            e.reason,
        ]
        if e.details:
            lines += ["", "Missing / conflicting evidence:" if any(
                "missing" in d or "mismatch" in d for d in e.details)
                else "Details:"]
            lines += [f"- {d}" for d in e.details]
        lines += ["", ("A payment action WAS already submitted "
                       f"({acted.type}) — see audit trail." if acted
                       else "No payment action was executed.")]
        return "\n".join(lines)


# -- observability -----------------------------------------------------------------------

def format_timeline(repo: Repository, case_id: str) -> str:
    """Human-readable per-case timeline reconstructed purely from the audit
    chain — this later powers the dashboard and the demo."""
    import json as _json
    lines = [case_id]
    for e in repo.read_audit(case_id):
        p = _json.loads(e.payload_json)
        detail = {
            "CASE_CREATED": lambda: f"\u20b9{p.get('amount'):,} "
                                    f"{p.get('reason_code')} "
                                    f"({p.get('hours_left')}h left)",
            "LINK_COMPLETED": lambda: f"{p.get('method')} -> {p.get('order_id')} "
                                      f"(conf {p.get('confidence')})",
            "GATHER_COMPLETED": lambda: f"{len(p.get('documents', []))} documents",
            "EVIDENCE_EXTRACTED": lambda: f"{p.get('count')} candidates "
                                          f"{p.get('keys')}",
            "EVIDENCE_ADMITTED": lambda: f"{len(p.get('ids', []))} admitted",
            "EVIDENCE_REJECTED": lambda: f"{len(p.get('items', []))} rejected: "
                + "; ".join(i['reason'][:60] for i in p.get('items', [])),
            "DECISION_MADE": lambda: f"{p.get('action')} via {p.get('rule_fired')} "
                                     f"(EV fight \u20b9{p.get('ev_fight')})",
            "DRAFT_VALIDATED": lambda: "citations clean",
            "ACTION_SUBMITTED": lambda: f"{p.get('action')} via {p.get('adapter')}"
                                        + (" [SIMULATED]" if p.get('simulated')
                                           else ""),
            "CASE_ESCALATED": lambda: p.get("reason", "")[:80],
            "CASE_CLOSED": lambda: f"dispute {p.get('dispute_status')}",
        }.get(e.step, lambda: "")()
        lines.append(f"{e.at}  {e.step:<20} {detail}")
    return "\n".join(lines)
