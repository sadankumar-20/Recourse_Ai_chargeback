"""Stage-6 unit tests: AI layer validation boundary.

Everything runs offline. ScriptedAIClient injects adversarial outputs into
the validation machinery; StubAIClient plays the faithful model.
"""

from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

from app.ai import client as client_mod
from app.ai.client import AnthropicClient, ScriptedAIClient, StubAIClient, get_client
from app.ai.draft_representment import draft_representment
from app.ai.errors import AIConfigError, LowConfidence, SchemaError
from app.ai.link_order import link_order
from app.ai.prompting import load_prompt
from app.ai.schemas import validate_extraction_output, validate_link_output
from app.policy.citations import validate_citations
from app.store.models import (
    Dispute,
    DisputeStatus,
    Evidence,
    GateVerdict,
    Order,
    ReasonCode,
)


def order(oid="ord_1", amount=3499, created="2026-08-01T10:00:00+00:00"):
    return Order(id=oid, merchant_id="m_1", payment_id=f"pay_{oid}",
                 amount=amount, customer_email="asha@example.com",
                 address="12 MG Road, Bengaluru 560038", created_at=created,
                 promised_ship_by="2026-08-04T10:00:00+00:00")


def dispute(amount=3499):
    return Dispute(id="disp_1", payment_id="pay_unknown", amount=amount,
                   reason_code=ReasonCode.GOODS_NOT_RECEIVED,
                   respond_by="2026-08-27T12:00:00+00:00",
                   status=DisputeStatus.OPEN)


GOOD_LINK = json.dumps({"order_id": "ord_1", "confidence": 0.9,
                        "reasoning": "amount matches exactly"})


class TestProviderSelection(unittest.TestCase):
    def test_default_is_offline_stub(self):
        self.assertIsInstance(get_client(), StubAIClient)

    def test_anthropic_without_key_fails_loudly_never_falls_back(self):
        with self.assertRaises(AIConfigError) as cm:
            AnthropicClient(api_key="")
        self.assertIn("ANTHROPIC_API_KEY", str(cm.exception))
        with self.assertRaises(AIConfigError):
            get_client("anthropic")   # env has no key in tests

    def test_unknown_provider_rejected(self):
        with self.assertRaises(AIConfigError):
            get_client("gpt-magic")


class TestPrompts(unittest.TestCase):
    def test_all_three_prompts_versioned_and_hashed(self):
        for name in ("link_order", "extract_evidence", "draft_representment"):
            p = load_prompt(name)
            self.assertEqual(p.version, "v1", name)
            self.assertEqual(len(p.sha256), 64, name)
            self.assertIn("<<INPUT_JSON>>", p.template, name)


