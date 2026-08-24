"""R1 tests: provenance (schema v3) and the read-only tool registry.

The properties under test are the ones the agentic loop (R2) will lean on:
tools cannot write, budgets cannot be exceeded, every call is chained into
the audit, arguments are validated, and provenance flows to the caller.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.audit.chain import verify_audit_chain
from app.datagen import generate
from app.store.db import SchemaVersionError
from app.store.models import (
    Case,
    CaseState,
    Dispute,
    Document,
    DocumentType,
    Provenance,
    ReasonCode,
)
from app.store.repo import Repository
from app.tools.investigation import (
    DEFAULT_TOOL_BUDGET,
    TOOLS,
    ReadOnlyRepo,
    ToolAccessDenied,
    ToolBudgetExceeded,
    ToolRegistry,
)


class WorldTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.data = Path(cls.tmp.name) / "data"
        generate(seed=42, out_dir=cls.data)
        cls.split = json.loads((cls.data / "split.json").read_text())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.dbdir = tempfile.TemporaryDirectory()
        self.db = Path(self.dbdir.name) / "w.db"
        shutil.copy(self.data / "dataset.db", self.db)
        self.repo = Repository(self.db)
        self.addCleanup(self.repo.close)
        self.addCleanup(self.dbdir.cleanup)
        self.did = self.split["dev"][0]
        self.repo.add_case(Case(id="case_t", dispute_id=self.did))
        self.registry = ToolRegistry(self.repo, "case_t")


class TestSchemaV3Provenance(WorldTestBase):
    def test_defaults_are_simulator(self):
        d = self.repo.get_dispute(self.did)
        self.assertEqual(d.provenance, "simulator")
        row = self.repo.conn.execute(
            "SELECT provenance FROM documents LIMIT 1").fetchone()
        self.assertEqual(row["provenance"], "simulator")

    def test_provenance_round_trip(self):
        self.repo.add_document(Document(
            id="doc_up1", case_id=None, type=DocumentType.POD,
            raw_text="Delivered to 560001", source="upload:user",
            fetched_at="2026-08-23T12:00:00+00:00",
            provenance=Provenance.USER_UPLOAD.value))
        self.assertEqual(self.repo.get_document("doc_up1").provenance,
                         "user_upload")

    def test_invalid_provenance_rejected_at_insert_and_in_sql(self):
        with self.assertRaises(ValueError):
            self.repo.add_document(Document(
                id="doc_bad", case_id=None, type=DocumentType.POD,
                raw_text="x", source="s",
                fetched_at="2026-08-23T12:00:00+00:00",
                provenance="made_up_source"))
        with self.assertRaises(sqlite3.IntegrityError):   # CHECK is the backstop
            with self.repo.conn:
                self.repo.conn.execute(
                    "INSERT INTO documents (id, type, raw_text, source, "
                    "fetched_at, provenance) VALUES ('doc_bad2','pod','x','s',"
                    "'2026-08-23T12:00:00+00:00','made_up_source')")

    def test_needs_input_transitions(self):
        self.repo.update_case_state("case_t", CaseState.LINKING)
        self.repo.update_case_state("case_t", CaseState.GATHERING)
        self.repo.update_case_state("case_t", CaseState.NEEDS_INPUT)
        self.repo.update_case_state("case_t", CaseState.GATHERING)  # resume
        self.repo.update_case_state("case_t", CaseState.NEEDS_INPUT)
        self.repo.update_case_state("case_t", CaseState.ESCALATED)  # give up
        self.assertIs(self.repo.get_case("case_t").state, CaseState.ESCALATED)

    def test_needs_input_illegal_jumps_rejected(self):
        for target in (CaseState.NEEDS_INPUT,):        # from intake: illegal
            with self.assertRaises(ValueError):
                self.repo.update_case_state("case_t", target)

    def test_old_schema_version_fails_with_instructions(self):
        with tempfile.TemporaryDirectory() as d:
            old = Path(d) / "old.db"
            conn = sqlite3.connect(old)
            conn.execute("CREATE TABLE merchants (id TEXT PRIMARY KEY)")
            conn.execute("PRAGMA user_version = 2")
            conn.commit(); conn.close()
            with self.assertRaises(SchemaVersionError) as cm:
                Repository(old)
            self.assertIn("generate.py", str(cm.exception))


class TestReadOnlyByConstruction(WorldTestBase):
    def test_whitelisted_reads_work(self):
        ro = ReadOnlyRepo(self.repo)
        self.assertIsNotNone(ro.get_dispute(self.did))

    def test_every_write_surface_is_blocked(self):
        ro = ReadOnlyRepo(self.repo)
        for method in ("add_document", "add_dispute", "add_case", "add_order",
                       "add_action", "add_decision", "add_evidence",
                       "update_case_state", "update_dispute_status",
                       "set_case_link", "set_evidence_verdict",
                       "append_audit", "conn", "close"):
            with self.assertRaises(ToolAccessDenied, msg=method):
                getattr(ro, method)

    def test_attribute_mutation_blocked(self):
        ro = ReadOnlyRepo(self.repo)
        with self.assertRaises(ToolAccessDenied):
            ro.anything = 1

    def test_select_helper_refuses_non_select(self):
        ro = ReadOnlyRepo(self.repo)
        for sql in ("UPDATE disputes SET amount = 0",
                    "DELETE FROM audit_log",
                    "  insert into merchants values ('x','y',1,2)"):
            with self.assertRaises(ToolAccessDenied):
                ro.select(sql)

    def test_registry_module_has_no_money_surface(self):
        import inspect
        from app.tools import investigation
        src = inspect.getsource(investigation)
        for banned in ("execute_action", "contest_dispute", "accept_dispute",
                       "payments_adapter", "PaymentsAdapter"):
            self.assertNotIn(banned, src, banned)


class TestToolRegistry(WorldTestBase):
    def test_each_tool_happy_path(self):
        d = self.repo.get_dispute(self.did)
        order = self.repo.get_order_by_payment(d.payment_id)
        r = self.registry.execute("get_dispute", {"dispute_id": d.id})
        self.assertTrue(r.ok); self.assertEqual(r.data["amount"], d.amount)
        r = self.registry.execute("search_orders", {"payment_id": d.payment_id})
        self.assertTrue(r.ok); self.assertEqual(r.data["count"], 1)
        r = self.registry.execute("get_order", {"order_id": order.id})
        self.assertTrue(r.ok)
        r = self.registry.execute("get_shipments", {"order_id": order.id})
        self.assertTrue(r.ok)
        ships = r.data["shipments"]
        r = self.registry.execute("get_refunds", {"order_id": order.id})
        self.assertTrue(r.ok); self.assertIn("total_refunded", r.data)
        r = self.registry.execute("search_inbox",
                                  {"customer_email": order.customer_email})
        self.assertTrue(r.ok)
        if ships and ships[0]["pod_doc_id"]:
            r = self.registry.execute("read_document",
                                      {"doc_id": ships[0]["pod_doc_id"]})
            self.assertTrue(r.ok)
            self.assertEqual(r.provenance, ["simulator"])

    def test_argument_validation_matrix(self):
        cases = [
            ("get_order", {}, "missing required"),
            ("get_order", {"order_id": 123}, "must be str"),
            ("get_order", {"order_id": "x", "extra": 1}, "unknown argument"),
            ("nonexistent_tool", {}, "unknown tool"),
        ]
        for name, args, expected in cases:
            r = self.registry.execute(name, args)
            self.assertFalse(r.ok)
            self.assertIn(expected, r.error)

    def test_missing_entities_are_structured_errors_not_exceptions(self):
        r = self.registry.execute("get_order", {"order_id": "ord_ghost"})
        self.assertFalse(r.ok)
        self.assertIn("no order", r.error)

    def test_budget_exhaustion_raises_and_prior_calls_are_chained(self):
        small = ToolRegistry(self.repo, "case_t", budget=3)
        for _ in range(3):
            small.execute("get_dispute", {"dispute_id": self.did})
        with self.assertRaises(ToolBudgetExceeded):
            small.execute("get_dispute", {"dispute_id": self.did})
        entries = [e for e in self.repo.read_audit("case_t")
                   if e.step == "TOOL_CALL"]
        self.assertEqual(len(entries), 3)
        last = json.loads(entries[-1].payload_json)
        self.assertEqual(last["budget"], {"used": 3, "of": 3})

    def test_every_call_lands_in_a_valid_audit_chain(self):
        self.registry.execute("get_dispute", {"dispute_id": self.did})
        self.registry.execute("get_order", {"order_id": "ord_ghost"})  # error too
        entries = [json.loads(e.payload_json)
                   for e in self.repo.read_audit("case_t")
                   if e.step == "TOOL_CALL"]
        self.assertEqual(len(entries), 2)
        self.assertTrue(entries[0]["ok"])
        self.assertFalse(entries[1]["ok"])
        self.assertIn("result_summary", entries[0])
        self.assertTrue(verify_audit_chain(self.repo, "case_t").valid)

    def test_user_upload_provenance_propagates_through_read(self):
        self.repo.add_document(Document(
            id="doc_up2", case_id=None, type=DocumentType.EMAIL,
            raw_text="customer says parcel arrived", source="upload:merchant",
            fetched_at="2026-08-23T12:00:00+00:00",
            provenance=Provenance.USER_UPLOAD.value))
        r = self.registry.execute("read_document", {"doc_id": "doc_up2"})
        self.assertEqual(r.provenance, ["user_upload"])
        chained = json.loads([e for e in self.repo.read_audit("case_t")
                              if e.step == "TOOL_CALL"][-1].payload_json)
        self.assertEqual(chained["provenance"], ["user_upload"])

    def test_specs_for_model_shape(self):
        specs = ToolRegistry.specs_for_model()
        self.assertEqual({s["name"] for s in specs}, set(TOOLS))
        order_spec = next(s for s in specs if s["name"] == "get_order")
        self.assertEqual(order_spec["params"]["order_id"],
                         {"type": "str", "required": True})
        self.assertEqual(DEFAULT_TOOL_BUDGET, 12)
