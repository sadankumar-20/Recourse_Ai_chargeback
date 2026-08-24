"""Read-only investigation tools (R1) — the future agent's hands.

Design rules, each enforced in code and tests, not by convention:

1. READ-ONLY BY CONSTRUCTION. Tools never receive the Repository — they
   receive a whitelist proxy exposing only lookup methods. A tool that tries
   to write raises ToolAccessDenied. There is nothing here for the executor,
   the adapters, or any money path to be reached from.
2. THE AI NEVER CALLS THIS MODULE. The AI lane cannot import app.tools
   (AST-enforced since Stage 6). The investigator (R2) emits tool *requests*
   as data; the orchestrator executes them through this registry. Tool
   choice is the model's; tool execution never is.
3. EVERY CALL IS EVIDENCE. Each execution appends a TOOL_CALL entry to the
   case's audit hash chain: tool, validated args, ok/error, provenance of
   what was read, a truncated result summary, and budget usage.
4. BOUNDED. A per-investigation budget caps total calls; exceeding it raises
   ToolBudgetExceeded (the loop must conclude or escalate, never wander).
5. STRUCTURED FAILURE. Bad tool names and bad arguments return machine-
   readable error results (the agent can adapt); only budget exhaustion and
   write attempts raise.

Provenance: results carry the provenance of what was read (a document's own
provenance, or 'simulator' for the synthetic system of record — R5 adds
'tracking_api' etc.), so the UI can badge every fact's origin.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from ..store.models import Provenance
from ..store.repo import Repository

DEFAULT_TOOL_BUDGET = 12
_SUMMARY_LIMIT = 240


class ToolAccessDenied(RuntimeError):
    """A tool attempted a non-whitelisted (write) repository operation."""


class ToolBudgetExceeded(RuntimeError):
    """The investigation spent its tool budget without concluding."""


class ReadOnlyRepo:
    """Whitelist proxy: the ONLY repository surface tools can touch.
    Everything else — add_*, update_*, set_*, append_audit, raw conn —
    raises. This is what makes 'read-only tools' a property, not a promise."""

    _ALLOWED = frozenset({
        "get_order", "get_order_by_payment", "get_dispute", "get_document",
        "list_shipments_for_order", "list_refunds_for_order",
        "list_documents_for_case", "list_evidence_for_case",
    })

    def __init__(self, repo: Repository):
        object.__setattr__(self, "_repo", repo)

    def __getattr__(self, name: str):
        if name in ReadOnlyRepo._ALLOWED:
            return getattr(self._repo, name)
        raise ToolAccessDenied(
            f"repository method '{name}' is not readable from investigation "
            f"tools — tools are read-only by construction")

    def __setattr__(self, name, value):
        raise ToolAccessDenied("investigation tools cannot mutate state")

    # controlled read-only SQL for search tools (SELECT enforced)
    def select(self, sql: str, params: tuple = ()) -> list[dict]:
        if not sql.lstrip().lower().startswith("select"):
            raise ToolAccessDenied("only SELECT queries are permitted")
        cur = self._repo.conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


@dataclass(frozen=True)
class ToolResult:
    tool: str
    ok: bool
    data: Any
    provenance: list[str]
    error: str | None = None

    def to_dict(self) -> dict:
        return {"tool": self.tool, "ok": self.ok, "data": self.data,
                "provenance": self.provenance, "error": self.error}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str                 # shown to the model (R2) and the UI
    params: dict[str, dict]          # name -> {"type": ..., "required": bool}
    fn: Callable[[ReadOnlyRepo, dict], tuple[Any, list[str]]]


# --- tool implementations (each returns (data, provenance_list)) --------------------

def _search_orders(ro: ReadOnlyRepo, a: dict):
    clauses, params = [], []
    if a.get("payment_id"):
        clauses.append("payment_id = ?"); params.append(a["payment_id"])
    if a.get("amount") is not None:
        clauses.append("amount = ?"); params.append(a["amount"])
    if a.get("customer_email"):
        clauses.append("customer_email = ?"); params.append(a["customer_email"])
    if not clauses:
        return {"orders": [], "note": "provide payment_id, amount, or "
                                      "customer_email"}, []
    rows = ro.select(
        "SELECT id, payment_id, amount, customer_email, address, created_at, "
        "promised_ship_by FROM orders WHERE " + " AND ".join(clauses) +
        " ORDER BY created_at LIMIT 8", tuple(params))
    return {"orders": rows, "count": len(rows)}, [Provenance.SIMULATOR.value]


def _get_order(ro: ReadOnlyRepo, a: dict):
    o = ro.get_order(a["order_id"])
    if o is None:
        return {"error": f"no order {a['order_id']!r}"}, []
    return {"id": o.id, "payment_id": o.payment_id, "amount": o.amount,
            "customer_email": o.customer_email, "address": o.address,
            "created_at": o.created_at, "promised_ship_by": o.promised_ship_by}, \
           [Provenance.SIMULATOR.value]


def _get_dispute(ro: ReadOnlyRepo, a: dict):
    d = ro.get_dispute(a["dispute_id"])
    if d is None:
        return {"error": f"no dispute {a['dispute_id']!r}"}, []
    return {"id": d.id, "payment_id": d.payment_id, "amount": d.amount,
            "reason_code": d.reason_code.value, "respond_by": d.respond_by,
            "status": d.status.value, "provenance": d.provenance}, [d.provenance]


def _get_shipments(ro: ReadOnlyRepo, a: dict):
    ships = ro.list_shipments_for_order(a["order_id"])
    return {"shipments": [{"id": s.id, "awb": s.awb, "courier": s.courier,
                           "ship_date": s.ship_date, "status": s.status,
                           "pod_doc_id": s.pod_doc_id} for s in ships],
            "count": len(ships)}, [Provenance.SIMULATOR.value]


def _get_refunds(ro: ReadOnlyRepo, a: dict):
    refunds = ro.list_refunds_for_order(a["order_id"])
    return {"refunds": [{"id": r.id, "amount": r.amount,
                         "created_at": r.created_at} for r in refunds],
            "total_refunded": sum(r.amount for r in refunds)}, \
           [Provenance.SIMULATOR.value]


def _search_inbox(ro: ReadOnlyRepo, a: dict):
    rows = ro.select(
        "SELECT id, type, source, fetched_at, provenance, "
        "substr(raw_text, 1, 160) AS snippet FROM documents "
        "WHERE source = ? AND type = 'email' ORDER BY fetched_at",
        (f"mailbox:{a['customer_email']}",))
    return {"emails": rows, "count": len(rows)}, \
           sorted({r["provenance"] for r in rows})


def _read_document(ro: ReadOnlyRepo, a: dict):
    d = ro.get_document(a["doc_id"])
    if d is None:
        return {"error": f"no document {a['doc_id']!r}"}, []
    return {"id": d.id, "type": d.type.value, "source": d.source,
            "fetched_at": d.fetched_at, "provenance": d.provenance,
            "raw_text": d.raw_text}, [d.provenance]


def _fetch_tracking(ro: ReadOnlyRepo, a: dict):
    """Simulated courier tracking system (provenance: simulator; the real
    HTTP adapter arrives in R5). Deterministically reconstructs the courier's
    own delivery record from world state: if the shipment reached
    'delivered', the courier knows it — even when the merchant lost the POD
    file. Read-only: materializing the confirmation as a document is the
    coordination layer's job, never a tool's."""
    rows = ro.select(
        "SELECT s.awb, s.courier, s.ship_date, s.status, o.address, "
        "o.customer_email FROM shipments s JOIN orders o ON o.id = s.order_id "
        "WHERE s.awb = ?", (a["awb"],))
    if not rows:
        return {"error": f"courier has no record of AWB {a['awb']!r}"}, []
    s = rows[0]
    if s["status"] != "delivered":
        return {"awb": s["awb"], "courier": s["courier"],
                "status": s["status"],
                "note": "no delivery confirmation available"},                [Provenance.SIMULATOR.value]
    from datetime import datetime, timedelta
    delivered_at = (datetime.fromisoformat(s["ship_date"])
                    + timedelta(hours=72)).isoformat(timespec="seconds")
    return {"awb": s["awb"], "courier": s["courier"], "status": "delivered",
            "ship_date": s["ship_date"], "delivered_at": delivered_at,
            "receiver": "".join(c for c in s["customer_email"].split("@")[0]
                                if not c.isdigit()).replace(".", " ").title(),
            "address": s["address"],
            "confirmation": "courier tracking shows successful delivery"},            [Provenance.SIMULATOR.value]


TOOLS: dict[str, ToolSpec] = {t.name: t for t in (
    ToolSpec("search_orders",
             "Find candidate orders by payment_id, amount, or customer email.",
             {"payment_id": {"type": str, "required": False},
              "amount": {"type": int, "required": False},
              "customer_email": {"type": str, "required": False}},
             _search_orders),
    ToolSpec("get_order", "Fetch one order (the system of record).",
             {"order_id": {"type": str, "required": True}}, _get_order),
    ToolSpec("get_dispute", "Fetch the dispute under investigation.",
             {"dispute_id": {"type": str, "required": True}}, _get_dispute),
    ToolSpec("get_shipments", "List shipments (AWB, courier, POD doc id) for an order.",
             {"order_id": {"type": str, "required": True}}, _get_shipments),
    ToolSpec("get_refunds", "List refunds and the total refunded for an order.",
             {"order_id": {"type": str, "required": True}}, _get_refunds),
    ToolSpec("search_inbox", "List the customer's email documents with snippets.",
             {"customer_email": {"type": str, "required": True}}, _search_inbox),
    ToolSpec("read_document", "Read a document's full text and provenance.",
             {"doc_id": {"type": str, "required": True}}, _read_document),
    ToolSpec("fetch_tracking",
             "Query the courier's tracking system by AWB — useful when the "
             "merchant's own POD document is missing.",
             {"awb": {"type": str, "required": True}}, _fetch_tracking),
)}


class ToolRegistry:
    """Executes validated tool requests for ONE case's investigation, under a
    budget, writing every call into the case's audit hash chain."""

    def __init__(self, repo: Repository, case_id: str,
                 budget: int = DEFAULT_TOOL_BUDGET):
        self._repo = repo               # kept ONLY for auditing
        self._ro = ReadOnlyRepo(repo)
        self.case_id = case_id
        self.budget = budget
        self.calls_used = 0

    @staticmethod
    def specs_for_model() -> list[dict]:
        """Tool schemas in a model-consumable shape (used by R2)."""
        return [{"name": t.name, "description": t.description,
                 "params": {k: {"type": v["type"].__name__,
                                "required": v["required"]}
                            for k, v in t.params.items()}}
                for t in TOOLS.values()]

    def execute(self, name: str, args: dict | None = None) -> ToolResult:
        args = args or {}
        if self.calls_used >= self.budget:
            raise ToolBudgetExceeded(
                f"tool budget of {self.budget} calls exhausted for "
                f"{self.case_id} — the investigation must conclude or escalate")
        self.calls_used += 1

        spec = TOOLS.get(name)
        if spec is None:
            return self._finish(name, args, ok=False, data=None, prov=[],
                                error=f"unknown tool '{name}' "
                                      f"(available: {sorted(TOOLS)})")
        error = self._validate(spec, args)
        if error:
            return self._finish(name, args, ok=False, data=None, prov=[],
                                error=error)
        data, prov = spec.fn(self._ro, args)
        ok = not (isinstance(data, dict) and "error" in data)
        return self._finish(name, args, ok=ok, data=data, prov=prov,
                            error=data.get("error") if not ok else None)

    @staticmethod
    def _validate(spec: ToolSpec, args: dict) -> str | None:
        unknown = set(args) - set(spec.params)
        if unknown:
            return f"unknown argument(s) {sorted(unknown)} for {spec.name}"
        for pname, rule in spec.params.items():
            if rule["required"] and pname not in args:
                return f"missing required argument '{pname}' for {spec.name}"
            if pname in args and not isinstance(args[pname], rule["type"]):
                return (f"argument '{pname}' must be {rule['type'].__name__}, "
                        f"got {type(args[pname]).__name__}")
        return None

    def _finish(self, name, args, *, ok, data, prov, error) -> ToolResult:
        result = ToolResult(tool=name, ok=ok, data=data,
                            provenance=prov, error=error)
        summary = json.dumps(data, default=str)[:_SUMMARY_LIMIT] if ok else error
        self._repo.append_audit(self.case_id, "TOOL_CALL", {
            "tool": name, "args": args, "ok": ok, "error": error,
            "provenance": prov, "result_summary": summary,
            "budget": {"used": self.calls_used, "of": self.budget}})
        return result
