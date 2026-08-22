"""Action executor: the ONLY writer of money actions (spec §8 step 10, §18).

Flow for contest/accept:
  1. validate the action type — ESCALATE is not a payments action, and
     nothing outside {contest, accept} can reach an adapter;
  2. idempotency check against the PERSISTED actions table with
     idempotency_key = dispute_id. One money action per dispute, ever: a
     duplicate OR CONFLICTING second attempt (e.g. accept after contest)
     returns the original action and is audited as ACTION_DUPLICATE;
  3. execute via the adapter. Transient failures (503) are audited as
     ACTION_FAILED and re-raised for the future orchestrator's retry loop —
     a failed call is NOT a submission and creates no action row;
  4. persist the ActionRecord and audit ACTION_SUBMITTED with the redacted
     request/response, adapter identity, actor, and policy/decision versions.

The executor never decides anything; it executes a decision it is handed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ..store.models import ActionRecord, Actor
from ..store.repo import Repository, utc_now_iso
from .payments_adapter import PaymentsAdapter, TransientPaymentsError

EXECUTABLE_ACTIONS = ("contest", "accept")


@dataclass(frozen=True)
class ExecutionResult:
    action: ActionRecord
    response: dict
    duplicate: bool


def execute_action(repo: Repository, adapter: PaymentsAdapter, *,
                   case_id: str, dispute_id: str, action_type: str,
                   payload: dict, actor: Actor,
                   decision_meta: dict | None = None) -> ExecutionResult:
    if action_type not in EXECUTABLE_ACTIONS:
        raise ValueError(
            f"'{action_type}' is not an executable payments action "
            f"(allowed: {EXECUTABLE_ACTIONS}). Escalation is a human task, "
            f"not an API call.")

    meta = decision_meta or {}
    idempotency_key = dispute_id

    existing = repo.get_action_by_idempotency_key(idempotency_key)
    if existing is not None:
        repo.append_audit(case_id, "ACTION_DUPLICATE", {
            "dispute_id": dispute_id,
            "attempted_action": action_type,
            "attempted_by": actor.value,
            "original_action_id": existing.id,
            "original_action_type": existing.type,
            "original_at": existing.at,
            "note": "idempotency_key already used — no second submission "
                    "was made; returning the original result",
        })
        return ExecutionResult(action=existing,
                               response=json.loads(existing.response_json),
                               duplicate=True)

    try:
        if action_type == "contest":
            result = adapter.contest_dispute(dispute_id, payload)
        else:
            result = adapter.accept_dispute(dispute_id)
    except TransientPaymentsError as e:
        repo.append_audit(case_id, "ACTION_FAILED", {
            "dispute_id": dispute_id, "action": action_type,
            "adapter": adapter.name, "status": e.status,
            "error": str(e), "actor": actor.value,
            "note": "transient failure — no submission occurred; retry is "
                    "the orchestrator's job",
        })
        raise

    action = ActionRecord(
        id=f"act_{dispute_id}", case_id=case_id, type=action_type,
        idempotency_key=idempotency_key,
        request_json=json.dumps(payload, sort_keys=True),
        response_json=json.dumps(result.to_dict(), sort_keys=True),
        actor=actor, at=utc_now_iso())
    repo.add_action(action)
    repo.append_audit(case_id, "ACTION_SUBMITTED", {
        "dispute_id": dispute_id, "action": action_type,
        "idempotency_key": idempotency_key,
        "adapter": adapter.name, "simulated": adapter.simulated,
        "request": payload, "response": result.to_dict(),
        "actor": actor.value, "action_id": action.id,
        **({"decision": meta} if meta else {}),
    })
    return ExecutionResult(action=action, response=result.to_dict(),
                           duplicate=False)
