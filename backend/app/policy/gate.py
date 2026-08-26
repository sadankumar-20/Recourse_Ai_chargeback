"""The Admissibility Gate (spec §8 step 7, §20, §30.3).

The AI may PROPOSE evidence; only this module may ADMIT it. Every check here
is deterministic string/date/arithmetic verification — zero LLM imports, no
semantic similarity, no confidence overrides. A failed gate cannot be
overridden by anything upstream.

The core anti-fabrication pattern, applied uniformly:

    every claimed field must (1) appear VERBATIM in the source document, and
    (2) match the system of record (shipment, order, refunds).

So an AI that invents an AWB fails (1), and an AI that quotes a real document
which contradicts the shipment record fails (2). Hallucinated evidence is
structurally impossible to admit, not merely discouraged.

Verdicts are structured and preserved: failed evidence is kept and marked
inadmissible with a precise reason — the dashboard shows PASS (verified) and
FAIL (exact reason) side by side.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime

from ..store.models import (
    Case,
    Dispute,
    Document,
    DocumentType,
    Evidence,
    GateVerdict,
    Order,
    Refund,
    Shipment,
)
from .playbooks import EvidenceRule, PlaybookSet, ReasonPlaybook

MIN_QUOTE_LEN = 8          # a 3-char "quote" proves nothing
_PIN_RE = re.compile(r"\b\d{6}\b")


# --- structured results ----------------------------------------------------------

@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str | None = None

    @property
    def status(self) -> str:
        return "PASS" if self.passed else "FAIL"


@dataclass(frozen=True)
class Verdict:
    status: GateVerdict
    evidence_id: str
    evidence_key: str
    playbook_version: str
    checks: tuple[CheckResult, ...]
    failure_reason: str | None      # first failure, precise; None on PASS

    def to_dict(self) -> dict:
        return {
            "status": self.status.value,
            "evidence_id": self.evidence_id,
            "evidence_key": self.evidence_key,
            "playbook_version": self.playbook_version,
            "checks": {c.name: {"status": c.status, "detail": c.detail}
                       for c in self.checks},
            "failure_reason": self.failure_reason,
        }


@dataclass
class GateContext:
    """Everything the gate may consult. All reads, no writes."""
    dispute: Dispute
    order: Order
    shipments: list[Shipment]
    refunds: list[Refund]
    documents: dict[str, Document]      # doc_id -> Document
    playbooks: PlaybookSet
    now: datetime
    case: Case | None = None

    @property
    def shipment(self) -> Shipment | None:
        return self.shipments[0] if self.shipments else None


class _Fail(Exception):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def _ts(value: str, what: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise _Fail(f"unparseable timestamp for {what}: {value!r}")


def _order_pincode(order: Order) -> str | None:
    m = _PIN_RE.findall(order.address)
    return m[-1] if m else None


# --- named checks (registry; referenced by name from playbooks.yaml) ---------------

def _doc_is_pod(ev, fields, doc, ctx):
    if doc.type is not DocumentType.POD:
        raise _Fail(f"wrong document type: '{ev.evidence_key}' requires a POD, "
                    f"but {doc.id} is type '{doc.type.value}'")


def _doc_is_email(ev, fields, doc, ctx):
    if doc.type is not DocumentType.EMAIL:
        raise _Fail(f"wrong document type: '{ev.evidence_key}' requires an email, "
                    f"but {doc.id} is type '{doc.type.value}'")


def _awb_matches_shipment(ev, fields, doc, ctx):
    claimed = str(fields["awb"])
    if claimed not in doc.raw_text:
        raise _Fail(f"claimed AWB {claimed} does not appear in source document {doc.id}")
    ship = ctx.shipment
    if ship is None:
        raise _Fail("no shipment exists on the linked order to match the AWB against")
    if claimed != ship.awb:
        raise _Fail(f"AWB mismatch: evidence AWB {claimed} does not match "
                    f"shipment AWB {ship.awb}")


def _delivery_after_ship(ev, fields, doc, ctx):
    claimed = str(fields["delivered_at"])
    if claimed not in doc.raw_text:
        raise _Fail(f"claimed delivery time {claimed} does not appear in "
                    f"source document {doc.id}")
    delivered = _ts(claimed, "delivered_at")
    ship = ctx.shipment
    if ship is None:
        raise _Fail("no shipment exists on the linked order")
    shipped = _ts(ship.ship_date, "shipment.ship_date")
    ordered = _ts(ctx.order.created_at, "order.created_at")
    if delivered < shipped:
        raise _Fail(f"timestamp incoherent: delivery {claimed} precedes "
                    f"shipment {ship.ship_date}")
    if shipped < ordered:
        raise _Fail(f"timestamp incoherent: shipment {ship.ship_date} precedes "
                    f"order creation {ctx.order.created_at}")


def _pincode_matches_order(ev, fields, doc, ctx):
    claimed = str(fields["pincode"])
    if claimed not in doc.raw_text:
        raise _Fail(f"claimed pincode {claimed} does not appear in "
                    f"source document {doc.id}")
    order_pin = _order_pincode(ctx.order)
    if order_pin is None:
        raise _Fail(f"order {ctx.order.id} address has no parseable pincode")
    if claimed != order_pin:
        raise _Fail(f"pincode mismatch: POD shows delivery to {claimed}, "
                    f"order address is {order_pin}")


def _otp_verified_yes(ev, fields, doc, ctx):
    if "Delivery OTP verified: YES" not in doc.raw_text:
        raise _Fail(f"document {doc.id} does not record a verified delivery OTP")


def _sent_at_in_doc(ev, fields, doc, ctx):
    claimed = str(fields["sent_at"])
    if claimed not in doc.raw_text:
        raise _Fail(f"claimed message timestamp {claimed} does not appear in "
                    f"source document {doc.id}")
    _ts(claimed, "sent_at")


def _sent_after_ship(ev, fields, doc, ctx):
    sent = _ts(str(fields["sent_at"]), "sent_at")
    ship = ctx.shipment
    if ship is None:
        raise _Fail("no shipment exists; an acknowledgement cannot corroborate delivery")
    shipped = _ts(ship.ship_date, "shipment.ship_date")
    if sent < shipped:
        raise _Fail(f"timestamp incoherent: acknowledgement at {fields['sent_at']} "
                    f"precedes shipment on {ship.ship_date}")


CHECKS = {
    "doc_is_pod": _doc_is_pod,
    "doc_is_email": _doc_is_email,
    "awb_matches_shipment": _awb_matches_shipment,
    "delivery_after_ship": _delivery_after_ship,
    "pincode_matches_order": _pincode_matches_order,
    "otp_verified_yes": _otp_verified_yes,
    "sent_at_in_doc": _sent_at_in_doc,
    "sent_after_ship": _sent_after_ship,
}


def verify_playbook_checks(playbooks: PlaybookSet) -> None:
    """Every check name referenced in YAML must resolve to a registered check."""
    from .playbooks import PlaybookError
    for code, rp in playbooks.reason_codes.items():
        for rule in rp.rules.values():
            for name in rule.checks:
                if name not in CHECKS:
                    raise PlaybookError(
                        f"reason_codes.{code}.{rule.key}: unknown check '{name}' "
                        f"(registered: {sorted(CHECKS)})")


# --- case preconditions ------------------------------------------------------------

def amount_reconciles(ctx: GateContext) -> CheckResult:
    """|disputed - (order - refunds)| <= tolerance. Integer rupee arithmetic."""
    refunds = sum(r.amount for r in ctx.refunds)
    expected = ctx.order.amount - refunds
    tol = ctx.playbooks.amount_tolerance_inr
    if abs(ctx.dispute.amount - expected) <= tol:
        return CheckResult("amount_reconciles", True)
    return CheckResult(
        "amount_reconciles", False,
        f"amount mismatch: disputed \u20b9{ctx.dispute.amount} != order "
        f"\u20b9{ctx.order.amount} - refunds \u20b9{refunds} = \u20b9{expected} "
        f"(tolerance \u20b9{tol})")


def case_preconditions(ctx: GateContext) -> list[CheckResult]:
    return [amount_reconciles(ctx)]


# --- the gate ----------------------------------------------------------------------

def _doc_belongs_to_case(doc: Document, ctx: GateContext) -> str | None:
    """Evidence source integrity: the document must be reachable from THIS
    case's order — the shipment's POD, the customer's own mailbox, the
    courier's tracking channel, or (R4) a merchant upload explicitly
    attached to THIS case. Linkage only scopes; every content check
    (verbatim quote, AWB match, pincode, amounts) still applies to uploads."""
    if ctx.case is not None and doc.case_id not in (None, ctx.case.id):
        return f"document {doc.id} is attached to a different case ({doc.case_id})"
    if doc.provenance in ("user_upload", "vision_transcribed") \
            and ctx.case is not None and doc.case_id == ctx.case.id:
        return None
    for ship in ctx.shipments:
        if doc.id == ship.pod_doc_id or doc.source == f"courier:{ship.awb}":
            return None
    if doc.source == f"mailbox:{ctx.order.customer_email}":
        return None
    return (f"document {doc.id} (source '{doc.source}') is not linked to this "
            f"case's order {ctx.order.id} — not its shipment POD and not the "
            f"customer's mailbox")


def admit(evidence: Evidence, ctx: GateContext) -> Verdict:
    """Deterministically verify one candidate. Never raises for evidence-level
    problems (returns FAIL with a precise reason); raises PlaybookError only
    for configuration-level problems (unsupported reason code, unknown check).
    """
    rp: ReasonPlaybook = ctx.playbooks.for_reason(ctx.dispute.reason_code)
    results: list[CheckResult] = []

    def fail(name: str, detail: str) -> Verdict:
        results.append(CheckResult(name, False, detail))
        return _finalize(evidence, ctx, results)

    # 1. structural sanity — malformed candidates are rejected, not repaired
    for attr in ("id", "evidence_key", "claim", "source_doc_id", "quoted_span"):
        if not str(getattr(evidence, attr, "") or "").strip():
            return fail("structural", f"malformed evidence: missing '{attr}'")
    try:
        fields = json.loads(evidence.fields_json)
        if not isinstance(fields, dict):
            raise ValueError("fields_json must encode an object")
    except (TypeError, ValueError) as e:
        return fail("structural", f"malformed evidence: fields_json invalid ({e})")
    results.append(CheckResult("structural", True))

    # 2. the key must exist in this reason code's checklist
    rule = rp.rules.get(evidence.evidence_key)
    if rule is None:
        return fail("key_known",
                    f"evidence key '{evidence.evidence_key}' is not in the "
                    f"'{rp.reason_code}' checklist (allowed: {sorted(rp.rules)})")
    results.append(CheckResult("key_known", True))

    # 3. source document must exist and belong to this case's world
    doc = ctx.documents.get(evidence.source_doc_id)
    if doc is None:
        return fail("source_exists",
                    f"unknown source document '{evidence.source_doc_id}'")
    results.append(CheckResult("source_exists", True))
    integrity_problem = _doc_belongs_to_case(doc, ctx)
    if integrity_problem:
        return fail("source_integrity", integrity_problem)
    results.append(CheckResult("source_integrity", True))

    # 4. the quoted span must exist VERBATIM — no similarity, no second LLM
    if len(evidence.quoted_span.strip()) < MIN_QUOTE_LEN:
        return fail("quote_verbatim",
                    f"quoted span too short to be meaningful "
                    f"(<{MIN_QUOTE_LEN} chars): {evidence.quoted_span!r}")
    if evidence.quoted_span not in doc.raw_text:
        return fail("quote_verbatim",
                    f"quoted span not found verbatim in document {doc.id}: "
                    f"{evidence.quoted_span[:80]!r}")
    results.append(CheckResult("quote_verbatim", True))

    # 5. required fields for this key
    missing = [f for f in rule.required_fields if f not in fields]
    if missing:
        return fail("required_fields",
                    f"evidence '{rule.key}' missing required field(s): {missing}")
    results.append(CheckResult("required_fields", True))

    # 6. amount reconciliation — money coherence guards every admission
    amt = amount_reconciles(ctx)
    results.append(amt)
    if not amt.passed:
        return _finalize(evidence, ctx, results)

    # 7. the key's named checks from the playbook (all run; all reported)
    from .playbooks import PlaybookError
    for name in rule.checks:
        fn = CHECKS.get(name)
        if fn is None:
            raise PlaybookError(f"unknown check '{name}' referenced by playbook")
        try:
            fn(evidence, fields, doc, ctx)
            results.append(CheckResult(name, True))
        except _Fail as e:
            results.append(CheckResult(name, False, e.detail))

    return _finalize(evidence, ctx, results)


def _finalize(evidence: Evidence, ctx: GateContext,
              results: list[CheckResult]) -> Verdict:
    failures = [r for r in results if not r.passed]
    return Verdict(
        status=GateVerdict.FAIL if failures else GateVerdict.PASS,
        evidence_id=evidence.id,
        evidence_key=evidence.evidence_key,
        playbook_version=ctx.playbooks.version,
        checks=tuple(results),
        failure_reason=failures[0].detail if failures else None,
    )


def admit_all(candidates: list[Evidence], ctx: GateContext) -> list[Verdict]:
    """Gate a batch. Adds cross-candidate duplicate detection: a second
    candidate with the same (key, source document, quoted span) is inadmissible
    — one fact may not be counted twice."""
    seen: dict[tuple, str] = {}
    verdicts: list[Verdict] = []
    for ev in candidates:
        sig = (ev.evidence_key, ev.source_doc_id, ev.quoted_span)
        if sig in seen:
            verdicts.append(Verdict(
                status=GateVerdict.FAIL, evidence_id=ev.id,
                evidence_key=ev.evidence_key,
                playbook_version=ctx.playbooks.version,
                checks=(CheckResult("duplicate", False,
                                    f"duplicate evidence: same key/source/span "
                                    f"as {seen[sig]}"),),
                failure_reason=f"duplicate evidence: same key/source/span as {seen[sig]}",
            ))
            continue
        seen[sig] = ev.id
        verdicts.append(admit(ev, ctx))
    return verdicts
