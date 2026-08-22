"""Stage-4 tests: policy playbooks + Admissibility Gate.

The gate must reject plausible-looking AI claims whenever deterministic
verification fails. Fixtures are pure in-memory domain objects — the gate
needs no database, which is itself an architectural claim under test.
"""

from __future__ import annotations

import ast
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.policy import gate as gate_mod
from app.policy.gate import (
    GateContext,
    Verdict,
    admit,
    admit_all,
    amount_reconciles,
    verify_playbook_checks,
)
from app.policy.playbooks import PlaybookError, load_playbooks
from app.store.models import (
    Dispute,
    DisputeStatus,
    Document,
    DocumentType,
    Evidence,
    GateVerdict,
    Order,
    ReasonCode,
    Refund,
    Shipment,
)

PB = load_playbooks()
NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)

POD_TEXT = (
    "PROOF OF DELIVERY\n"
    "Courier: Delhivery\n"
    "AWB: DLV9000000001\n"
    "Delivered: 2026-08-10T14:22:00+00:00\n"
    "Receiver: Asha Rao\n"
    "Delivery OTP verified: YES\n"
    "Address: 12 MG Road, Bengaluru 560038\n"
)
THREAD_TEXT = (
    "From: asha.rao1@example.com\nDate: 2026-08-05T09:00:00+00:00\n\n"
    "order ka status kya hai?\n---\n"
    "From: support@kadaicrafts.in\nDate: 2026-08-05T15:00:00+00:00\n\n"
    "Hi, we're checking.\n---\n"
    "From: asha.rao1@example.com\nDate: 2026-08-11T10:00:00+00:00\n\n"
    "bhaiya parcel mil gaya tha 10 August ko, but size chhota hai. refund kar do please"
)


def world(*, dispute_amount=3499, reason=ReasonCode.GOODS_NOT_RECEIVED,
          refunds=(), pod_text=POD_TEXT):
    order = Order(id="ord_1", merchant_id="m_1", payment_id="pay_1",
                  amount=3499, customer_email="asha.rao1@example.com",
                  address="12 MG Road, Bengaluru 560038",
                  created_at="2026-08-01T10:00:00+00:00",
                  promised_ship_by="2026-08-04T10:00:00+00:00")
    shipment = Shipment(id="shp_1", order_id="ord_1", awb="DLV9000000001",
                        courier="Delhivery", ship_date="2026-08-03T08:00:00+00:00",
                        status="delivered", pod_doc_id="doc_pod")
    docs = {
        "doc_pod": Document(id="doc_pod", case_id=None, type=DocumentType.POD,
                            raw_text=pod_text, source="courier:DLV9000000001",
                            fetched_at="2026-08-10T15:00:00+00:00"),
        "doc_mail": Document(id="doc_mail", case_id=None, type=DocumentType.EMAIL,
                             raw_text=THREAD_TEXT,
                             source="mailbox:asha.rao1@example.com",
                             fetched_at="2026-08-23T00:00:00+00:00"),
        "doc_other": Document(id="doc_other", case_id=None, type=DocumentType.EMAIL,
                              raw_text="parcel mil gaya on some other order",
                              source="mailbox:someone.else@example.com",
                              fetched_at="2026-08-23T00:00:00+00:00"),
    }
    dispute = Dispute(id="disp_1", payment_id="pay_1", amount=dispute_amount,
                      reason_code=reason, respond_by="2026-08-27T12:00:00+00:00",
                      status=DisputeStatus.OPEN)
    return GateContext(dispute=dispute, order=order, shipments=[shipment],
                       refunds=list(refunds), documents=docs, playbooks=PB, now=NOW)


def ev(key, doc_id, span, fields, eid="E1", claim="claim text"):
    return Evidence(id=eid, case_id="case_1", evidence_key=key, claim=claim,
                    source_doc_id=doc_id, quoted_span=span,
                    fields_json=json.dumps(fields))


def good_pod():
    return ev("pod", "doc_pod", "Delivered: 2026-08-10T14:22:00+00:00",
              {"awb": "DLV9000000001", "delivered_at": "2026-08-10T14:22:00+00:00"})


