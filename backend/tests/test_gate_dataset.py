"""Stage-4 dataset tests: the Admissibility Gate against the Stage-3 world.

No artificial clean fixtures here — the gate runs over the actually generated
dataset (seed 42, tmp dir), with extraction done by the deterministic oracle.
Every assertion ties a planted imperfection to a specific gate behavior.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from app import datagen
from app.datagen import generate
from app.evals.gate_report import run_gate_report
from app.evals.oracle import build_candidates
from app.policy.gate import GateContext, admit_all
from app.policy.playbooks import PlaybookError, load_playbooks
from app.store.models import GateVerdict
from app.store.repo import Repository


class GateDatasetBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name)
        generate(seed=42, out_dir=cls.out)
        cls.gt = json.loads((cls.out / "ground_truth.json").read_text())["labels"]
        cls.split = json.loads((cls.out / "split.json").read_text())
        cls.sim_now = datetime.fromisoformat(cls.split["sim_now"])
        cls.pb = load_playbooks()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.repo = Repository(self.out / "dataset.db")
        self.addCleanup(self.repo.close)

    def ctx_and_verdicts(self, dispute_id: str):
        dispute = self.repo.get_dispute(dispute_id)
        rp = self.pb.for_reason(dispute.reason_code)
        candidates, notes = build_candidates(self.repo, dispute,
                                             checklist_keys=tuple(rp.rules))
        order = self.repo.get_order_by_payment(dispute.payment_id)
        docs = {}
        for ship in self.repo.list_shipments_for_order(order.id):
            if ship.pod_doc_id:
                docs[ship.pod_doc_id] = self.repo.get_document(ship.pod_doc_id)
        for row in self.repo.conn.execute(
                "SELECT id FROM documents WHERE source = ?",
                (f"mailbox:{order.customer_email}",)).fetchall():
            docs[row["id"]] = self.repo.get_document(row["id"])
        ctx = GateContext(dispute=dispute, order=order,
                          shipments=self.repo.list_shipments_for_order(order.id),
                          refunds=self.repo.list_refunds_for_order(order.id),
                          documents=docs, playbooks=self.pb, now=self.sim_now)
        return ctx, admit_all(candidates, ctx), candidates, notes

    def dev_ids(self, scenario: str, mvp_only: bool = True) -> list[str]:
        ids = []
        for did in self.split["dev"]:
            if self.gt[did]["scenario"] != scenario:
                continue
            if mvp_only:
                reason = self.repo.get_dispute(did).reason_code.value
                if reason not in self.pb.reason_codes:
                    continue
            ids.append(did)
        return ids


class TestGateOnDataset(GateDatasetBase):
    def test_pincode_mismatch_scenarios_are_caught(self):
        ids = self.dev_ids(datagen.CONFLICT_PIN)
        self.assertTrue(ids, "dev split must contain conflicting_pincode cases")
        for did in ids:
            _, verdicts, _, _ = self.ctx_and_verdicts(did)
            addr = [v for v in verdicts if v.evidence_key == "address_match"]
            self.assertTrue(addr, did)
            self.assertIs(addr[0].status, GateVerdict.FAIL, did)
            self.assertIn("pincode mismatch", addr[0].failure_reason, did)
            # other evidence on the same case still admits — precise, not blanket
            others = [v for v in verdicts if v.evidence_key in ("awb", "pod")]
            for v in others:
                self.assertIs(v.status, GateVerdict.PASS, (did, v.failure_reason))

    def test_missing_pod_scenarios_yield_checklist_gaps_not_fabrication(self):
        ids = self.dev_ids(datagen.MISSING_POD)
        self.assertTrue(ids)
        for did in ids:
            _, verdicts, candidates, notes = self.ctx_and_verdicts(did)
            keys = {c.evidence_key for c in candidates}
            self.assertNotIn("pod", keys, did)      # nothing invented
            self.assertNotIn("awb", keys, did)
            self.assertTrue(any("no POD" in n for n in notes), did)

    def test_clean_and_hinglish_cases_fully_admit(self):
        for scenario in (datagen.CLEAN, datagen.HINGLISH):
            for did in self.dev_ids(scenario):
                _, verdicts, candidates, _ = self.ctx_and_verdicts(did)
                self.assertTrue(candidates, did)
                for v in verdicts:
                    self.assertIs(v.status, GateVerdict.PASS,
                                  (did, v.evidence_key, v.failure_reason))

    def test_hinglish_admissions_are_extracted_and_admitted(self):
        found = 0
        for did in self.dev_ids(datagen.HINGLISH):
            _, verdicts, _, _ = self.ctx_and_verdicts(did)
            adm = [v for v in verdicts if v.evidence_key == "admission_email"]
            if adm:
                found += 1
                self.assertIs(adm[0].status, GateVerdict.PASS, did)
        self.assertGreater(found, 0, "at least one admission must be admitted")

    def test_fabricated_quote_on_real_data_is_rejected(self):
        did = self.dev_ids(datagen.CLEAN)[0]
        ctx, _, candidates, _ = self.ctx_and_verdicts(did)
        victim = candidates[0]
        victim.quoted_span = victim.quoted_span[:-1] + (
            "0" if not victim.quoted_span.endswith("0") else "1")
        v = admit_all([victim], ctx)[0]
        self.assertIs(v.status, GateVerdict.FAIL)
        self.assertIn("not found verbatim", v.failure_reason)

    def test_deferred_reason_codes_raise_playbook_error(self):
        deferred = [did for did in self.split["dev"]
                    if self.repo.get_dispute(did).reason_code.value
                    not in self.pb.reason_codes]
        self.assertTrue(deferred, "dataset must contain deferred reason codes")
        with self.assertRaises(PlaybookError):
            self.pb.for_reason(self.repo.get_dispute(deferred[0]).reason_code)

    def test_report_is_derived_and_consistent(self):
        r = run_gate_report(self.out, "dev")
        self.assertEqual(r["disputes_in_split"], 80)
        self.assertEqual(r["evidence_checked"], r["passed"] + r["failed"])
        # every failure on perfect extraction must be a planted imperfection
        self.assertEqual(set(r["failure_reasons"]), {"pincode mismatch"})
        n_pin_dev = len(self.dev_ids(datagen.CONFLICT_PIN))
        self.assertEqual(r["failure_reasons"]["pincode mismatch"], n_pin_dev)
        self.assertGreater(r["passed"], 100)