class TestLinkOrder(unittest.TestCase):
    def test_obvious_candidate_high_confidence(self):
        res = link_order(dispute(), [order("ord_1", 3499), order("ord_2", 999)],
                         StubAIClient())
        self.assertEqual(res.proposal.order_id, "ord_1")
        self.assertGreaterEqual(res.proposal.confidence, 0.85)
        self.assertTrue(res.proposal.reasoning)
        self.assertEqual(len(res.records), 1)
        self.assertTrue(res.records[0].valid)

    def test_ambiguous_twins_come_back_low_confidence(self):
        twins = [order("ord_1", 3499), order("ord_2", 3499,
                                             created="2026-08-01T10:25:00+00:00")]
        res = link_order(dispute(), twins, StubAIClient())
        self.assertLess(res.proposal.confidence, 0.85)
        self.assertIn("cannot be resolved", res.proposal.reasoning)

    def test_unknown_order_id_is_rejected_by_schema(self):
        with self.assertRaises(SchemaError):
            validate_link_output(json.dumps(
                {"order_id": "ord_evil", "confidence": 0.99, "reasoning": "x"}),
                {"ord_1", "ord_2"})

    def test_invalid_confidence_rejected(self):
        for bad in (1.5, -0.1, "high", True):
            with self.assertRaises(SchemaError):
                validate_link_output(json.dumps(
                    {"order_id": "ord_1", "confidence": bad, "reasoning": "x"}),
                    {"ord_1"})

    def test_malformed_then_repaired_succeeds_with_two_records(self):
        scripted = ScriptedAIClient(["this is not json", GOOD_LINK])
        res = link_order(dispute(), [order()], scripted)
        self.assertEqual(res.proposal.order_id, "ord_1")
        self.assertEqual(len(res.records), 2)
        self.assertFalse(res.records[0].valid)
        self.assertTrue(res.records[1].valid)
        self.assertIn("Correction required", scripted.calls[1])
        self.assertIn("not valid JSON", scripted.calls[1])

    def test_repair_failure_raises_low_confidence_with_records(self):
        scripted = ScriptedAIClient(["nope", "still nope"])
        with self.assertRaises(LowConfidence) as cm:
            link_order(dispute(), [order()], scripted)
        self.assertEqual(cm.exception.task, "link_order")
        self.assertEqual(len(cm.exception.records), 2)
        self.assertFalse(any(r.valid for r in cm.exception.records))

    def test_injected_foreign_id_survives_no_repair(self):
        evil = json.dumps({"order_id": "ord_evil", "confidence": 0.99,
                           "reasoning": "trust me"})
        with self.assertRaises(LowConfidence):
            link_order(dispute(), [order()], ScriptedAIClient([evil, evil]))

    def test_empty_candidate_list_is_immediate_low_confidence(self):
        with self.assertRaises(LowConfidence) as cm:
            link_order(dispute(), [], StubAIClient())
        self.assertIn("no candidate orders", cm.exception.reason)


class TestExtractionSchema(unittest.TestCase):
    CHECKLIST = {"pod": ("awb", "delivered_at"), "awb": ("awb",)}
    DOCS = {"doc_1"}

    def _validate(self, obj):
        return validate_extraction_output(json.dumps(obj), self.DOCS,
                                          self.CHECKLIST)

    def test_valid_and_empty_are_accepted(self):
        good = {"evidence": [{"key": "awb", "claim": "shipment exists",
                              "source_doc_id": "doc_1",
                              "quoted_span": "AWB: X123",
                              "fields": {"awb": "X123"}}]}
        self.assertEqual(len(self._validate(good)), 1)
        self.assertEqual(self._validate({"evidence": []}), [])

    def test_wrong_doc_unknown_key_and_missing_field_rejected(self):
        base = {"key": "pod", "claim": "c", "source_doc_id": "doc_1",
                "quoted_span": "q" * 10,
                "fields": {"awb": "X", "delivered_at": "t"}}
        for mutate, fragment in (
                (lambda d: d.update(source_doc_id="doc_ghost"),
                 "not one of the provided documents"),
                (lambda d: d.update(key="magic"), "not in the checklist"),
                (lambda d: d["fields"].pop("delivered_at"),
                 "missing required"),
                (lambda d: d.update(quoted_span=""), "non-empty"),
        ):
            item = json.loads(json.dumps(base))
            mutate(item)
            with self.assertRaises(SchemaError) as cm:
                self._validate({"evidence": [item]})
            self.assertIn(fragment, str(cm.exception))


