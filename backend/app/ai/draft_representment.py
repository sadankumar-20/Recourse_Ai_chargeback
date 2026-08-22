"""Citation-constrained drafting (spec §8 step 9, §30.4).

Receives ONLY admitted evidence (guarded — passing failed evidence raises).
The deterministic policy/citations validator is the final authority: its
violations drive the single repair attempt, and a still-invalid draft raises
LowConfidence. There is no bypass."""

from __future__ import annotations

import json
from dataclasses import dataclass

from ..policy.citations import validate_citations
from ..store.models import Dispute, Evidence, GateVerdict, Order
from .prompting import AICallRecord, call_with_repair, load_prompt
from .schemas import validate_draft_output

PROMPT = load_prompt("draft_representment")


@dataclass(frozen=True)
class DraftResult:
    text: str
    display_map: dict[str, str]          # display id (E1) -> evidence.id
    records: tuple[AICallRecord, ...]


def draft_representment(admitted: list[Evidence], dispute: Dispute,
                        order: Order, client) -> DraftResult:
    if not admitted:
        raise ValueError("cannot draft with zero admitted evidence")
    for e in admitted:
        if e.gate_verdict is not GateVerdict.PASS:
            raise ValueError(
                f"evidence {e.id} has verdict {e.gate_verdict} — only "
                f"gate-admitted evidence may enter the drafting prompt")

    display_map: dict[str, str] = {}
    ev_payload = []
    for i, e in enumerate(sorted(admitted, key=lambda x: x.id)):
        display = f"E{i + 1}"
        display_map[display] = e.id
        ev_payload.append({"display_id": display, "key": e.evidence_key,
                           "claim": e.claim, "quoted_span": e.quoted_span,
                           "fields": json.loads(e.fields_json)})
    payload = {
        "dispute": {"id": dispute.id, "amount": dispute.amount,
                    "reason_code": dispute.reason_code.value},
        "order": {"id": order.id, "amount": order.amount},
        "admitted_evidence": ev_payload,
    }
    admitted_ids = set(display_map)
    text, records = call_with_repair(
        client, PROMPT, payload,
        validator=lambda t: validate_draft_output(t, admitted_ids,
                                                  validate_citations),
        task="draft_representment")
    return DraftResult(text=text, display_map=display_map, records=records)
