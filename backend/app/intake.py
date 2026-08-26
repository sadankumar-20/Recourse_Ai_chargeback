"""Interactive intake (R4): 'Tell Recourse what happened.'

Coordination layer between the merchant's words and the existing machinery:
1. The ORIGINAL text is stored verbatim as a case document (provenance
   user_submitted) — the AI's interpretation is stored separately in the
   audit trail and can never overwrite the source material.
2. Triage (untrusted) proposes a reason code and extracts references; the
   dispute is anchored to a real order via payment_id (given or extracted)
   or an order reference. Unresolvable reports fail with a structured
   'what's missing' answer instead of a guessed case.
3. From there the case is a first-class citizen of the EXISTING pipeline:
   the same orchestrator, investigator, gate, decision engine, executor,
   audit. Intake creates context; it decides nothing and executes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .ai.intake_triage import Triage, triage_intake
from .store.models import (
    Case,
    Dispute,
    Document,
    DocumentType,
    Provenance,
    ReasonCode,
)
from .store.repo import Repository, utc_now_iso

USER_DISPUTE_RESPOND_HOURS = 168        # 7 days for merchant-submitted cases


class IntakeError(ValueError):
    def __init__(self, message: str, missing: list[str] | None = None,
                 interpretation: dict | None = None):
        super().__init__(message)
        self.missing = missing or []
        self.interpretation = interpretation or {}


@dataclass(frozen=True)
class IntakeResult:
    case: Case
    dispute: Dispute
    triage: Triage
    narrative_doc_id: str


def submit_intake(repo: Repository, text: str, client, *, now: datetime,
                  payment_id: str | None = None,
                  dispute_id: str | None = None) -> IntakeResult:
    text = (text or "").strip()
    if len(text) < 15:
        raise IntakeError("tell Recourse what happened — a couple of "
                          "sentences about the dispute", ["description"])
    triage = triage_intake(text, client)

    if dispute_id:
        dispute = repo.get_dispute(dispute_id)
        if dispute is None:
            raise IntakeError(f"no dispute {dispute_id!r} exists",
                              interpretation=triage.to_dict())
    else:
        dispute = _anchor_dispute(repo, triage, payment_id, now)

    if repo.get_case_by_dispute(dispute.id) is not None:
        raise IntakeError(
            f"dispute {dispute.id} already has a case — open it instead of "
            f"creating a duplicate", interpretation=triage.to_dict())

    case = Case(id=f"case_{dispute.id}", dispute_id=dispute.id)
    repo.add_case(case)
    doc = Document(id=f"doc_intake_{dispute.id}", case_id=case.id,
                   type=DocumentType.LOG, raw_text=text,
                   source="intake:merchant", fetched_at=utc_now_iso(),
                   provenance=Provenance.USER_SUBMITTED.value)
    repo.add_document(doc)
    repo.append_audit(case.id, "CASE_SUBMITTED", {
        "channel": "interactive_intake",
        "original_chars": len(text), "narrative_doc": doc.id,
        "provenance": Provenance.USER_SUBMITTED.value,
        "interpretation": {**triage.to_dict(),
                           "note": "untrusted AI interpretation — the "
                                   "verbatim original is the source "
                                   "material; nothing here decides the "
                                   "case"},
        "dispute_id": dispute.id, "respond_by": dispute.respond_by})
    return IntakeResult(case=case, dispute=dispute, triage=triage,
                        narrative_doc_id=doc.id)


def _anchor_dispute(repo: Repository, triage: Triage,
                    payment_id: str | None, now: datetime) -> Dispute:
    pay = payment_id or triage.payment_id
    order = repo.get_order_by_payment(pay) if pay else None
    if order is None and triage.order_ref:
        for candidate in (triage.order_ref, f"ord_{triage.order_ref}",
                          f"ord_{triage.order_ref.zfill(4)}"):
            order = repo.get_order(candidate)
            if order:
                break
    if order is None:
        raise IntakeError(
            "could not anchor the report to an order — provide a payment id "
            "(pay_…) or an order reference",
            missing=["payment_id or order reference"],
            interpretation=triage.to_dict())
    dispute_id = f"disp_u_{order.id.removeprefix('ord_')}"
    existing = repo.get_dispute(dispute_id)
    if existing is not None:
        return existing
    dispute = Dispute(
        id=dispute_id, payment_id=order.payment_id, amount=order.amount,
        reason_code=ReasonCode(triage.reason_code),
        respond_by=(now + timedelta(hours=USER_DISPUTE_RESPOND_HOURS)
                    ).isoformat(timespec="seconds"),
        provenance=Provenance.USER_SUBMITTED.value)
    repo.add_dispute(dispute)
    return dispute
