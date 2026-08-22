"""Strict validators for LLM output. Every response is untrusted input.

Hand-rolled on purpose (ADR-001): validation is explicit code we own and
test, not a library annotation. Any deviation raises SchemaError with a
message precise enough to drive the single repair retry.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .errors import SchemaError
from .prompting import strip_json


def _parse_object(text: str) -> dict:
    try:
        value = json.loads(strip_json(text))
    except (TypeError, ValueError) as e:
        raise SchemaError(f"response is not valid JSON: {e}")
    if not isinstance(value, dict):
        raise SchemaError(f"expected a JSON object, got {type(value).__name__}")
    return value


@dataclass(frozen=True)
class LinkProposal:
    order_id: str
    confidence: float
    reasoning: str


def validate_link_output(text: str, candidate_ids: set[str]) -> LinkProposal:
    obj = _parse_object(text)
    extra = set(obj) - {"order_id", "confidence", "reasoning"}
    if extra:
        raise SchemaError(f"unexpected keys: {sorted(extra)}")
    for key in ("order_id", "confidence", "reasoning"):
        if key not in obj:
            raise SchemaError(f"missing required key '{key}'")
    order_id = obj["order_id"]
    if not isinstance(order_id, str) or order_id not in candidate_ids:
        raise SchemaError(
            f"order_id {order_id!r} is not one of the supplied candidates "
            f"{sorted(candidate_ids)} — ids outside the candidate set are "
            f"never accepted")
    conf = obj["confidence"]
    if not isinstance(conf, (int, float)) or isinstance(conf, bool) \
            or not (0.0 <= float(conf) <= 1.0):
        raise SchemaError(f"confidence must be a number in [0,1], got {conf!r}")
    reasoning = obj["reasoning"]
    if not isinstance(reasoning, str) or not reasoning.strip():
        raise SchemaError("reasoning must be a non-empty string")
    return LinkProposal(order_id=order_id, confidence=float(conf),
                        reasoning=reasoning.strip())


@dataclass(frozen=True)
class ExtractedCandidate:
    key: str
    claim: str
    source_doc_id: str
    quoted_span: str
    fields: dict


def validate_extraction_output(text: str, allowed_doc_ids: set[str],
                               checklist: dict[str, tuple[str, ...]]
                               ) -> list[ExtractedCandidate]:
    """checklist maps evidence key -> required field names."""
    obj = _parse_object(text)
    if set(obj) != {"evidence"}:
        raise SchemaError("top level must be exactly {'evidence': [...]}")
    items = obj["evidence"]
    if not isinstance(items, list):
        raise SchemaError("'evidence' must be a list (empty list is valid)")
    out: list[ExtractedCandidate] = []
    for i, item in enumerate(items):
        where = f"evidence[{i}]"
        if not isinstance(item, dict):
            raise SchemaError(f"{where}: must be an object")
        for key in ("key", "claim", "source_doc_id", "quoted_span", "fields"):
            if key not in item:
                raise SchemaError(f"{where}: missing required key '{key}'")
        if item["key"] not in checklist:
            raise SchemaError(f"{where}: key {item['key']!r} is not in the "
                              f"checklist {sorted(checklist)}")
        if item["source_doc_id"] not in allowed_doc_ids:
            raise SchemaError(f"{where}: source_doc_id {item['source_doc_id']!r} "
                              f"is not one of the provided documents")
        if not isinstance(item["quoted_span"], str) or not item["quoted_span"].strip():
            raise SchemaError(f"{where}: quoted_span must be a non-empty string")
        if not isinstance(item["claim"], str) or not item["claim"].strip():
            raise SchemaError(f"{where}: claim must be a non-empty string")
        fields = item["fields"]
        if not isinstance(fields, dict):
            raise SchemaError(f"{where}: fields must be an object")
        missing = [f for f in checklist[item["key"]] if f not in fields]
        if missing:
            raise SchemaError(f"{where}: fields missing required "
                              f"{missing} for key '{item['key']}'")
        out.append(ExtractedCandidate(
            key=item["key"], claim=item["claim"].strip(),
            source_doc_id=item["source_doc_id"],
            quoted_span=item["quoted_span"], fields=fields))
    return out


def validate_draft_output(text: str, admitted_display_ids: set[str],
                          citation_validator) -> str:
    """Drafts are plain text; the deterministic citation validator is the
    final authority. Its violations become the repair message verbatim."""
    if not isinstance(text, str) or len(text.strip()) < 40:
        raise SchemaError("draft is empty or too short to be a representment")
    violations = citation_validator(text.strip(), admitted_display_ids)
    if violations:
        raise SchemaError("citation violations: " + " | ".join(violations[:5]))
    return text.strip()
