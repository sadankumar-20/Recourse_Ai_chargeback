"""The investigation planner (R2) — the agent's mind, deliberately handless.

This module decides WHAT to investigate next; it can execute nothing. It
returns tool *requests as data*; the coordination layer
(backend/app/investigation.py) executes them through the R1 read-only
registry. This module cannot import the repository, sqlite3, or app.tools
(AST-enforced since Stage 6), and it never emits FIGHT/ACCEPT/ESCALATE —
"I should check tracking" is in scope; "therefore fight" never is.

Two planner implementations behind one interface (the ADR-003 pattern):
- Anthropic: one structured-JSON planning call per step through the existing
  hardened complete() path (strict schema -> one repair -> LowConfidence).
- Deterministic offline planner: a pure function of (context, history) —
  reproducible, network-free, used by the entire test suite. It encodes a
  sensible checklist-driven strategy, not the model's judgment; the point of
  the shared interface is that everything downstream (registry, gate,
  decision engine, audit) treats both identically.

No chain-of-thought is produced or stored: a PlannerDecision carries only
operational fields — goal, tool, args, what's missing, why it stopped.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from .errors import LowConfidence
from .prompting import AICallRecord, call_with_repair, load_prompt
from .schemas import SchemaError, strip_json

PLAN_ACTIONS = ("tool", "complete", "needs_input")


def validate_plan_output(text: str) -> dict:
    """Strict plan schema: exact keys, enum action, typed fields. Anything
    else raises SchemaError -> one repair -> LowConfidence (existing path)."""
    obj = json.loads(strip_json(text))
    if not isinstance(obj, dict):
        raise SchemaError("plan must be a JSON object")
    allowed = {"action", "goal", "tool", "args", "missing", "request_to_user"}
    if not set(obj) <= allowed or not {"action", "goal"} <= set(obj):
        raise SchemaError(f"plan keys must be within {sorted(allowed)} and "
                          f"include action + goal, got {sorted(obj)}")
    if obj["action"] not in PLAN_ACTIONS:
        raise SchemaError(f"action must be one of {PLAN_ACTIONS}")
    if not isinstance(obj["goal"], str) or not obj["goal"].strip():
        raise SchemaError("goal must be a non-empty string")
    if obj["action"] == "tool" and not isinstance(obj.get("tool"), str):
        raise SchemaError("action 'tool' requires a tool name")
    if "args" in obj and not isinstance(obj["args"], dict):
        raise SchemaError("args must be an object")
    if "missing" in obj and not (isinstance(obj["missing"], list) and
                                 all(isinstance(m, str)
                                     for m in obj["missing"])):
        raise SchemaError("missing must be a list of strings")
    return obj


@dataclass(frozen=True)
class InvestigationContext:
    """Everything the planner may know. Assembled by the coordination layer;
    contains ids and facts, never live objects."""
    dispute: dict          # id, amount, reason_code, respond_by, payment_id
    order: dict            # id, customer_email, address (already linked)
    checklist: list[dict]  # [{key, description, required}] from the playbook
    tool_specs: list[dict] # registry's model-consumable specs


@dataclass(frozen=True)
class PlannerDecision:
    action: str                       # tool | complete | needs_input
    goal: str                         # operational, user-facing
    tool: str | None = None
    args: dict = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    request_to_user: str | None = None

    def to_dict(self) -> dict:
        return {"action": self.action, "goal": self.goal, "tool": self.tool,
                "args": self.args, "missing": self.missing,
                "request_to_user": self.request_to_user}


@dataclass(frozen=True)
class PlanStep:
    decision: PlannerDecision
    records: list[AICallRecord]


def plan_next(ctx: InvestigationContext, history: list[dict],
              client) -> PlanStep:
    """history: [{tool, args, ok, summary}] of prior steps (observations),
    exactly what the coordination layer chose to share — never raw secrets."""
    if getattr(client, "provider", "stub") == "anthropic":
        return _plan_with_model(ctx, history, client)
    return PlanStep(decision=_deterministic_plan(ctx, history), records=[])


# --- Anthropic path -----------------------------------------------------------------

def _plan_with_model(ctx: InvestigationContext, history: list[dict],
                     client) -> PlanStep:
    prompt = load_prompt("investigate")
    payload = {"dispute": ctx.dispute, "order": ctx.order,
               "checklist": ctx.checklist, "tools": ctx.tool_specs,
               "history": history[-10:]}
    obj, records = call_with_repair(client, prompt, payload,
                                    validate_plan_output, task="investigate")
    return PlanStep(decision=PlannerDecision(
        action=obj["action"], goal=obj["goal"], tool=obj.get("tool"),
        args=obj.get("args") or {}, missing=obj.get("missing") or [],
        request_to_user=obj.get("request_to_user")), records=list(records))


# --- deterministic offline planner --------------------------------------------------

def _seen(history: list[dict], tool: str) -> list[dict]:
    return [h for h in history if h["tool"] == tool]


def _deterministic_plan(ctx: InvestigationContext,
                        history: list[dict]) -> PlannerDecision:
    """Checklist-driven strategy: establish shipment facts, obtain a POD (the
    merchant's file, or the courier's own record when the file is missing),
    read the customer's messages, reconcile refunds, then conclude. Pure
    function of its inputs — byte-reproducible."""
    order_id = ctx.order["id"]
    keys = {c["key"] for c in ctx.checklist}

    pod_seen = any(h["tool"] == "read_document" and h.get("ok")
                   and (h.get("data") or {}).get("type") == "pod"
                   for h in history)

    ships = _seen(history, "get_shipments")
    if not ships:
        return PlannerDecision("tool", "establish whether a shipment exists",
                               tool="get_shipments",
                               args={"order_id": order_id})
    shipments = (ships[-1].get("data") or {}).get("shipments", [])

    if "pod" in keys and shipments and not pod_seen:
        pod_ids = [s["pod_doc_id"] for s in shipments if s.get("pod_doc_id")]
        read_ids = {h["args"].get("doc_id") for h in _seen(history,
                                                           "read_document")}
        for pid in pod_ids:
            if pid not in read_ids:
                return PlannerDecision("tool", "read the merchant's proof of "
                                               "delivery",
                                       tool="read_document",
                                       args={"doc_id": pid})
        if not pod_ids:
            tracks = _seen(history, "fetch_tracking")
            if not tracks:
                return PlannerDecision(
                    "tool", "the merchant's POD file is missing — query the "
                            "courier's own tracking record",
                    tool="fetch_tracking", args={"awb": shipments[0]["awb"]})
            data = tracks[-1].get("data") or {}
            if not tracks[-1].get("ok") or data.get("status") != "delivered":
                return PlannerDecision(
                    "needs_input", "no delivery confirmation exists on the "
                                   "merchant or courier side",
                    missing=["pod"],
                    request_to_user=(
                        f"Upload the courier proof of delivery for AWB "
                        f"{shipments[0]['awb']} ({shipments[0]['courier']}); "
                        f"the courier's tracking shows "
                        f"'{data.get('status', 'no record')}' and no delivery "
                        f"confirmation."))

    if not _seen(history, "search_knowledge"):
        return PlannerDecision(
            "tool", "retrieve the representment requirements for this "
                    "reason code",
            tool="search_knowledge",
            args={"query": f"{ctx.dispute['reason_code']} representment "
                           f"requirements evidence"})

    if not _seen(history, "search_inbox"):
        return PlannerDecision("tool", "review the customer's messages",
                               tool="search_inbox",
                               args={"customer_email":
                                     ctx.order["customer_email"]})
    inbox = (_seen(history, "search_inbox")[-1].get("data") or {})
    read_ids = {h["args"].get("doc_id") for h in _seen(history,
                                                       "read_document")}
    for email in (inbox.get("emails") or [])[:3]:
        if email["id"] not in read_ids:
            return PlannerDecision("tool", "read a customer message",
                                   tool="read_document",
                                   args={"doc_id": email["id"]})

    if not _seen(history, "get_refunds"):
        return PlannerDecision("tool", "reconcile refunds against the "
                                       "disputed amount",
                               tool="get_refunds", args={"order_id": order_id})

    return PlannerDecision(
        "complete", "every findable source has been examined; hand the "
                    "gathered documents to the gate and the decision engine")
