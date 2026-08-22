"""Stage-2 tests: domain models + SQLite store.

Covers: schema creation, dataclass round-trips for every entity, FK
enforcement, enum/state validation, the case state machine, the append-only
audit API shape, and persistence across connections. Deterministic and
isolated: each test gets a fresh temp-file database.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.store.models import (
    ActionRecord, Actor, Case, CaseState, Decision, DecisionAction, Dispute,
    DisputeStatus, Document, DocumentType, Evidence, GateVerdict, Merchant,
    Order, Outcome, OutcomeResult, ReasonCode, Refund, Shipment,
)
from app.store.repo import Repository, TransitionError


def sample_merchant() -> Merchant:
    return Merchant(id="m_1", name="Kadai Crafts", auto_accept_cap=2000,
                    escalation_amount_cap=10000)


def sample_order() -> Order:
    return Order(id="ord_1", merchant_id="m_1", payment_id="pay_1", amount=3499,
                 customer_email="asha@example.com", address="12 MG Road, 560001",
                 created_at="2026-08-01T10:00:00+00:00",
                 promised_ship_by="2026-08-04T10:00:00+00:00")


def sample_dispute() -> Dispute:
    return Dispute(id="disp_1", payment_id="pay_1", amount=3499,
                   reason_code=ReasonCode.GOODS_NOT_RECEIVED,
                   respond_by="2026-08-20T10:00:00+00:00")


class StoreTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.repo = Repository(self.db_path)
        self.addCleanup(self.repo.close)
        self.addCleanup(self.tmp.cleanup)

    def seed_case(self) -> Case:
        self.repo.add_merchant(sample_merchant())
        self.repo.add_order(sample_order())
        self.repo.add_dispute(sample_dispute())
        case = Case(id="case_1", dispute_id="disp_1")
        self.repo.add_case(case)
        return case


class TestSchemaAndRoundTrips(StoreTestBase):
    def test_schema_created_with_all_tables(self):
        cur = self.repo.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        tables = {r["name"] for r in cur.fetchall()}
        self.assertEqual(tables, {
            "merchants", "orders", "refunds", "shipments", "documents",
            "disputes", "cases", "evidence", "decisions", "actions",
            "outcomes", "audit_log"})

    def test_full_entity_round_trips(self):
        case = self.seed_case()
        self.repo.add_refund(Refund(id="rf_1", order_id="ord_1", amount=500,
                                    created_at="2026-08-05T09:00:00+00:00"))
        self.repo.add_document(Document(
            id="doc_1", case_id=case.id, type=DocumentType.EMAIL,
            raw_text="bhaiya parcel mil gaya but size galat hai, refund kar do",
            source="mail:thread_88", fetched_at="2026-08-15T11:00:00+00:00"))
        self.repo.add_shipment(Shipment(
            id="shp_1", order_id="ord_1", awb="AWB123", courier="Delhivery",
            ship_date="2026-08-03T08:00:00+00:00", status="delivered",
            pod_doc_id="doc_1"))
        self.repo.add_evidence(Evidence(
            id="ev_1", case_id=case.id, evidence_key="admission_email",
            claim="customer acknowledged delivery",
            source_doc_id="doc_1", quoted_span="parcel mil gaya",
            fields_json=json.dumps({"acknowledged": True})))
        self.repo.add_decision(Decision(
            id="dec_1", case_id=case.id, action=DecisionAction.FIGHT,
            completeness=1.0, p_win=0.8, ev_fight=2299.2, ev_accept=-3499.0,
            thresholds_version="v1"))
        self.repo.add_action(ActionRecord(
            id="act_1", case_id=case.id, type="contest",
            idempotency_key="disp_1", request_json="{}", response_json="{}",
            actor=Actor.AGENT, at="2026-08-16T12:00:00+00:00"))
        self.repo.add_outcome(Outcome(id="out_1", case_id=case.id,
                                      result=OutcomeResult.WON,
                                      amount_recovered=3499))

        self.assertEqual(self.repo.get_merchant("m_1"), sample_merchant())
        self.assertEqual(self.repo.get_order("ord_1"), sample_order())
        self.assertEqual(self.repo.get_order_by_payment("pay_1").id, "ord_1")
        self.assertEqual(self.repo.get_dispute("disp_1").reason_code,
                         ReasonCode.GOODS_NOT_RECEIVED)
        self.assertEqual(self.repo.list_refunds_for_order("ord_1")[0].amount, 500)
        self.assertEqual(self.repo.list_shipments_for_order("ord_1")[0].awb, "AWB123")
        doc = self.repo.get_document("doc_1")
        self.assertIs(doc.type, DocumentType.EMAIL)
        ev = self.repo.list_evidence_for_case(case.id)[0]
        self.assertIsNone(ev.gate_verdict)
        self.assertEqual(json.loads(ev.fields_json), {"acknowledged": True})
        self.assertIs(self.repo.list_decisions_for_case(case.id)[0].action,
                      DecisionAction.FIGHT)
        self.assertIs(self.repo.get_action_by_idempotency_key("disp_1").actor,
                      Actor.AGENT)
        self.assertIs(self.repo.get_outcome_for_case(case.id).result,
                      OutcomeResult.WON)

    def test_persistence_across_connections(self):
        self.seed_case()
        self.repo.close()
        repo2 = Repository(self.db_path)
        self.addCleanup(repo2.close)
        self.assertIsNotNone(repo2.get_case("case_1"))
        self.assertEqual(repo2.get_dispute("disp_1").amount, 3499)


class TestIntegrityAndValidation(StoreTestBase):
    def test_foreign_keys_enforced(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.add_order(sample_order())  # merchant m_1 missing
        self.repo.add_merchant(sample_merchant())
        self.repo.add_order(sample_order())
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.add_case(Case(id="case_x", dispute_id="ghost_dispute"))

    def test_one_case_per_dispute(self):
        self.seed_case()
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.add_case(Case(id="case_dup", dispute_id="disp_1"))

    def test_idempotency_key_unique(self):
        case = self.seed_case()
        a = ActionRecord(id="act_1", case_id=case.id, type="contest",
                         idempotency_key="disp_1", request_json="{}",
                         response_json="{}", actor=Actor.AGENT, at="t")
        self.repo.add_action(a)
        a2 = ActionRecord(id="act_2", case_id=case.id, type="contest",
                          idempotency_key="disp_1", request_json="{}",
                          response_json="{}", actor=Actor.AGENT, at="t")
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.add_action(a2)

    def test_invalid_enum_values_rejected_by_repo(self):
        self.seed_case()
        with self.assertRaises(ValueError):
            self.repo.add_dispute(Dispute(id="d2", payment_id="p2", amount=100,
                                          reason_code="not_a_reason",
                                          respond_by="t"))
        with self.assertRaises(ValueError):
            self.repo.update_dispute_status("disp_1", "vanished")
        with self.assertRaises(ValueError):
            self.repo.set_case_link("case_1", "ord_1", confidence=1.5)

    def test_check_constraints_block_raw_sql_bypass(self):
        """Defense-in-depth: even raw SQL cannot persist an invalid state."""
        self.seed_case()
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.conn.execute(
                "UPDATE cases SET state = 'quantum_superposition' WHERE id = 'case_1'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.conn.execute(
                "INSERT INTO merchants (id, name, auto_accept_cap, escalation_amount_cap)"
                " VALUES ('m_bad', 'x', 5000, 1000)")  # caps inverted

    def test_case_state_machine_enforced(self):
        self.seed_case()
        # legal happy path
        for s in (CaseState.LINKING, CaseState.GATHERING, CaseState.GATED,
                  CaseState.DECIDED, CaseState.ACTED, CaseState.CLOSED):
            self.repo.update_case_state("case_1", s)
        self.assertIs(self.repo.get_case("case_1").state, CaseState.CLOSED)
        # closed is terminal
        with self.assertRaises(TransitionError):
            self.repo.update_case_state("case_1", CaseState.INTAKE)

    def test_illegal_jump_rejected(self):
        self.seed_case()
        with self.assertRaises(TransitionError):
            self.repo.update_case_state("case_1", CaseState.ACTED)  # intake -> acted

    def test_escalation_reachable_from_live_states(self):
        self.seed_case()
        self.repo.update_case_state("case_1", CaseState.LINKING)
        self.repo.update_case_state("case_1", CaseState.ESCALATED)
        # human approval moves an escalated case forward
        self.repo.update_case_state("case_1", CaseState.ACTED)

    def test_gate_verdict_consistency_rules(self):
        case = self.seed_case()
        self.repo.add_document(Document(id="doc_1", case_id=case.id,
                                        type=DocumentType.POD, raw_text="pod",
                                        source="ship", fetched_at="t"))
        self.repo.add_evidence(Evidence(id="ev_1", case_id=case.id,
                                        evidence_key="pod", claim="c",
                                        source_doc_id="doc_1", quoted_span="q",
                                        fields_json="{}"))
        with self.assertRaises(ValueError):
            self.repo.set_evidence_verdict("ev_1", GateVerdict.FAIL)  # no reason
        with self.assertRaises(ValueError):
            self.repo.set_evidence_verdict("ev_1", GateVerdict.PASS, "reason?!")
        self.repo.set_evidence_verdict("ev_1", GateVerdict.FAIL, "awb mismatch")
        failed = self.repo.list_evidence_for_case(case.id, GateVerdict.FAIL)
        self.assertEqual(failed[0].fail_reason, "awb mismatch")


class TestAuditLogAppendOnly(StoreTestBase):
    def test_append_and_ordered_read(self):
        case = self.seed_case()
        e1 = self.repo.append_audit(case.id, "intake", {"ok": 1})
        e2 = self.repo.append_audit(case.id, "linking", {"ok": 2})
        entries = self.repo.read_audit(case.id)
        self.assertEqual([e.step for e in entries], ["intake", "linking"])
        self.assertLess(e1.seq, e2.seq)
        # Stage 7 completed the deferred hash chain: first entry links to
        # GENESIS, every entry carries its hash, entries link to each other
        from app.audit.chain import GENESIS
        self.assertEqual(entries[0].prev_hash, GENESIS)
        self.assertEqual(len(entries[0].entry_hash), 64)
        self.assertEqual(entries[1].prev_hash, entries[0].entry_hash)

    def test_repository_exposes_no_mutation_path_for_audit(self):
        audit_methods = [m for m in dir(self.repo)
                         if "audit" in m.lower() and not m.startswith("_")]
        self.assertEqual(sorted(audit_methods), ["append_audit", "read_audit"])
        for verb in ("update", "delete", "remove", "edit", "purge", "truncate"):
            self.assertFalse(
                any(verb in m.lower() for m in dir(self.repo) if "audit" in m.lower()),
                f"repository must not expose an audit {verb} method")

    def test_audit_requires_existing_case(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.append_audit("ghost_case", "intake", {})


if __name__ == "__main__":
    unittest.main()
