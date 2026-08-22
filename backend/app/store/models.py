"""Domain models for Recourse (spec §12).

Plain dataclasses + enums. No behavior beyond identity and vocabulary —
business rules live in policy/, persistence in store/db.py + store/repo.py.

Money is integer INR rupees. Timestamps are ISO-8601 UTC strings; SQLite has
no datetime type and strings keep the audit log human-readable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Optional


class ReasonCode(StrEnum):
    GOODS_NOT_RECEIVED = "goods_not_received"
    NOT_AS_DESCRIBED = "not_as_described"
    DUPLICATE = "duplicate"
    FRAUD = "fraud"
    CREDIT_NOT_PROCESSED = "credit_not_processed"
    CANCELLED_RECURRING = "cancelled_recurring"


class CaseState(StrEnum):
    INTAKE = "intake"
    LINKING = "linking"
    GATHERING = "gathering"
    GATED = "gated"
    DECIDED = "decided"
    ACTED = "acted"
    CLOSED = "closed"
    ESCALATED = "escalated"


# The §8 state machine. Storage refuses any transition not listed here.
# ESCALATED is reachable from every live state (fail-safe exit), and a human
# approval moves an escalated case forward to ACTED.
ALLOWED_TRANSITIONS: dict[CaseState, set[CaseState]] = {
    CaseState.INTAKE: {CaseState.LINKING, CaseState.ESCALATED},
    CaseState.LINKING: {CaseState.GATHERING, CaseState.ESCALATED},
    CaseState.GATHERING: {CaseState.GATED, CaseState.ESCALATED},
    CaseState.GATED: {CaseState.DECIDED, CaseState.ESCALATED},
    CaseState.DECIDED: {CaseState.ACTED, CaseState.ESCALATED},
    CaseState.ACTED: {CaseState.CLOSED},
    CaseState.ESCALATED: {CaseState.ACTED, CaseState.CLOSED},
    CaseState.CLOSED: set(),
}


class DisputeStatus(StrEnum):
    OPEN = "open"
    UNDER_REVIEW = "under_review"
    WON = "won"
    LOST = "lost"
    ACCEPTED = "accepted"
    EXPIRED = "expired"


class GateVerdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"


class DecisionAction(StrEnum):
    FIGHT = "FIGHT"
    ACCEPT = "ACCEPT"
    ESCALATE = "ESCALATE"


class Actor(StrEnum):
    AGENT = "agent"
    HUMAN = "human"


class DocumentType(StrEnum):
    EMAIL = "email"
    POD = "pod"          # proof of delivery
    INVOICE = "invoice"
    LOG = "log"


class OutcomeResult(StrEnum):
    WON = "won"
    LOST = "lost"
    ACCEPTED = "accepted"
    EXPIRED = "expired"


# --- Entities ---------------------------------------------------------------

@dataclass
class Merchant:
    id: str
    name: str
    auto_accept_cap: int
    escalation_amount_cap: int


@dataclass
class Order:
    id: str
    merchant_id: str
    payment_id: str
    amount: int
    customer_email: str
    address: str
    created_at: str
    promised_ship_by: str


@dataclass
class Refund:
    id: str
    order_id: str
    amount: int
    created_at: str


@dataclass
class Shipment:
    id: str
    order_id: str
    awb: str
    courier: str
    ship_date: str
    status: str
    pod_doc_id: Optional[str] = None


@dataclass
class Document:
    id: str
    case_id: Optional[str]  # None until attached to a case
    type: DocumentType
    raw_text: str
    source: str
    fetched_at: str


@dataclass
class Dispute:
    id: str
    payment_id: str
    amount: int
    reason_code: ReasonCode
    respond_by: str
    status: DisputeStatus = DisputeStatus.OPEN


@dataclass
class Case:
    id: str
    dispute_id: str
    state: CaseState = CaseState.INTAKE
    linked_order_id: Optional[str] = None
    link_confidence: Optional[float] = None


@dataclass
class Evidence:
    id: str
    case_id: str
    claim: str
    source_doc_id: str
    quoted_span: str
    fields_json: str
    gate_verdict: Optional[GateVerdict] = None
    fail_reason: Optional[str] = None


@dataclass
class Decision:
    id: str
    case_id: str
    action: DecisionAction
    completeness: float
    p_win: float
    ev_fight: float
    ev_accept: float
    thresholds_version: str


@dataclass
class ActionRecord:
    id: str
    case_id: str
    type: str
    idempotency_key: str
    request_json: str
    response_json: str
    actor: Actor
    at: str


@dataclass
class Outcome:
    id: str
    case_id: str
    result: OutcomeResult
    amount_recovered: int


@dataclass
class AuditLog:
    """Append-only log entry (spec §12/§19).

    `seq` is a DB-assigned autoincrement so ordering is authoritative.
    `prev_hash`/`entry_hash` columns exist now so the schema is final, but the
    hash-chain computation is deferred to the dedicated audit stage; until
    then they persist as None.
    """
    seq: Optional[int]
    case_id: str
    step: str
    payload_json: str
    at: str
    prev_hash: Optional[str] = None
    entry_hash: Optional[str] = None
