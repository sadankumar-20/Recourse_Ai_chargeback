"""Stage-7 tests: audit hash chain — determinism, verification, tamper proof.

The point under test: the audit trail is not a UI log. Any modification,
deletion, or reordering of stored entries is detectable, with a precise
report of where the chain broke.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.audit.chain import (
    GENESIS,
    canonical_json,
    compute_entry_hash,
    redact,
    verify_audit_chain,
)
from app.store.models import Case, Dispute, Merchant, Order, ReasonCode
from app.store.repo import Repository


class ChainTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(Path(self.tmp.name) / "t.db")
        self.addCleanup(self.repo.close)
        self.addCleanup(self.tmp.cleanup)
        self.repo.add_merchant(Merchant("m_1", "Shop", 2000, 10000))
        self.repo.add_order(Order("ord_1", "m_1", "pay_1", 3499, "a@b.c",
                                  "addr 560001", "2026-08-01T00:00:00+00:00",
                                  "2026-08-04T00:00:00+00:00"))
        self.repo.add_dispute(Dispute("disp_1", "pay_1", 3499,
                                      ReasonCode.GOODS_NOT_RECEIVED,
                                      "2026-08-27T00:00:00+00:00"))
        self.repo.add_case(Case("case_1", "disp_1"))

    def chain_of(self, n=5):
        for i in range(n):
            self.repo.append_audit("case_1", f"step_{i}", {"i": i, "x": "y"})
        return self.repo.read_audit("case_1")


class TestChainConstruction(ChainTestBase):
    def test_genesis_and_linking(self):
        entries = self.chain_of(3)
        self.assertEqual(entries[0].prev_hash, GENESIS)
        for prev, cur in zip(entries, entries[1:]):
            self.assertEqual(cur.prev_hash, prev.entry_hash)

    def test_hash_is_deterministic_and_recomputable(self):
        e = self.chain_of(1)[0]
        recomputed = compute_entry_hash(e.prev_hash, e.case_id, e.step,
                                        e.payload_json, e.at)
        self.assertEqual(recomputed, e.entry_hash)

    def test_canonicalization_is_key_order_independent(self):
        self.assertEqual(canonical_json({"b": 1, "a": {"d": 2, "c": 3}}),
                         canonical_json({"a": {"c": 3, "d": 2}, "b": 1}))

    def test_redaction_before_hashing(self):
        self.repo.append_audit("case_1", "call", {
            "api_key": "sk-secret", "nested": {"Authorization": "Bearer t"},
            "safe": "value"})
        e = self.repo.read_audit("case_1")[0]
        self.assertNotIn("sk-secret", e.payload_json)
        self.assertNotIn("Bearer t", e.payload_json)
        # stored payload IS the hashed payload — chain verifies over redacted bytes
        self.assertTrue(verify_audit_chain(self.repo, "case_1").valid)

    def test_redact_helper_handles_lists(self):
        out = redact({"items": [{"token": "x", "ok": 1}]})
        self.assertEqual(out["items"][0]["token"], "[REDACTED]")
        self.assertEqual(out["items"][0]["ok"], 1)

    def test_non_dict_payload_rejected(self):
        with self.assertRaises(TypeError):
            self.repo.append_audit("case_1", "bad", "just a string")

    def test_valid_chain_verifies_with_report(self):
        self.chain_of(5)
        report = verify_audit_chain(self.repo, "case_1")
        self.assertTrue(report.valid)
        self.assertEqual(report.entries, 5)
        self.assertIn("Audit chain VALID", report.to_text())

    def test_empty_chain_is_valid(self):
        self.assertTrue(verify_audit_chain(self.repo, "case_1").valid)


class TestTamperDetection(ChainTestBase):
    """Tampering happens through raw SQL — the repository exposes no mutation
    path (Stage 2 guarantee), so an attacker must go around it, and the chain
    catches them anyway."""

    def test_modified_payload_detected_at_exact_entry(self):
        entries = self.chain_of(5)
        self.assertTrue(verify_audit_chain(self.repo, "case_1").valid)
        victim = entries[2]
        tampered = json.loads(victim.payload_json)
        tampered["x"] = "FORGED"
        with self.repo.conn:
            self.repo.conn.execute(
                "UPDATE audit_log SET payload_json = ? WHERE seq = ?",
                (json.dumps(tampered, sort_keys=True, separators=(",", ":")),
                 victim.seq))
        report = verify_audit_chain(self.repo, "case_1")
        self.assertFalse(report.valid)
        self.assertEqual(report.broken_at_seq, victim.seq)
        self.assertIn("entry_hash mismatch", report.reason)
        self.assertIn("Broken at entry", report.to_text())

    def test_modified_prev_hash_detected(self):
        entries = self.chain_of(4)
        with self.repo.conn:
            self.repo.conn.execute(
                "UPDATE audit_log SET prev_hash = ? WHERE seq = ?",
                ("f" * 64, entries[1].seq))
        report = verify_audit_chain(self.repo, "case_1")
        self.assertFalse(report.valid)
        self.assertEqual(report.broken_at_seq, entries[1].seq)
        self.assertIn("chain link broken", report.reason)

    def test_deleted_middle_entry_detected(self):
        entries = self.chain_of(5)
        with self.repo.conn:
            self.repo.conn.execute("DELETE FROM audit_log WHERE seq = ?",
                                   (entries[2].seq,))
        report = verify_audit_chain(self.repo, "case_1")
        self.assertFalse(report.valid)
        self.assertEqual(report.broken_at_seq, entries[3].seq)
        self.assertIn("deleted, reordered", report.reason)

    def test_reordered_entries_detected(self):
        entries = self.chain_of(5)
        a, b = entries[1], entries[3]
        with self.repo.conn:                       # swap two rows' contents
            for src, dst in ((a, b), (b, a)):
                self.repo.conn.execute(
                    "UPDATE audit_log SET step=?, payload_json=?, at=?, "
                    "prev_hash=?, entry_hash=? WHERE seq=?",
                    (src.step, src.payload_json, src.at, src.prev_hash,
                     src.entry_hash, dst.seq))
        report = verify_audit_chain(self.repo, "case_1")
        self.assertFalse(report.valid)
        self.assertIsNotNone(report.broken_at_seq)

    def test_corrupted_entry_hash_detected(self):
        entries = self.chain_of(3)
        with self.repo.conn:
            self.repo.conn.execute(
                "UPDATE audit_log SET entry_hash = ? WHERE seq = ?",
                ("a" * 64, entries[0].seq))
        report = verify_audit_chain(self.repo, "case_1")
        self.assertFalse(report.valid)
        self.assertEqual(report.broken_at_seq, entries[0].seq)

    def test_chains_are_isolated_per_case(self):
        self.repo.add_dispute(Dispute("disp_2", "pay_1x", 100,
                                      ReasonCode.DUPLICATE,
                                      "2026-08-27T00:00:00+00:00"))
        self.repo.add_case(Case("case_2", "disp_2"))
        self.chain_of(3)
        self.repo.append_audit("case_2", "intake", {"ok": True})
        entries = self.repo.read_audit("case_1")
        with self.repo.conn:
            self.repo.conn.execute("DELETE FROM audit_log WHERE seq = ?",
                                   (entries[1].seq,))
        self.assertFalse(verify_audit_chain(self.repo, "case_1").valid)
        self.assertTrue(verify_audit_chain(self.repo, "case_2").valid)
