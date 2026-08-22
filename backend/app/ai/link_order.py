"""AI order linking (spec §30.4). Called ONLY after deterministic exact
matching has failed. Ranks supplied candidates; never touches the database;
never selects an id outside the candidate set (schema-enforced)."""

from __future__ import annotations

from dataclasses import dataclass

from ..store.models import Dispute, Order
from .errors import LowConfidence
from .prompting import AICallRecord, call_with_repair, load_prompt
from .schemas import LinkProposal, validate_link_output

PROMPT = load_prompt("link_order")


@dataclass(frozen=True)
class LinkResult:
    proposal: LinkProposal
    records: tuple[AICallRecord, ...]


def link_order(dispute: Dispute, candidates: list[Order], client) -> LinkResult:
    if not candidates:
        raise LowConfidence(task="link_order",
                            reason="no candidate orders were found to rank")
    payload = {
        "dispute": {"id": dispute.id, "payment_id": dispute.payment_id,
                    "amount": dispute.amount,
                    "reason_code": dispute.reason_code.value,
                    "respond_by": dispute.respond_by},
        "candidates": [{"id": o.id, "amount": o.amount,
                        "customer_email": o.customer_email,
                        "created_at": o.created_at, "address": o.address}
                       for o in candidates],
    }
    ids = {o.id for o in candidates}
    proposal, records = call_with_repair(
        client, PROMPT, payload,
        validator=lambda text: validate_link_output(text, ids),
        task="link_order")
    return LinkResult(proposal=proposal, records=records)