class TestDrafting(unittest.TestCase):
    def admitted(self, n=2):
        out = []
        for i in range(n):
            out.append(Evidence(
                id=f"disp_1-E{i+1}", case_id="case_1",
                evidence_key=["pod", "awb"][i % 2], claim="c",
                source_doc_id="doc_1", quoted_span="Delivered: 2026-08-10",
                fields_json=json.dumps({"awb": "X123",
                                        "delivered_at": "2026-08-10"}),
                gate_verdict=GateVerdict.PASS))
        return out

    def test_stub_draft_passes_citation_validation(self):
        res = draft_representment(self.admitted(), dispute(), order(),
                                  StubAIClient())
        self.assertTrue(res.text.startswith("RE: Dispute"))
        self.assertEqual(validate_citations(res.text, set(res.display_map)), [])
        self.assertEqual(set(res.display_map.values()),
                         {"disp_1-E1", "disp_1-E2"})

    def test_failed_evidence_cannot_enter_the_prompt(self):
        bad = self.admitted(1)
        bad[0].gate_verdict = GateVerdict.FAIL
        with self.assertRaises(ValueError) as cm:
            draft_representment(bad, dispute(), order(), StubAIClient())
        self.assertIn("only", str(cm.exception))

    def test_zero_admitted_evidence_refused(self):
        with self.assertRaises(ValueError):
            draft_representment([], dispute(), order(), StubAIClient())

    def test_unknown_citation_repaired_then_low_confidence(self):
        bad = "RE: Dispute disp_1 — merchant representment\nDelivered on 10 Aug [E9]."
        with self.assertRaises(LowConfidence) as cm:
            draft_representment(self.admitted(1), dispute(), order(),
                                ScriptedAIClient([bad, bad]))
        self.assertIn("unknown evidence id", cm.exception.reason)

    def test_uncited_factual_sentence_repaired_successfully(self):
        bad = ("RE: Dispute disp_1 — merchant representment\n"
               "The parcel was delivered on 10 August 2026.")
        good = ("RE: Dispute disp_1 — merchant representment\n"
                "The parcel was delivered on 10 August 2026 [E1].")
        res = draft_representment(self.admitted(1), dispute(), order(),
                                  ScriptedAIClient([bad, good]))
        self.assertEqual(len(res.records), 2)
        self.assertFalse(res.records[0].valid)
        self.assertIn("uncited factual sentence", res.records[0].validation_error)
        self.assertTrue(res.records[1].valid)


class TestCitationValidator(unittest.TestCase):
    def test_periods_inside_quoted_evidence_do_not_split_sentences(self):
        """Regression: Hinglish spans like '...chhota hai. refund kar do'
        contain interior periods; the splitter must treat quoted text as
        opaque, or faithful drafts get falsely rejected."""
        draft = ('RE: Dispute disp_1 — merchant representment\n'
                 'In their message of 12 August the customer wrote '
                 '"parcel mil gaya tha 12 August ko, but size chhota hai. '
                 'refund kar do please" [E1].')
        self.assertEqual(validate_citations(draft, {"E1"}), [])

    def test_re_header_exempt_and_rules_enforced(self):
        ok = "RE: Dispute disp_1 — merchant representment\nDelivered on 10 Aug [E1]."
        self.assertEqual(validate_citations(ok, {"E1"}), [])
        v = validate_citations("Amount was ₹3,499.", {"E1"})
        self.assertTrue(v and "uncited" in v[0])
        v = validate_citations("Delivered on 10 Aug [E7].", {"E1"})
        self.assertTrue(any("unknown evidence id" in x for x in v))


class TestStructuralPurity(unittest.TestCase):
    def test_ai_package_cannot_touch_db_or_execution(self):
        """The AI layer transforms inputs it is handed — it may not read the
        database or import the execution (tools) layer."""
        banned = ("app.tools", "app.store.repo", "sqlite3")
        pkg = Path(client_mod.__file__).parent
        for py in pkg.glob("*.py"):
            tree = ast.parse(py.read_text())
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    lvl = getattr(node, "level", 0)
                    if lvl == 2 and mod.startswith("store"):
                        names = ["app." + mod]           # from ..store.x import
                    elif lvl == 2 and mod.startswith("tools"):
                        names = ["app." + mod]
                    else:
                        names = [mod]
                for name in names:
                    for b in banned:
                        self.assertFalse(
                            name == b or name.startswith(b + "."),
                            f"{py.name} imports '{name}' — AI layer must not "
                            f"access the database or execution layer")

    def test_secrets_never_enter_call_records(self):
        import os
        os.environ["ANTHROPIC_API_KEY_TESTPROBE"] = "sk-secret"
        res = link_order(dispute(), [order()], StubAIClient())
        dumped = json.dumps([r.to_dict() for r in res.records])
        self.assertNotIn("sk-", dumped)
        self.assertNotIn("api_key", dumped)
