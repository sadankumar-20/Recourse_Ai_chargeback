"""Stage-6 integration tests: the AI layer wired into the deterministic spine,
running on the real Stage-3 generated world (no bespoke fixtures).

The flow under test (spec §8):
  dispute -> candidate orders -> AI link -> documents -> AI extraction
    -> Admissibility Gate -> admitted evidence -> deterministic decision
    -> AI draft -> deterministic citation validation

Plus the ablation the panel story rests on: AI-only would accept a fabricated
claim; AI + Gate structurally cannot.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from app import datagen
from app.ai.client import ScriptedAIClient, StubAIClient
from app.ai.draft_representment import draft_representment
from app.ai.extract_evidence import extract_evidence
from app.ai.link_order import link_order
from app.config import LINK_CONFIDENCE_FLOOR
from app.datagen import generate
from app.policy.citations import validate_citations
from app.policy.decide import decide
from app.policy.gate import GateContext, admit_all, case_preconditions
from app.policy.playbooks import load_playbooks
from app.store.models import DecisionAction, GateVerdict
from app.store.repo import Repository


class AIPipelineBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name)
        generate(seed=42, out_dir=cls.out)
        cls.gt = json.loads((cls.out / "ground_truth.json").read_text())["labels"]
        cls.split = json.loads((cls.out / "split.json").read_text())
        cls.sim_now = datetime.fromisoformat(cls.split["sim_now"])
        cls.pb = load_playbooks()
        cls.client = StubAIClient()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.repo = Repository(self.out / "dataset.db")
        self.addCleanup(self.repo.close)

    def dev_dispute(self, scenario: str):
        for did in self.split["dev"]:
            if self.gt[did]["scenario"] == scenario:
                d = self.repo.get_dispute(did)
                if d.reason_code.value in self.pb.reason_codes:
                    return d
        self.fail(f"no dev dispute for scenario {scenario}")

    def case_materials(self, dispute):
        order = self.repo.get_order_by_payment(dispute.payment_id)
        shipments = self.repo.list_shipments_for_order(order.id)
        docs = []
        for s in shipments:
            if s.pod_doc_id:
                docs.append(self.repo.get_document(s.pod_doc_id))
        for row in self.repo.conn.execute(
                "SELECT id FROM documents WHERE source = ?",
                (f"mailbox:{order.customer_email}",)).fetchall():
            docs.append(self.repo.get_document(row["id"]))
        ctx = GateContext(dispute=dispute, order=order, shipments=shipments,
                          refunds=self.repo.list_refunds_for_order(order.id),
                          documents={d.id: d for d in docs},
                          playbooks=self.pb, now=self.sim_now)
        return order, docs, ctx


class TestEndToEndOnRealData(AIPipelineBase):
    def test_hinglish_dispute_full_pipeline(self):
        dispute = self.dev_dispute(datagen.HINGLISH)
        order, docs, ctx = self.case_materials(dispute)
        rp = self.pb.for_reason(dispute.reason_code)

        # AI extraction over the real messy documents
        ext = extract_evidence(f"pre:{dispute.id}", dispute, docs, rp, self.client)
        self.assertTrue(ext.candidates)

        # Hinglish quote must remain untranslated and verbatim
        adm = [c for c in ext.candidates if c.evidence_key == "admission_email"]
        self.assertTrue(adm, "admission must be extracted from the thread")
        thread = ctx.documents[adm[0].source_doc_id].raw_text
        self.assertIn(adm[0].quoted_span, thread)
        self.assertTrue(any(m in adm[0].quoted_span
                            for m in StubAIClient.ADMISSION_MARKERS))

        # Gate admits everything real; decision engine says FIGHT
        verdicts = admit_all(ext.candidates, ctx)
        for v in verdicts:
            self.assertIs(v.status, GateVerdict.PASS, v.failure_reason)
        outcome = decide(dispute=dispute, playbook=rp,
                         playbook_version=self.pb.version, verdicts=verdicts,
                         now=self.sim_now, has_shipment=bool(ctx.shipments),
                         preconditions_ok=all(c.passed for c in
                                              case_preconditions(ctx)))
        self.assertIs(outcome.action, DecisionAction.FIGHT)

        # Draft cites only admitted evidence; validator is clean
        for c in ext.candidates:
            c.gate_verdict = GateVerdict.PASS
        draft = draft_representment(ext.candidates, dispute, order, self.client)
        self.assertEqual(validate_citations(draft.text, set(draft.display_map)),
                         [])
        self.assertIn(dispute.id, draft.text)

    def test_pincode_mismatch_ai_extracts_faithfully_gate_rejects(self):
        dispute = self.dev_dispute(datagen.CONFLICT_PIN)
        _, docs, ctx = self.case_materials(dispute)
        rp = self.pb.for_reason(dispute.reason_code)
        ext = extract_evidence(f"pre:{dispute.id}", dispute, docs, rp, self.client)
        verdicts = {v.evidence_key: v for v in admit_all(ext.candidates, ctx)}
        # faithful extraction of the typo'd pin -> deterministic rejection
        self.assertIn("address_match", verdicts)
        self.assertIs(verdicts["address_match"].status, GateVerdict.FAIL)
        self.assertIn("pincode mismatch", verdicts["address_match"].failure_reason)
        self.assertIs(verdicts["pod"].status, GateVerdict.PASS)

    def test_ambiguous_dispute_link_confidence_below_policy_floor(self):
        did = next(d for d in self.split["dev"]
                   if self.gt[d]["scenario"] == datagen.AMBIGUOUS)
        dispute = self.repo.get_dispute(did)
        self.assertIsNone(self.repo.get_order_by_payment(dispute.payment_id))
        # deterministic candidate search: same-amount orders (the twins)
        rows = self.repo.conn.execute(
            "SELECT id FROM orders WHERE amount = ?", (dispute.amount,)).fetchall()
        candidates = [self.repo.get_order(r["id"]) for r in rows]
        self.assertGreaterEqual(len(candidates), 2)
        res = link_order(dispute, candidates, self.client)
        self.assertLess(res.proposal.confidence, LINK_CONFIDENCE_FLOOR,
                        "twins must not be confidently linked")
        self.assertIn(res.proposal.order_id, {c.id for c in candidates})

    def test_clean_dispute_links_confidently_when_unique(self):
        dispute = self.dev_dispute(datagen.CLEAN)
        true_order = self.repo.get_order_by_payment(dispute.payment_id)
        decoy = self.repo.get_order("ord_0500")
        res = link_order(dispute, [true_order, decoy], self.client)
        self.assertEqual(res.proposal.order_id, true_order.id)
        self.assertGreaterEqual(res.proposal.confidence, LINK_CONFIDENCE_FLOOR)


class TestAblationAIOnlyVsGate(AIPipelineBase):
    """The panel story: 'AI-only' trusts the model's claim that evidence is
    verbatim; 'AI + Gate' verifies. Fabrication survives the first and cannot
    survive the second."""

    def test_fabricated_evidence_survives_ai_only_dies_at_the_gate(self):
        """Layer 2 defense: a schema-valid fabrication (real document id,
        invented quote) is exactly what 'AI-only' would believe — and exactly
        what the gate rejects with a precise reason."""
        dispute = self.dev_dispute(datagen.CLEAN)
        _, docs, ctx = self.case_materials(dispute)
        rp = self.pb.for_reason(dispute.reason_code)
        pod_doc = next(d for d in docs if d.type.value == "pod")

        fabricated = {"evidence": [{
            "key": "pod",
            "claim": "the shipment was delivered",
            "source_doc_id": pod_doc.id,                      # real doc...
            "quoted_span": "Delivered: 2099-01-01T00:00:00+00:00",  # ...invented quote
            "fields": {"awb": "DLV9999999999",
                       "delivered_at": "2099-01-01T00:00:00+00:00"}}]}
        liar = ScriptedAIClient([json.dumps(fabricated)])
        ext = extract_evidence(f"pre:{dispute.id}", dispute, docs, rp, liar)

        # AI-only view: schema-valid, plausible, would be believed
        ai_only_accepts = len(ext.candidates)

        # AI + Gate: deterministically rejected with a precise reason
        verdicts = admit_all(ext.candidates, ctx)
        gate_accepts = sum(1 for v in verdicts if v.status is GateVerdict.PASS)
        self.assertEqual(ai_only_accepts, 1)
        self.assertEqual(gate_accepts, 0)
        self.assertIs(verdicts[0].status, GateVerdict.FAIL)
        self.assertIn("not found verbatim", verdicts[0].failure_reason)

    def test_fabrication_with_no_documents_dies_even_earlier_at_schema(self):
        """Layer 1 defense (found while writing the test above): when a case
        has NO documents, a fabricated source_doc_id cannot even pass schema
        validation — the lie never reaches the gate."""
        dispute = self.dev_dispute(datagen.MISSING_POD)
        _, docs, ctx = self.case_materials(dispute)
        rp = self.pb.for_reason(dispute.reason_code)
        self.assertEqual(docs, [], "missing_pod cases have no documents")
        fabricated = json.dumps({"evidence": [{
            "key": "pod", "claim": "delivered",
            "source_doc_id": "doc_9999",
            "quoted_span": "Delivered: 2026-08-10T14:22:00+00:00",
            "fields": {"awb": "DLV9999999999",
                       "delivered_at": "2026-08-10T14:22:00+00:00"}}]})
        from app.ai.errors import LowConfidence
        with self.assertRaises(LowConfidence) as cm:
            extract_evidence(f"pre:{dispute.id}", dispute, docs, rp,
                             ScriptedAIClient([fabricated, fabricated]))
        self.assertIn("not one of the provided documents", cm.exception.reason)

    def test_faithful_extraction_admits_at_full_rate_on_clean_case(self):
        dispute = self.dev_dispute(datagen.CLEAN)
        _, docs, ctx = self.case_materials(dispute)
        rp = self.pb.for_reason(dispute.reason_code)
        ext = extract_evidence(f"pre:{dispute.id}", dispute, docs, rp,
                               self.client)
        verdicts = admit_all(ext.candidates, ctx)
        self.assertTrue(verdicts)
        self.assertTrue(all(v.status is GateVerdict.PASS for v in verdicts))
