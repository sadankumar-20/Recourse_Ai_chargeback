"""Oracle extractor: deterministic, regex-based candidate builder.

This is NOT the AI. It simulates a perfect extractor over the synthetic
corpus so the Admissibility Gate can be validated on Stage-3 data before any
LLM exists, and later serves as the upper-baseline in evaluation ("how would
the gate behave under flawless extraction?").

Deliberately honest: it extracts what the DOCUMENTS say (e.g. the typo'd
pincode on a mismatched POD), never what the system of record says — exactly
like a faithful extractor would. It resolves the order strictly by
payment_id and never reads ground-truth labels.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from ..store.models import Dispute, Evidence
from ..store.repo import Repository

_AWB_RE = re.compile(r"^AWB: (.+)$", re.M)
_DELIVERED_RE = re.compile(r"^Delivered: (.+)$", re.M)
_ADDRESS_RE = re.compile(r"^Address: (.+)$", re.M)
_PIN_RE = re.compile(r"\b\d{6}\b")
_OTP_LINE = "Delivery OTP verified: YES"

# Substrings that mark a customer's own acknowledgement of receipt. Mirrors
# the generator's canonical markers; the ORACLE may know them (it is a test
# harness over synthetic data) — the future AI will not.
ADMISSION_MARKERS = ["mil gaya", "receive ho gaya", "delivery ho gayi", "aa gaya"]


def build_candidates(repo: Repository, dispute: Dispute,
                     checklist_keys: tuple[str, ...] | None = None
                     ) -> tuple[list[Evidence], list[str]]:
    """Return (candidates, notes). Notes explain gaps a human would care
    about: unresolvable order, missing POD, no admission found.

    Extraction is checklist-driven (spec §8 step 6): when ``checklist_keys``
    is given, only those evidence keys are proposed — a real extractor is
    handed the reason code's checklist, so proposing off-checklist keys would
    be a harness artifact, not realistic behavior."""
    notes: list[str] = []
    order = repo.get_order_by_payment(dispute.payment_id)
    if order is None:
        return [], [f"order unresolvable: payment_id {dispute.payment_id} "
                    f"matches no order"]
    shipments = repo.list_shipments_for_order(order.id)
    candidates: list[Evidence] = []
    n = 0

    def cand(key: str, claim: str, doc_id: str, span: str, fields: dict) -> None:
        nonlocal n
        if checklist_keys is not None and key not in checklist_keys:
            return
        n += 1
        candidates.append(Evidence(
            id=f"{dispute.id}-E{n}", case_id=f"pre-case:{dispute.id}",
            evidence_key=key, claim=claim, source_doc_id=doc_id,
            quoted_span=span, fields_json=json.dumps(fields)))

    # --- POD-derived candidates ------------------------------------------------
    pod = None
    if shipments and shipments[0].pod_doc_id:
        pod = repo.get_document(shipments[0].pod_doc_id)
    if pod is None:
        notes.append("no POD document on shipment"
                     if shipments else "no shipment on order")
    else:
        awb_m = _AWB_RE.search(pod.raw_text)
        if awb_m:
            cand("awb", "a shipment exists for this order", pod.id,
                 awb_m.group(0), {"awb": awb_m.group(1).strip()})
        del_m = _DELIVERED_RE.search(pod.raw_text)
        if del_m and awb_m:
            cand("pod", "the shipment was delivered", pod.id, del_m.group(0),
                 {"awb": awb_m.group(1).strip(),
                  "delivered_at": del_m.group(1).strip()})
        addr_m = _ADDRESS_RE.search(pod.raw_text)
        if addr_m:
            pins = _PIN_RE.findall(addr_m.group(1))
            if pins:
                cand("address_match", "delivered to the ordered address",
                     pod.id, addr_m.group(0), {"pincode": pins[-1]})
        if _OTP_LINE in pod.raw_text:
            cand("otp_verified", "courier verified the delivery OTP",
                 pod.id, _OTP_LINE, {})

    # --- mailbox-derived candidates ----------------------------------------------
    row = repo.conn.execute(
        "SELECT id, raw_text FROM documents WHERE source = ? AND type = 'email'",
        (f"mailbox:{order.customer_email}",)).fetchone()
    if row:
        block = _find_admission_block(row["raw_text"], order.customer_email)
        if block:
            sent_at, body_line = block
            cand("admission_email", "customer acknowledged receiving the parcel",
                 row["id"], body_line, {"sent_at": sent_at})
        else:
            notes.append("mailbox thread present but no admission found")
    return candidates, notes


def _find_admission_block(thread: str, customer_email: str
                          ) -> tuple[str, str] | None:
    """Thread blocks are 'From:\\nDate:\\n\\nbody' separated by ---.
    Return (sent_at, admission_line) for the customer's admission, if any."""
    for block in thread.split("\n---\n"):
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue
        frm = lines[0].removeprefix("From: ").strip()
        sent_at = lines[1].removeprefix("Date: ").strip()
        body = "\n".join(lines[3:]).strip() or (lines[-1].strip())
        if frm != customer_email:
            continue
        if any(m in body for m in ADMISSION_MARKERS):
            return sent_at, body
    return None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
