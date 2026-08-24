"""Recourse REST API (Stage 10).

The boundary between humans and the agent. The dashboard talks ONLY to this
API; the API talks to the orchestrator and executor — never directly to the
payments adapter's action methods, never to AI internals, never around the
policy engine. Human approval exercises the transition Stage 8 reserved
(ESCALATED -> ACTED) as an explicit human actor, through the SAME executor
and the SAME idempotency as the agent.

Evidence verification panels are produced by REPLAYING the deterministic
gate on demand (the gate is pure, so re-verification is free and honest) —
the UI shows live check results, not stored screenshots of them.

Clock: the synthetic world is frozen at split.json's sim_now; the API pins
its clock there when a data dir is supplied (demo honesty: stated in
/health), otherwise uses real time.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, g, jsonify, request

from . import config
from .audit.chain import verify_audit_chain
from .orchestrator import Orchestrator
from .policy.gate import GateContext, admit_all
from .policy.playbooks import load_playbooks
from .store.models import Actor, CaseState, GateVerdict
from .store.repo import Repository
from .tools.executor import execute_action
from .tools.payments_adapter import SimulatorAdapter

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"


def create_app(db_path: str | Path, data_dir: str | Path | None = None,
               eval_metrics_path: str | Path | None = None,
               now: datetime | None = None) -> Flask:
    app = Flask(__name__, static_folder=str(FRONTEND_DIR), static_url_path="")
    app.config["DB_PATH"] = str(db_path)
    app.config["EVAL_METRICS"] = str(
        eval_metrics_path
        or Path(__file__).resolve().parents[2] / "evals" / "metrics.json")

    sim_now = now
    if sim_now is None and data_dir and (Path(data_dir) / "split.json").exists():
        sim_now = datetime.fromisoformat(
            json.loads((Path(data_dir) / "split.json").read_text())["sim_now"])
    app.config["SIM_NOW"] = sim_now
    playbooks = load_playbooks()

    # -- per-request repository ----------------------------------------------------

    def repo() -> Repository:
        if "repo" not in g:
            g.repo = Repository(app.config["DB_PATH"])
        return g.repo

    @app.teardown_appcontext
    def _close(_exc):
        r = g.pop("repo", None)
        if r is not None:
            r.close()

    def now_dt() -> datetime:
        return app.config["SIM_NOW"] or datetime.now(timezone.utc)

    def hours_left(dispute) -> float:
        return (datetime.fromisoformat(dispute.respond_by)
                - now_dt()).total_seconds() / 3600.0

    def orchestrator() -> Orchestrator:
        return Orchestrator(repo(), SimulatorAdapter(repo()),
                            playbooks=playbooks, now=app.config["SIM_NOW"])

    def err(status: int, message: str):
        return jsonify({"error": message}), status

    # -- health -------------------------------------------------------------------------

    @app.get("/health")
    def health():
        r = repo()
        counts = {t: r.conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
                  for t in ("disputes", "cases", "actions")}
        return jsonify({
            "ok": True, "counts": counts,
            "clock": now_dt().isoformat(timespec="seconds"),
            "clock_mode": "pinned_to_synthetic_world" if app.config["SIM_NOW"]
                          else "real_time",
            "ai_provider": config.AI_PROVIDER,
            "payments_provider": config.PAYMENTS_ADAPTER,
            "playbook_version": playbooks.version})

    # -- webhook intake --------------------------------------------------------------------

    @app.post("/webhooks/dispute")
    def webhook():
        event = request.get_json(silent=True) or {}
        try:
            result = orchestrator().process_event(event)
        except (ValueError, KeyError) as e:
            return err(400, str(e))
        return jsonify({"case_id": result.case.id,
                        "state": result.final_state.value,
                        "escalation_summary": result.escalation_summary}), 201

    # -- case queue -----------------------------------------------------------------------

    def _case_row(case) -> dict:
        r = repo()
        dispute = r.get_dispute(case.dispute_id)
        decisions = r.list_decisions_for_case(case.id)
        hours = hours_left(dispute)
        return {
            "case_id": case.id, "dispute_id": dispute.id,
            "amount": dispute.amount, "reason_code": dispute.reason_code.value,
            "state": case.state.value,
            "decision": decisions[-1].action.value if decisions else None,
            "link_confidence": case.link_confidence,
            "respond_by": dispute.respond_by, "hours_left": round(hours, 1),
            "escalated": case.state is CaseState.ESCALATED,
            "urgent": 0 < hours < config.DEADLINE_ESCALATE_HOURS
                      and case.state not in (CaseState.CLOSED,),
            "dispute_status": dispute.status.value,
        }

    @app.get("/cases")
    def cases():
        r = repo()
        state = request.args.get("state")
        rows = r.conn.execute("SELECT id FROM cases").fetchall()
        out = []
        for row in rows:
            case = r.get_case(row["id"])
            if state and case.state.value != state:
                continue
            out.append(_case_row(case))
        out.sort(key=lambda c: (c["state"] == "closed", c["hours_left"]))
        return jsonify({"cases": out, "total": len(out)})

    # -- case detail -----------------------------------------------------------------------

    def _audit_payloads(case_id: str) -> dict[str, dict]:
        latest: dict[str, dict] = {}
        for e in repo().read_audit(case_id):
            latest[e.step] = json.loads(e.payload_json)
        return latest

    @app.get("/cases/<case_id>")
    def case_detail(case_id):
        r = repo()
        case = r.get_case(case_id)
        if case is None:
            return err(404, f"no case {case_id}")
        dispute = r.get_dispute(case.dispute_id)
        order = r.get_order(case.linked_order_id) if case.linked_order_id else None
        steps = _audit_payloads(case_id)
        action = r.get_action_by_idempotency_key(dispute.id)
        draft = None
        if action and action.type == "contest":
            bundle = json.loads(action.request_json)
            if "representment" in bundle:
                draft = {"text": bundle["representment"],
                         "display_map": steps.get("DRAFT_CREATED", {})
                                             .get("display_map", {})}
        chain = verify_audit_chain(r, case_id)
        hours = hours_left(dispute)
        admitted = sum(1 for e in r.list_evidence_for_case(case_id)
                       if e.gate_verdict is GateVerdict.PASS)
        return jsonify({
            **_case_row(case),
            "order": None if order is None else {
                "id": order.id, "amount": order.amount,
                "customer_email": order.customer_email,
                "address": order.address, "created_at": order.created_at},
            "decision_math": steps.get("DECISION_MADE"),
            "escalation": steps.get("CASE_ESCALATED"),
            "draft": draft,
            "execution": None if action is None else {
                "type": action.type, "actor": action.actor.value,
                "at": action.at, "idempotency_key": action.idempotency_key,
                "response": json.loads(action.response_json)},
            "audit_chain": {"valid": chain.valid, "entries": chain.entries,
                            "broken_at": chain.broken_at_seq,
                            "reason": chain.reason},
            "allowed_human_actions": _allowed_actions(case, dispute, hours,
                                                      admitted),
        })

    def _allowed_actions(case, dispute, hours: float, admitted: int) -> list[str]:
        if case.state is not CaseState.ESCALATED:
            return []
        allowed = ["REJECT"]
        if hours > 0:
            allowed.append("ACCEPT")
            if admitted > 0:
                allowed.append("FIGHT")
        return allowed

    # -- evidence with live deterministic re-verification -------------------------------------

    @app.get("/cases/<case_id>/evidence")
    def case_evidence(case_id):
        r = repo()
        case = r.get_case(case_id)
        if case is None:
            return err(404, f"no case {case_id}")
        evidence = r.list_evidence_for_case(case_id)
        checks_by_id: dict[str, list] = {}
        if evidence and case.linked_order_id:
            dispute = r.get_dispute(case.dispute_id)
            order = r.get_order(case.linked_order_id)
            docs = {}
            for ship in r.list_shipments_for_order(order.id):
                if ship.pod_doc_id:
                    docs[ship.pod_doc_id] = r.get_document(ship.pod_doc_id)
            for row in r.conn.execute(
                    "SELECT id FROM documents WHERE source = ?",
                    (f"mailbox:{order.customer_email}",)).fetchall():
                docs[row["id"]] = r.get_document(row["id"])
            ctx = GateContext(dispute=dispute, order=order,
                              shipments=r.list_shipments_for_order(order.id),
                              refunds=r.list_refunds_for_order(order.id),
                              documents=docs, playbooks=playbooks, now=now_dt())
            for v in admit_all(evidence, ctx):     # pure gate replay
                checks_by_id[v.evidence_id] = [
                    {"name": c.name, "passed": c.passed, "detail": c.detail}
                    for c in v.checks]
        out = []
        for e in evidence:
            doc = r.get_document(e.source_doc_id)
            out.append({
                "id": e.id, "key": e.evidence_key, "claim": e.claim,
                "quoted_span": e.quoted_span,
                "fields": json.loads(e.fields_json),
                "verdict": e.gate_verdict.value if e.gate_verdict else None,
                "fail_reason": e.fail_reason,
                "source": None if doc is None else {
                    "id": doc.id, "type": doc.type.value, "source": doc.source},
                "checks": checks_by_id.get(e.id, []),
            })
        return jsonify({"evidence": out,
                        "note": "checks are a live replay of the deterministic "
                                "Admissibility Gate, not stored UI text"})

    # -- audit timeline ------------------------------------------------------------------------

    @app.get("/cases/<case_id>/audit")
    def case_audit(case_id):
        r = repo()
        if r.get_case(case_id) is None:
            return err(404, f"no case {case_id}")
        chain = verify_audit_chain(r, case_id)
        entries = [{"seq": e.seq, "at": e.at, "step": e.step,
                    "payload": json.loads(e.payload_json),
                    "entry_hash": e.entry_hash[:12]}
                   for e in r.read_audit(case_id)]
        return jsonify({"entries": entries,
                        "chain": {"valid": chain.valid,
                                  "entries": chain.entries,
                                  "broken_at": chain.broken_at_seq,
                                  "reason": chain.reason}})

    # -- metrics (committed Stage-9 artifact) ----------------------------------------------------

    @app.get("/metrics")
    def metrics():
        path = Path(app.config["EVAL_METRICS"])
        if not path.exists():
            return err(404, "no evaluation artifact; run "
                            "python3 evals/run_eval.py --ablate-gate")
        artifact = json.loads(path.read_text())
        gaps: dict[str, dict] = {}
        for c in artifact["cases"]:
            reason = c.get("escalation_reason") or ""
            if "unsupported reason code" in reason:
                gap = gaps.setdefault(c["reason_code"], {
                    "cases": 0, "amount_at_risk": 0,
                    "gt_winnable_amount": 0,
                    "needs": f"a v2 playbook for '{c['reason_code']}' "
                             f"(evidence checklist + checks + p_win bands)"})
                gap["cases"] += 1
                gap["amount_at_risk"] += c["amount"]
                if c["ground_truth_action"] == "FIGHT":
                    gap["gt_winnable_amount"] += c["amount"]
        return jsonify({"evaluation": artifact["metrics"],
                        "config": artifact["config"],
                        "meta": artifact["meta"],
                        "coverage_gaps": gaps})

    # -- human approval / rejection ------------------------------------------------------------------

    @app.post("/cases/<case_id>/approve")
    def approve(case_id):
        r = repo()
        case = r.get_case(case_id)
        if case is None:
            return err(404, f"no case {case_id}")
        if case.state is not CaseState.ESCALATED:
            return err(409, f"case is '{case.state.value}' — only escalated "
                            f"cases accept human approval")
        body = request.get_json(silent=True) or {}
        action = body.get("action")
        actor_name = (body.get("actor") or "").strip()
        if action not in ("FIGHT", "ACCEPT"):
            return err(400, f"action must be FIGHT or ACCEPT, got {action!r}")
        if not actor_name:
            return err(400, "actor is required — approvals are attributable")
        dispute = r.get_dispute(case.dispute_id)
        hours = hours_left(dispute)
        if hours <= 0:
            return err(409, f"deadline passed {abs(hours):.1f}h ago — "
                            f"money actions are prohibited (enforced "
                            f"server-side; the UI cannot bypass this)")
        admitted = [e for e in r.list_evidence_for_case(case_id)
                    if e.gate_verdict is GateVerdict.PASS]
        if action == "FIGHT" and not admitted:
            return err(409, "FIGHT requires at least one gate-admitted "
                            "evidence item; none exists on this case")

        r.append_audit(case_id, "HUMAN_APPROVED", {
            "action": action, "actor_name": actor_name,
            "hours_left": round(hours, 1),
            "admitted_evidence": [e.id for e in admitted]})
        bundle = {"human_approval": {"actor": actor_name, "action": action},
                  "evidence": [e.id for e in admitted]}
        result = execute_action(
            r, SimulatorAdapter(r), case_id=case_id, dispute_id=dispute.id,
            action_type="contest" if action == "FIGHT" else "accept",
            payload=bundle, actor=Actor.HUMAN)
        if not result.duplicate:
            r.update_case_state(case_id, CaseState.ACTED)
            r.update_case_state(case_id, CaseState.CLOSED)
            r.append_audit(case_id, "CASE_CLOSED", {
                "action": result.action.type, "actor": "human",
                "dispute_status": r.get_dispute(dispute.id).status.value})
        return jsonify({"case_id": case_id, "duplicate": result.duplicate,
                        "action": result.action.type,
                        "state": r.get_case(case_id).state.value,
                        "response": result.response})

    @app.post("/cases/<case_id>/reject")
    def reject(case_id):
        r = repo()
        case = r.get_case(case_id)
        if case is None:
            return err(404, f"no case {case_id}")
        if case.state is not CaseState.ESCALATED:
            return err(409, f"case is '{case.state.value}' — only escalated "
                            f"cases can be rejected")
        body = request.get_json(silent=True) or {}
        actor_name = (body.get("actor") or "").strip()
        reason = (body.get("reason") or "").strip()
        if not actor_name or not reason:
            return err(400, "actor and reason are required")
        r.append_audit(case_id, "CASE_REJECTED", {
            "actor_name": actor_name, "reason": reason,
            "note": "human closed the case without any money action"})
        r.update_case_state(case_id, CaseState.CLOSED)
        return jsonify({"case_id": case_id, "state": "closed",
                        "money_action": None})

    # -- frontend --------------------------------------------------------------------------------

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    return app
