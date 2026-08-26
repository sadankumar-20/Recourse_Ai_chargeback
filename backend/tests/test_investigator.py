"""R2 tests: the agentic investigation loop.

The headline test: on a missing_pod dispute the fixed pipeline escalates
('missing required pod'), while the agent notices the gap, queries the
courier's tracking record, materializes the confirmation, and the UNCHANGED
gate and decision engine take it to FIGHT. Same world, same policy — only
the investigation differs.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from app import datagen
from app.ai.client import StubAIClient
from app.ai.investigator import (
    InvestigationContext,
    PlannerDecision,
    plan_next,
    validate_plan_output,
)
from app.ai.schemas import SchemaError
from app.audit.chain import verify_audit_chain
from app.datagen import generate
from app.investigation import run_investigation
from app.orchestrator import Orchestrator
from app.policy.playbooks import load_playbooks
from app.store.models import Case, CaseState, DecisionAction, Shipment
from app.store.repo import Repository
from app.tools.payments_adapter import SimulatorAdapter


class AgenticTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.data = Path(cls.tmp.name) / "data"
        generate(seed=42, out_dir=cls.data)
        cls.gt = json.loads((cls.data / "ground_truth.json").read_text())["labels"]
        cls.split = json.loads((cls.data / "split.json").read_text())
        cls.sim_now = datetime.fromisoformat(cls.split["sim_now"])
        cls.pb = load_playbooks()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.dbdir = tempfile.TemporaryDirectory()
        self.db = Path(self.dbdir.name) / "w.db"
        shutil.copy(self.data / "dataset.db", self.db)
        self.repo = Repository(self.db)
        self.addCleanup(self.repo.close)
        self.addCleanup(self.dbdir.cleanup)

    def dev_dispute(self, scenario, mvp_only=True):
        for did in self.split["dev"]:
            if self.gt[did]["scenario"] != scenario:
                continue
            if mvp_only and (self.repo.get_dispute(did).reason_code.value
                             not in self.pb.reason_codes):
                continue
            return did
        self.fail(scenario)

    def orch(self, mode):
        return Orchestrator(self.repo, SimulatorAdapter(self.repo),
                            ai_client=StubAIClient(), playbooks=self.pb,
                            now=self.sim_now, sleep=lambda s: None,
                            investigation_mode=mode)

    def investigate(self, did, **kw):
        dispute = self.repo.get_dispute(did)
        order = self.repo.get_order_by_payment(dispute.payment_id)
        case = Case(id=f"case_{did}", dispute_id=did)
        self.repo.add_case(case)
        self.repo.update_case_state(case.id, CaseState.LINKING)
        self.repo.update_case_state(case.id, CaseState.GATHERING)
        pb = self.pb.for_reason(dispute.reason_code)
        return case, run_investigation(self.repo, case, dispute, order, pb,
                                       StubAIClient(), **kw)


class TestPlanner(unittest.TestCase):
    def ctx(self):
        return InvestigationContext(
            dispute={"id": "d", "amount": 100, "reason_code":
                     "goods_not_received", "respond_by": "x",
                     "payment_id": "p"},
            order={"id": "ord_1", "customer_email": "a@b.c", "address": "x"},
            checklist=[{"key": "pod", "description": "", "required": True},
                       {"key": "awb", "description": "", "required": True}],
            tool_specs=[])

    def test_deterministic_and_reproducible(self):
        history = [{"tool": "get_shipments", "args": {"order_id": "ord_1"},
                    "ok": True, "data": {"shipments": []}, "summary": ""}]
        a = plan_next(self.ctx(), history, StubAIClient()).decision
        b = plan_next(self.ctx(), history, StubAIClient()).decision
        self.assertEqual(a.to_dict(), b.to_dict())

    def test_strategy_progression(self):
        ctx, history = self.ctx(), []
        d = plan_next(ctx, history, StubAIClient()).decision
        self.assertEqual((d.action, d.tool), ("tool", "get_shipments"))
        history.append({"tool": "get_shipments", "args": {}, "ok": True,
                        "data": {"shipments": [{"awb": "A1", "courier": "C",
                                                "pod_doc_id": None}]},
                        "summary": ""})
        d = plan_next(ctx, history, StubAIClient()).decision
        self.assertEqual(d.tool, "fetch_tracking")     # notices the gap
        history.append({"tool": "fetch_tracking", "args": {"awb": "A1"},
                        "ok": True, "data": {"status": "delivered"},
                        "summary": ""})
        d = plan_next(ctx, history, StubAIClient()).decision
        self.assertEqual(d.tool, "search_knowledge")   # R3: policy context
        self.assertIn("goods_not_received", d.args["query"])
        history.append({"tool": "search_knowledge", "args": d.args,
                        "ok": True, "data": {"results": []}, "summary": ""})
        d = plan_next(ctx, history, StubAIClient()).decision
        self.assertEqual(d.tool, "search_inbox")

    def test_needs_input_when_no_delivery_record_anywhere(self):
        ctx = self.ctx()
        history = [
            {"tool": "get_shipments", "args": {}, "ok": True,
             "data": {"shipments": [{"awb": "A1", "courier": "C",
                                     "pod_doc_id": None}]}, "summary": ""},
            {"tool": "fetch_tracking", "args": {"awb": "A1"}, "ok": True,
             "data": {"status": "in_transit"}, "summary": ""}]
        d = plan_next(ctx, history, StubAIClient()).decision
        self.assertEqual(d.action, "needs_input")
        self.assertIn("AWB A1", d.request_to_user)
        self.assertEqual(d.missing, ["pod"])

    def test_plan_output_validator(self):
        good = '{"action": "tool", "goal": "g", "tool": "get_order", "args": {}}'
        self.assertEqual(validate_plan_output(good)["tool"], "get_order")
        for bad in ('{"action": "decide", "goal": "g"}',
                    '{"action": "tool", "goal": "g"}',
                    '{"goal": "g"}',
                    '{"action": "complete", "goal": "g", "extra": 1}',
                    '{"action": "complete", "goal": ""}'):
            with self.assertRaises(SchemaError, msg=bad):
                validate_plan_output(bad)


class TestLoopLimits(AgenticTestBase):
    def test_budget_exhaustion_terminates_safely(self):
        did = self.dev_dispute(datagen.CLEAN)
        case, out = self.investigate(did, budget=2)
        self.assertEqual(out.termination, "BUDGET_EXHAUSTED")
        self.assertEqual(out.stats["tool_calls"], 2)
        self.assertTrue(verify_audit_chain(self.repo, case.id).valid)

    def test_iteration_exhaustion_terminates_safely(self):
        did = self.dev_dispute(datagen.CLEAN)
        case, out = self.investigate(did, max_iterations=1)
        self.assertEqual(out.termination, "ITERATIONS_EXHAUSTED")

    def test_no_progress_detection(self):
        class StuckPlanner:
            provider = "stub"
        import app.investigation as inv
        original = inv.plan_next
        from app.ai.investigator import PlanStep
        inv.plan_next = lambda ctx, history, client: PlanStep(
            PlannerDecision("tool", "loop forever", tool="get_order",
                            args={"order_id": "ord_0001"}), [])
        try:
            did = self.dev_dispute(datagen.CLEAN)
            case, out = self.investigate(did)
            self.assertEqual(out.termination, "NO_PROGRESS")
            self.assertLessEqual(out.stats["tool_calls"], 3)
        finally:
            inv.plan_next = original

    def test_invalid_tool_request_is_structured_not_fatal(self):
        from app.ai.investigator import PlanStep
        import app.investigation as inv
        original = inv.plan_next
        calls = {"n": 0}

        def flaky(ctx, history, client):
            calls["n"] += 1
            if calls["n"] == 1:
                return PlanStep(PlannerDecision(
                    "tool", "try a nonexistent tool",
                    tool="hack_database", args={}), [])
            return PlanStep(PlannerDecision("complete", "done"), [])
        inv.plan_next = flaky
        try:
            did = self.dev_dispute(datagen.CLEAN)
            case, out = self.investigate(did)
            self.assertEqual(out.termination, "SUFFICIENT_EVIDENCE")
            self.assertEqual(out.stats["invalid_tool_requests"], 1)
        finally:
            inv.plan_next = original


class TestHeadlineRecovery(AgenticTestBase):
    """The architectural difference, demonstrated on the real world."""

    def test_fixed_escalates_agentic_fights_same_missing_pod_dispute(self):
        did = self.dev_dispute(datagen.MISSING_POD)
        event = {"event": "dispute.created", "dispute_id": did}

        fixed = self.orch("fixed").process_event(event)
        self.assertIs(fixed.final_state, CaseState.ESCALATED)
        self.assertIn("pod", fixed.escalation_summary)

        # fresh copy of the same world for the agentic run
        self.repo.close()
        shutil.copy(self.data / "dataset.db", self.db)
        self.repo = Repository(self.db)

        agentic = self.orch("agentic").process_event(event)
        self.assertIs(agentic.final_state, CaseState.CLOSED)
        decision = self.repo.list_decisions_for_case(agentic.case.id)[-1]
        self.assertIs(decision.action, DecisionAction.FIGHT)

        steps = [e.step for e in self.repo.read_audit(agentic.case.id)]
        for expected in ("AGENT_PLAN", "TOOL_CALL", "AGENT_OBSERVATION",
                         "AGENT_COMPLETE", "EVIDENCE_ADMITTED",
                         "DECISION_MADE", "ACTION_SUBMITTED"):
            self.assertIn(expected, steps)
        tracking_calls = [json.loads(e.payload_json)
                          for e in self.repo.read_audit(agentic.case.id)
                          if e.step == "TOOL_CALL"
                          and json.loads(e.payload_json)["tool"]
                          == "fetch_tracking"]
        self.assertTrue(tracking_calls)
        # the courier confirmation was admitted by the UNCHANGED gate
        evidence = self.repo.list_evidence_for_case(agentic.case.id)
        pod = next(e for e in evidence if e.evidence_key == "pod")
        self.assertEqual(pod.gate_verdict.value, "PASS")
        self.assertIn("doc_track_", pod.source_doc_id)
        self.assertTrue(verify_audit_chain(self.repo, agentic.case.id).valid)

    def test_agentic_needs_input_when_courier_has_no_record(self):
        """Shipment stuck in transit, no POD anywhere -> structured ask."""
        did = self.dev_dispute(datagen.MISSING_POD)
        dispute = self.repo.get_dispute(did)
        order = self.repo.get_order_by_payment(dispute.payment_id)
        with self.repo.conn:
            self.repo.conn.execute(
                "UPDATE shipments SET status = 'in_transit' "
                "WHERE order_id = ?", (order.id,))
        result = self.orch("agentic").process_event(
            {"event": "dispute.created", "dispute_id": did})
        # R4 fulfilled the R2 deferral: the case now PAUSES for the merchant
        self.assertIs(result.final_state, CaseState.NEEDS_INPUT)
        self.assertIn("Upload the courier proof of delivery",
                      result.escalation_summary)
        payload = next(json.loads(e.payload_json)
                       for e in self.repo.read_audit(result.case.id)
                       if e.step == "AGENT_NEEDS_INPUT")
        self.assertIn("AWB", payload["request_to_user"])
        self.assertEqual(payload["missing"], ["pod"])


class TestAgenticIntegration(AgenticTestBase):
    def test_clean_dispute_end_to_end_decision_from_engine(self):
        did = self.dev_dispute(datagen.CLEAN)
        result = self.orch("agentic").process_event(
            {"event": "dispute.created", "dispute_id": did})
        self.assertIs(result.final_state, CaseState.CLOSED)
        payload = next(json.loads(e.payload_json)
                       for e in self.repo.read_audit(result.case.id)
                       if e.step == "DECISION_MADE")
        self.assertEqual(payload["rule_fired"], "fight_ev_positive")
        self.assertEqual(payload["thresholds_version"], "v1")

    def test_pincode_mismatch_still_escalates_under_agentic(self):
        """The gate's authority is mode-independent."""
        did = self.dev_dispute(datagen.CONFLICT_PIN)
        result = self.orch("agentic").process_event(
            {"event": "dispute.created", "dispute_id": did})
        self.assertIs(result.final_state, CaseState.ESCALATED)
        self.assertIn("pincode mismatch", result.escalation_summary)

    def test_agent_events_carry_no_reasoning_dumps(self):
        did = self.dev_dispute(datagen.CLEAN)
        case, out = self.investigate(did)
        for e in self.repo.read_audit(case.id):
            if e.step in ("AGENT_PLAN", "AGENT_OBSERVATION", "AGENT_COMPLETE"):
                payload = json.loads(e.payload_json)
                self.assertNotIn("reasoning", payload)
                self.assertNotIn("thoughts", payload)
                if "observation" in payload:
                    self.assertLessEqual(len(payload["observation"]), 210)


class TestLaneIsolation(unittest.TestCase):
    def test_investigator_module_purity(self):
        import ast
        src = Path("app/ai/investigator.py").read_text()
        for node in ast.walk(ast.parse(src)):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [("." * node.level) + (node.module or "")]
            for m in mods:
                self.assertNotIn("tools", m)
                self.assertNotIn("repo", m)
                self.assertNotIn("sqlite3", m)
                self.assertNotIn("executor", m)

    def test_no_lane_imports_the_runner(self):
        for pkg in ("app/ai", "app/policy", "app/tools"):
            for py in Path(pkg).glob("*.py"):
                self.assertNotIn("app.investigation",
                                 py.read_text(), py)
                self.assertNotIn("from ..investigation",
                                 py.read_text(), py)

    def test_runner_touches_no_money_surface(self):
        src = Path("app/investigation.py").read_text()
        for banned in ("execute_action", "contest_dispute", "accept_dispute",
                       "payments_adapter", "decide("):
            self.assertNotIn(banned, src, banned)