class TestPlaybookLoader(unittest.TestCase):
    def test_loads_and_all_checks_resolve(self):
        verify_playbook_checks(PB)
        self.assertEqual(PB.version, "v1")
        self.assertEqual(set(PB.reason_codes),
                         {"goods_not_received", "not_as_described", "duplicate"})

    def test_unsupported_reason_code_raises(self):
        with self.assertRaises(PlaybookError) as cm:
            PB.for_reason(ReasonCode.FRAUD)
        self.assertIn("fraud", str(cm.exception))

    def test_invalid_playbooks_fail_loudly(self):
        import tempfile
        bad_docs = {
            "unknown top-level": "version: v9\nbogus: true\nreason_codes: {}",
            "missing version": "reason_codes: {}\ndefaults: {amount_tolerance_inr: 1}",
            "unknown reason": ("version: v9\ndefaults: {amount_tolerance_inr: 1}\n"
                               "reason_codes:\n  martian_fraud:\n    required: []\n"),
            "rule without checks": (
                "version: v9\ndefaults: {amount_tolerance_inr: 1}\nreason_codes:\n"
                "  duplicate:\n    required:\n"
                "      - {key: awb, description: d, required_fields: [], checks: []}\n"
                "    p_win_bands: [{min_completeness: 0.0, p_win: 0.1}]\n"),
            "bands not descending": (
                "version: v9\ndefaults: {amount_tolerance_inr: 1}\nreason_codes:\n"
                "  duplicate:\n    required:\n"
                "      - {key: awb, description: d, required_fields: [awb],\n"
                "         checks: [awb_matches_shipment]}\n"
                "    p_win_bands: [{min_completeness: 0.0, p_win: 0.1},\n"
                "                  {min_completeness: 1.0, p_win: 0.8}]\n"),
        }
        for label, text in bad_docs.items():
            with self.subTest(label):
                with tempfile.NamedTemporaryFile("w", suffix=".yaml") as f:
                    f.write(text); f.flush()
                    with self.assertRaises(PlaybookError):
                        load_playbooks(f.name)


class TestHappyPaths(unittest.TestCase):
    def test_full_goods_not_received_checklist_passes(self):
        ctx = world()
        candidates = [
            ev("awb", "doc_pod", "AWB: DLV9000000001", {"awb": "DLV9000000001"}, "E1"),
            good_pod(),
            ev("address_match", "doc_pod", "Address: 12 MG Road, Bengaluru 560038",
               {"pincode": "560038"}, "E3"),
            ev("otp_verified", "doc_pod", "Delivery OTP verified: YES", {}, "E4"),
            ev("admission_email", "doc_mail",
               "bhaiya parcel mil gaya tha 10 August ko, but size chhota hai. refund kar do please",
               {"sent_at": "2026-08-11T10:00:00+00:00"}, "E5"),
        ]
        verdicts = admit_all(candidates, ctx)
        for v in verdicts:
            self.assertIs(v.status, GateVerdict.PASS, v.failure_reason)
            self.assertIsNone(v.failure_reason)
            self.assertEqual(v.playbook_version, "v1")
            self.assertTrue(all(c.passed for c in v.checks))

    def test_verdict_structure_is_audit_ready(self):
        v = admit(good_pod(), world())
        d = v.to_dict()
        self.assertEqual(d["status"], "PASS")
        for check in ("structural", "key_known", "source_exists",
                      "source_integrity", "quote_verbatim", "required_fields",
                      "amount_reconciles", "awb_matches_shipment",
                      "delivery_after_ship"):
            self.assertIn(check, d["checks"], check)

    def test_partial_refund_reconciles_within_tolerance(self):
        refunds = [Refund(id="rf_1", order_id="ord_1", amount=1000,
                          created_at="2026-08-12T00:00:00+00:00")]
        ctx = world(dispute_amount=2499, refunds=refunds)
        self.assertTrue(amount_reconciles(ctx).passed)
        self.assertIs(admit(good_pod(), ctx).status, GateVerdict.PASS)


