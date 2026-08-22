"""AI evidence extraction (spec §8 step 6, §30.4). Checklist-driven; output
is UNTRUSTED and must subsequently pass the deterministic Admissibility Gate.
Quotes stay verbatim in the source language (incl. Hinglish) — the gate's
exact-substring check depends on it."""

from __future__ import annotations

import json
from dataclasses import dataclass

from ..policy.playbooks import ReasonPlaybook
from ..store.models import Dispute, Document, Evidence
from .prompting import AICallRecord, call_with_repair, load_prompt
from .schemas import validate_extraction_output

PROMPT = load_prompt("extract_evidence")


@dataclass(frozen=True)
class ExtractionResult:
    candidates: list[Evidence]
    records: tuple[AICallRecord, ...]


def extract_evidence(case_id: str, dispute: Dispute,
                     documents: list[Document], playbook: ReasonPlaybook,
                     client) -> ExtractionResult:
    checklist = {r.key: r.required_fields for r in playbook.rules.values()}
    payload = {
        "dispute": {"id": dispute.id, "reason_code": dispute.reason_code.value},
        "checklist": [{"key": r.key, "description": r.description,
                       "required_fields": list(r.required_fields)}
                      for r in playbook.rules.values()],
        "documents": [{"id": d.id, "type": d.type.value, "source": d.source,
                       "raw_text": d.raw_text} for d in documents],
    }
    extracted, records = call_with_repair(
        client, PROMPT, payload,
        validator=lambda text: validate_extraction_output(
            text, {d.id for d in documents}, checklist),
        task="extract_evidence")
    candidates = [
        Evidence(id=f"E{i + 1}", case_id=case_id, evidence_key=c.key,
                 claim=c.claim, source_doc_id=c.source_doc_id,
                 quoted_span=c.quoted_span, fields_json=json.dumps(c.fields))
        for i, c in enumerate(extracted)]
    return ExtractionResult(candidates=candidates, records=records)
