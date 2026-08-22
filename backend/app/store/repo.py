"""Repository layer: dataclass <-> SQLite, with invariants enforced in code.

Rules encoded here (not left to callers):
- Enum-typed fields are validated through the Python enums before any SQL runs,
  so callers get a clear ``ValueError`` instead of a database error.
- Case state changes go through :meth:`update_case_state`, which consults
  ``ALLOWED_TRANSITIONS`` (the §8 state machine). There is no generic
  ``update_case`` that could sidestep it.
- The audit log is append-only *by API shape*: this class exposes
  ``append_audit`` and ``read_audit`` and nothing else. No update, no delete.
  (Cryptographic tamper-evidence — the hash chain — is a later stage.)
- Entities that the workflow never edits (orders, refunds, decisions, actions,
  outcomes, ...) get insert + read only. Fewer mutation paths, fewer bugs.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..audit.chain import GENESIS, canonical_json, compute_entry_hash, redact
from .db import connect, init_db
from .models import (
    ALLOWED_TRANSITIONS,
    ActionRecord,
    Actor,
    AuditLog,
    Case,
    CaseState,
    Decision,
    DecisionAction,
    Dispute,
    DisputeStatus,
    Document,
    DocumentType,
    Evidence,
    GateVerdict,
    Merchant,
    Order,
    Outcome,
    OutcomeResult,
    ReasonCode,
    Refund,
    Shipment,
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TransitionError(ValueError):
    """Raised when a case state change violates the §8 state machine."""


class Repository:
    """Thin, explicit persistence layer over sqlite3."""

    def __init__(self, db_path: str | Path):
        init_db(db_path)
        self.conn: sqlite3.Connection = connect(db_path)

    def close(self) -> None:
        self.conn.close()

    # -- internals -----------------------------------------------------------

    def _insert(self, table: str, cols: dict) -> None:
        placeholders = ", ".join("?" for _ in cols)
        names = ", ".join(cols)
        with self.conn:
            self.conn.execute(
                f"INSERT INTO {table} ({names}) VALUES ({placeholders})",
                tuple(cols.values()),
            )

    def _get(self, table: str, key: str, value: str) -> sqlite3.Row | None:
        cur = self.conn.execute(f"SELECT * FROM {table} WHERE {key} = ?", (value,))
        return cur.fetchone()

    # -- merchants / orders / refunds / shipments ------------------------------

    def add_merchant(self, m: Merchant) -> None:
        self._insert("merchants", {
            "id": m.id, "name": m.name,
            "auto_accept_cap": m.auto_accept_cap,
            "escalation_amount_cap": m.escalation_amount_cap,
        })

    def get_merchant(self, merchant_id: str) -> Merchant | None:
        r = self._get("merchants", "id", merchant_id)
        return Merchant(**dict(r)) if r else None

    def add_order(self, o: Order) -> None:
        self._insert("orders", {
            "id": o.id, "merchant_id": o.merchant_id, "payment_id": o.payment_id,
            "amount": o.amount, "customer_email": o.customer_email,
            "address": o.address, "created_at": o.created_at,
            "promised_ship_by": o.promised_ship_by,
        })

    def get_order(self, order_id: str) -> Order | None:
        r = self._get("orders", "id", order_id)
        return Order(**dict(r)) if r else None

    def get_order_by_payment(self, payment_id: str) -> Order | None:
        r = self._get("orders", "payment_id", payment_id)
        return Order(**dict(r)) if r else None

    def add_refund(self, rf: Refund) -> None:
        self._insert("refunds", {
            "id": rf.id, "order_id": rf.order_id,
            "amount": rf.amount, "created_at": rf.created_at,
        })

    def list_refunds_for_order(self, order_id: str) -> list[Refund]:
        cur = self.conn.execute(
            "SELECT * FROM refunds WHERE order_id = ? ORDER BY created_at", (order_id,))
        return [Refund(**dict(r)) for r in cur.fetchall()]

    def add_shipment(self, s: Shipment) -> None:
        self._insert("shipments", {
            "id": s.id, "order_id": s.order_id, "awb": s.awb, "courier": s.courier,
            "ship_date": s.ship_date, "status": s.status, "pod_doc_id": s.pod_doc_id,
        })

    def list_shipments_for_order(self, order_id: str) -> list[Shipment]:
        cur = self.conn.execute(
            "SELECT * FROM shipments WHERE order_id = ? ORDER BY ship_date", (order_id,))
        return [Shipment(**dict(r)) for r in cur.fetchall()]

    # -- documents -------------------------------------------------------------

    def add_document(self, d: Document) -> None:
        self._insert("documents", {
            "id": d.id, "case_id": d.case_id, "type": DocumentType(d.type).value,
            "raw_text": d.raw_text, "source": d.source, "fetched_at": d.fetched_at,
        })

    def get_document(self, doc_id: str) -> Document | None:
        r = self._get("documents", "id", doc_id)
        if not r:
            return None
        d = dict(r)
        d["type"] = DocumentType(d["type"])
        return Document(**d)

    def list_documents_for_case(self, case_id: str) -> list[Document]:
        cur = self.conn.execute(
            "SELECT * FROM documents WHERE case_id = ? ORDER BY fetched_at", (case_id,))
        out = []
        for r in cur.fetchall():
            d = dict(r)
            d["type"] = DocumentType(d["type"])
            out.append(Document(**d))
        return out

    # -- disputes / cases --------------------------------------------------------

    def add_dispute(self, dp: Dispute) -> None:
        self._insert("disputes", {
            "id": dp.id, "payment_id": dp.payment_id, "amount": dp.amount,
            "reason_code": ReasonCode(dp.reason_code).value,
            "respond_by": dp.respond_by,
            "status": DisputeStatus(dp.status).value,
        })

    def get_dispute(self, dispute_id: str) -> Dispute | None:
        r = self._get("disputes", "id", dispute_id)
        if not r:
            return None
        d = dict(r)
        d["reason_code"] = ReasonCode(d["reason_code"])
        d["status"] = DisputeStatus(d["status"])
        return Dispute(**d)

    def update_dispute_status(self, dispute_id: str, status: DisputeStatus) -> None:
        status = DisputeStatus(status)
        with self.conn:
            cur = self.conn.execute(
                "UPDATE disputes SET status = ? WHERE id = ?",
                (status.value, dispute_id))
            if cur.rowcount == 0:
                raise KeyError(f"no dispute {dispute_id!r}")

    def add_case(self, c: Case) -> None:
        self._insert("cases", {
            "id": c.id, "dispute_id": c.dispute_id,
            "state": CaseState(c.state).value,
            "linked_order_id": c.linked_order_id,
            "link_confidence": c.link_confidence,
        })

    def get_case(self, case_id: str) -> Case | None:
        r = self._get("cases", "id", case_id)
        if not r:
            return None
        d = dict(r)
        d["state"] = CaseState(d["state"])
        return Case(**d)

    def get_case_by_dispute(self, dispute_id: str) -> Case | None:
        r = self._get("cases", "dispute_id", dispute_id)
        if not r:
            return None
        d = dict(r)
        d["state"] = CaseState(d["state"])
        return Case(**d)

    def update_case_state(self, case_id: str, new_state: CaseState) -> Case:
        """The ONLY way to move a case. Enforces the §8 state machine."""
        new_state = CaseState(new_state)
        case = self.get_case(case_id)
        if case is None:
            raise KeyError(f"no case {case_id!r}")
        if new_state not in ALLOWED_TRANSITIONS[case.state]:
            raise TransitionError(
                f"illegal transition {case.state.value} -> {new_state.value} "
                f"for case {case_id}")
        with self.conn:
            self.conn.execute(
                "UPDATE cases SET state = ? WHERE id = ?",
                (new_state.value, case_id))
        case.state = new_state
        return case

    def set_case_link(self, case_id: str, order_id: str, confidence: float) -> None:
        if not (0.0 <= confidence <= 1.0):
            raise ValueError(f"link confidence out of range: {confidence}")
        with self.conn:
            cur = self.conn.execute(
                "UPDATE cases SET linked_order_id = ?, link_confidence = ? WHERE id = ?",
                (order_id, confidence, case_id))
            if cur.rowcount == 0:
                raise KeyError(f"no case {case_id!r}")

    # -- evidence / decisions / actions / outcomes -------------------------------

    def add_evidence(self, e: Evidence) -> None:
        self._insert("evidence", {
            "id": e.id, "case_id": e.case_id, "evidence_key": e.evidence_key,
            "claim": e.claim,
            "source_doc_id": e.source_doc_id, "quoted_span": e.quoted_span,
            "fields_json": e.fields_json,
            "gate_verdict": GateVerdict(e.gate_verdict).value if e.gate_verdict else None,
            "fail_reason": e.fail_reason,
        })

    def set_evidence_verdict(self, evidence_id: str, verdict: GateVerdict,
                             fail_reason: str | None = None) -> None:
        verdict = GateVerdict(verdict)
        if verdict is GateVerdict.FAIL and not fail_reason:
            raise ValueError("FAIL verdict requires a fail_reason")
        if verdict is GateVerdict.PASS and fail_reason:
            raise ValueError("PASS verdict must not carry a fail_reason")
        with self.conn:
            cur = self.conn.execute(
                "UPDATE evidence SET gate_verdict = ?, fail_reason = ? WHERE id = ?",
                (verdict.value, fail_reason, evidence_id))
            if cur.rowcount == 0:
                raise KeyError(f"no evidence {evidence_id!r}")

    def list_evidence_for_case(self, case_id: str,
                               verdict: GateVerdict | None = None) -> list[Evidence]:
        if verdict is None:
            cur = self.conn.execute(
                "SELECT * FROM evidence WHERE case_id = ? ORDER BY id", (case_id,))
        else:
            cur = self.conn.execute(
                "SELECT * FROM evidence WHERE case_id = ? AND gate_verdict = ? ORDER BY id",
                (case_id, GateVerdict(verdict).value))
        out = []
        for r in cur.fetchall():
            d = dict(r)
            d["gate_verdict"] = GateVerdict(d["gate_verdict"]) if d["gate_verdict"] else None
            out.append(Evidence(**d))
        return out

    def add_decision(self, dec: Decision) -> None:
        self._insert("decisions", {
            "id": dec.id, "case_id": dec.case_id,
            "action": DecisionAction(dec.action).value,
            "completeness": dec.completeness, "p_win": dec.p_win,
            "ev_fight": dec.ev_fight, "ev_accept": dec.ev_accept,
            "thresholds_version": dec.thresholds_version,
        })

    def list_decisions_for_case(self, case_id: str) -> list[Decision]:
        cur = self.conn.execute(
            "SELECT * FROM decisions WHERE case_id = ? ORDER BY id", (case_id,))
        out = []
        for r in cur.fetchall():
            d = dict(r)
            d["action"] = DecisionAction(d["action"])
            out.append(Decision(**d))
        return out

    def add_action(self, a: ActionRecord) -> None:
        self._insert("actions", {
            "id": a.id, "case_id": a.case_id, "type": a.type,
            "idempotency_key": a.idempotency_key,
            "request_json": a.request_json, "response_json": a.response_json,
            "actor": Actor(a.actor).value, "at": a.at,
        })

    def get_action_by_idempotency_key(self, key: str) -> ActionRecord | None:
        r = self._get("actions", "idempotency_key", key)
        if not r:
            return None
        d = dict(r)
        d["actor"] = Actor(d["actor"])
        return ActionRecord(**d)

    def add_outcome(self, o: Outcome) -> None:
        self._insert("outcomes", {
            "id": o.id, "case_id": o.case_id,
            "result": OutcomeResult(o.result).value,
            "amount_recovered": o.amount_recovered,
        })

    def get_outcome_for_case(self, case_id: str) -> Outcome | None:
        r = self._get("outcomes", "case_id", case_id)
        if not r:
            return None
        d = dict(r)
        d["result"] = OutcomeResult(d["result"])
        return Outcome(**d)

    # -- audit log: APPEND-ONLY by API shape --------------------------------------

    def append_audit(self, case_id: str, step: str, payload: dict) -> AuditLog:
        """Insert a hash-chained audit entry (spec §19).

        No update or delete method exists on purpose. The payload is redacted
        (secrets never enter the trail) and canonicalized BEFORE hashing, so
        stored bytes and hashed bytes are identical. Chain is per-case:
        prev_hash = the case's previous entry_hash, or GENESIS."""
        if not isinstance(payload, dict):
            raise TypeError("audit payload must be a dict (canonicalized and "
                            "hashed deterministically)")
        payload_json = canonical_json(redact(payload))
        at = utc_now_iso()
        with self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_log WHERE case_id = ? "
                "ORDER BY seq DESC LIMIT 1", (case_id,)).fetchone()
            prev_hash = row["entry_hash"] if row and row["entry_hash"] else GENESIS
            entry_hash = compute_entry_hash(prev_hash, case_id, step,
                                            payload_json, at)
            cur = self.conn.execute(
                "INSERT INTO audit_log (case_id, step, payload_json, at, "
                "prev_hash, entry_hash) VALUES (?, ?, ?, ?, ?, ?)",
                (case_id, step, payload_json, at, prev_hash, entry_hash))
            seq = cur.lastrowid
        return AuditLog(seq=seq, case_id=case_id, step=step,
                        payload_json=payload_json, at=at,
                        prev_hash=prev_hash, entry_hash=entry_hash)

    def read_audit(self, case_id: str) -> list[AuditLog]:
        cur = self.conn.execute(
            "SELECT * FROM audit_log WHERE case_id = ? ORDER BY seq", (case_id,))
        return [AuditLog(**dict(r)) for r in cur.fetchall()]
