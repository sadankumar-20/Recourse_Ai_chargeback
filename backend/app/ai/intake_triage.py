"""Intake triage (R4): interpret the merchant's free-text dispute report.

Output is UNTRUSTED AI-derived structure — a reason-code guess, extracted
references, the customer's claim, what's missing, and a confidence. It never
overwrites the original text (the coordination layer stores that verbatim as
source material), and it never determines FIGHT/ACCEPT/ESCALATE.

Anthropic path: one structured-JSON call through the hardened machinery.
Offline path: deterministic keyword rules — reproducible, network-free.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .prompting import call_with_repair, load_prompt
from .schemas import SchemaError, strip_json

REASONS = ("goods_not_received", "not_as_described", "duplicate",
           "credit_not_processed", "cancelled_recurring", "fraud")
_PAY_RE = re.compile(r"\b(pay_[0-9A-Za-z]+)\b")
_ORDER_RE = re.compile(r"(?:order\s*#?\s*|ord[_ ])(\w+)", re.I)
_RULES = (
    ("duplicate", ("charged twice", "double charge", "duplicate", "two times")),
    ("not_as_described", ("not as described", "damaged", "wrong item",
                          "different from", "defective", "broken")),
    ("credit_not_processed", ("refund not", "credit not", "no refund")),
    ("cancelled_recurring", ("cancelled subscription", "recurring",
                             "cancel my subscription")),
    ("goods_not_received", ("never received", "not received", "didn't arrive",
                            "did not arrive", "never delivered",
                            "not delivered", "no delivery", "never got")),
)


@dataclass(frozen=True)
class Triage:
    reason_code: str
    confidence: float
    customer_claim: str
    payment_id: str | None = None
    order_ref: str | None = None
    missing: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"reason_code": self.reason_code, "confidence": self.confidence,
                "customer_claim": self.customer_claim,
                "payment_id": self.payment_id, "order_ref": self.order_ref,
                "missing": self.missing}


def validate_triage_output(text: str) -> dict:
    obj = json.loads(strip_json(text))
    allowed = {"reason_code", "confidence", "customer_claim", "payment_id",
               "order_ref", "missing"}
    if not isinstance(obj, dict) or not set(obj) <= allowed \
            or not {"reason_code", "confidence", "customer_claim"} <= set(obj):
        raise SchemaError(f"triage keys must be within {sorted(allowed)}")
    if obj["reason_code"] not in REASONS:
        raise SchemaError(f"reason_code must be one of {REASONS}")
    if not isinstance(obj["confidence"], (int, float)) \
            or not 0 <= obj["confidence"] <= 1:
        raise SchemaError("confidence must be a number in [0, 1]")
    return obj


def triage_intake(text: str, client) -> Triage:
    if getattr(client, "provider", "stub") == "anthropic":
        prompt = load_prompt("intake_triage")
        obj, _ = call_with_repair(client, prompt, {"merchant_report": text},
                                  validate_triage_output, task="intake_triage")
        return Triage(reason_code=obj["reason_code"],
                      confidence=float(obj["confidence"]),
                      customer_claim=obj["customer_claim"],
                      payment_id=obj.get("payment_id"),
                      order_ref=obj.get("order_ref"),
                      missing=obj.get("missing") or [])
    return _deterministic_triage(text)


def _deterministic_triage(text: str) -> Triage:
    low = text.lower()
    reason, hits = "goods_not_received", 0
    for code, needles in _RULES:
        n = sum(1 for k in needles if k in low)
        if n > hits:
            reason, hits = code, n
    pay = _PAY_RE.search(text)
    order = _ORDER_RE.search(text)
    claim = text.strip().split("\n")[0][:200]
    missing = []
    if not pay and not order:
        missing.append("order or payment reference")
    return Triage(reason_code=reason,
                  confidence=round(min(0.9, 0.4 + 0.2 * hits), 2),
                  customer_claim=claim,
                  payment_id=pay.group(1) if pay else None,
                  order_ref=order.group(1) if order else None,
                  missing=missing)