class TestAdversarial(unittest.TestCase):
    """A plausible-looking AI claim must still be rejected when deterministic
    verification fails."""

    def assertFailsWith(self, verdict: Verdict, fragment: str):
        self.assertIs(verdict.status, GateVerdict.FAIL)
        self.assertIn(fragment, verdict.failure_reason)

    def test_01_fabricated_quote(self):
        e = ev("admission_email", "doc_mail", "Customer acknowledged delivery.",
               {"sent_at": "2026-08-11T10:00:00+00:00"})
        self.assertFailsWith(admit(e, world()), "not found verbatim")

    def test_02_almost_correct_quote_one_char_off(self):
        e = good_pod()
        e.quoted_span = "Delivered: 2026-08-10T14:22:01+00:00"  # :01 not :00
        self.assertFailsWith(admit(e, world()), "not found verbatim")

    def test_03_wrong_awb_plausible_format(self):
        # The POD genuinely contains this AWB — but it is not the shipment's.
        pod_text = POD_TEXT.replace("DLV9000000001", "DLV9000000002")
        ctx = world(pod_text=pod_text)
        e = ev("awb", "doc_pod", "AWB: DLV9000000002", {"awb": "DLV9000000002"})
        v = admit(e, ctx)
        self.assertFailsWith(v, "AWB mismatch")
        self.assertIn("DLV9000000002", v.failure_reason)
        self.assertIn("DLV9000000001", v.failure_reason)

    def test_04_missing_document(self):
        e = ev("pod", "doc_ghost", "Delivered: 2026-08-10T14:22:00+00:00",
               {"awb": "DLV9000000001", "delivered_at": "2026-08-10T14:22:00+00:00"})
        self.assertFailsWith(admit(e, world()), "unknown source document")

    def test_05_wrong_document_type(self):
        e = ev("pod", "doc_mail", "bhaiya parcel mil gaya tha 10 August ko, but size chhota hai. refund kar do please",
               {"awb": "DLV9000000001", "delivered_at": "2026-08-10T14:22:00+00:00"})
        self.assertFailsWith(admit(e, world()), "wrong document type")

    def test_06_pod_before_shipment(self):
        pod_text = POD_TEXT.replace("2026-08-10T14:22:00+00:00",
                                    "2026-08-02T14:22:00+00:00")  # before ship 08-03
        ctx = world(pod_text=pod_text)
        e = ev("pod", "doc_pod", "Delivered: 2026-08-02T14:22:00+00:00",
               {"awb": "DLV9000000001", "delivered_at": "2026-08-02T14:22:00+00:00"})
        self.assertFailsWith(admit(e, ctx), "precedes shipment")

    def test_07_acknowledgement_before_shipment(self):
        thread = THREAD_TEXT.replace("2026-08-11T10:00:00+00:00",
                                     "2026-08-02T10:00:00+00:00")
        ctx = world()
        ctx.documents["doc_mail"].raw_text = thread
        e = ev("admission_email", "doc_mail",
               "bhaiya parcel mil gaya tha 10 August ko, but size chhota hai. refund kar do please",
               {"sent_at": "2026-08-02T10:00:00+00:00"})
        self.assertFailsWith(admit(e, ctx), "precedes shipment")

    def test_08_pincode_mismatch(self):
        pod_text = POD_TEXT.replace("560038", "560083")
        ctx = world(pod_text=pod_text)
        e = ev("address_match", "doc_pod", "Address: 12 MG Road, Bengaluru 560083",
               {"pincode": "560083"})
        v = admit(e, ctx)
        self.assertFailsWith(v, "pincode mismatch")
        self.assertIn("560083", v.failure_reason)
        self.assertIn("560038", v.failure_reason)

    def test_09_lying_about_pincode_field(self):
        # Doc has the typo'd pin; AI claims the ORDER pin in fields to sneak by.
        pod_text = POD_TEXT.replace("560038", "560083")
        ctx = world(pod_text=pod_text)
        e = ev("address_match", "doc_pod", "Address: 12 MG Road, Bengaluru 560083",
               {"pincode": "560038"})
        self.assertFailsWith(admit(e, ctx), "does not appear in source document")

    def test_10_partial_refund_arithmetic_mismatch(self):
        refunds = [Refund(id="rf_1", order_id="ord_1", amount=1000,
                          created_at="2026-08-12T00:00:00+00:00")]
        ctx = world(dispute_amount=3499, refunds=refunds)  # should be 2499
        v = admit(good_pod(), ctx)
        self.assertFailsWith(v, "amount mismatch")
        self.assertIn("2499", v.failure_reason)

    def test_11_incorrect_disputed_amount(self):
        ctx = world(dispute_amount=3400)
        self.assertFailsWith(admit(good_pod(), ctx), "amount mismatch")

    def test_12_malformed_evidence(self):
        e = good_pod()
        e.fields_json = "{not json"
        self.assertFailsWith(admit(e, world()), "malformed evidence")
        e2 = good_pod()
        e2.quoted_span = ""
        self.assertFailsWith(admit(e2, world()), "missing 'quoted_span'")

    def test_13_missing_required_field(self):
        e = ev("pod", "doc_pod", "Delivered: 2026-08-10T14:22:00+00:00",
               {"awb": "DLV9000000001"})  # delivered_at absent
        self.assertFailsWith(admit(e, world()), "missing required field")

    def test_14_unsupported_reason_code_is_config_error(self):
        ctx = world(reason=ReasonCode.FRAUD)
        with self.assertRaises(PlaybookError):
            admit(good_pod(), ctx)

    def test_15_unknown_evidence_key_for_reason(self):
        ctx = world(reason=ReasonCode.DUPLICATE)
        e = ev("admission_email", "doc_mail",
               "bhaiya parcel mil gaya tha 10 August ko, but size chhota hai. refund kar do please",
               {"sent_at": "2026-08-11T10:00:00+00:00"})
        self.assertFailsWith(admit(e, ctx), "not in the 'duplicate' checklist")

    def test_16_duplicate_evidence_in_batch(self):
        ctx = world()
        a, b = good_pod(), good_pod()
        b.id = "E2"
        verdicts = admit_all([a, b], ctx)
        self.assertIs(verdicts[0].status, GateVerdict.PASS)
        self.assertFailsWith(verdicts[1], "duplicate evidence")
        self.assertIn("E1", verdicts[1].failure_reason)

    def test_17_evidence_from_another_customers_mailbox(self):
        e = ev("admission_email", "doc_other", "parcel mil gaya on some other order",
               {"sent_at": "2026-08-11T10:00:00+00:00"})
        self.assertFailsWith(admit(e, world()), "not linked to this case's order")

    def test_18_conflicting_source_documents_resolved_by_record(self):
        # Two AWB candidates conflict; the shipment record decides which lives.
        ctx = world()
        ctx.documents["doc_pod2"] = Document(
            id="doc_pod2", case_id=None, type=DocumentType.POD,
            raw_text=POD_TEXT.replace("DLV9000000001", "DLV9000000002"),
            source="courier:DLV9000000002", fetched_at="2026-08-10T15:00:00+00:00")
        a = ev("awb", "doc_pod", "AWB: DLV9000000001", {"awb": "DLV9000000001"}, "E1")
        b = ev("awb", "doc_pod2", "AWB: DLV9000000002", {"awb": "DLV9000000002"}, "E2")
        va, vb = admit_all([a, b], ctx)
        self.assertIs(va.status, GateVerdict.PASS)
        self.assertIs(vb.status, GateVerdict.FAIL)  # doc not linked to this order
        self.assertIsNotNone(vb.failure_reason)

    def test_19_failed_evidence_is_preserved_not_deleted(self):
        v = admit(ev("awb", "doc_pod", "AWB: DLV9000000001", {"awb": "WRONG"}), world())
        self.assertIs(v.status, GateVerdict.FAIL)
        self.assertEqual(v.evidence_id, "E1")          # identity retained
        self.assertTrue(any(not c.passed for c in v.checks))
        self.assertTrue(any(c.passed for c in v.checks))  # partial trail kept


class TestPolicyPurity(unittest.TestCase):
    def test_policy_package_has_zero_llm_or_network_imports(self):
        """The Admissibility Gate must be deterministic: no AI, no network."""
        banned = ("app.ai", "anthropic", "openai", "requests", "httpx",
                  "urllib", "socket", "http")
        pkg = Path(gate_mod.__file__).parent
        for py in pkg.glob("*.py"):
            tree = ast.parse(py.read_text())
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                for name in names:
                    for b in banned:
                        self.assertFalse(
                            name == b or name.startswith(b + "."),
                            f"{py.name} imports '{name}' — policy layer must "
                            f"stay deterministic and offline")
