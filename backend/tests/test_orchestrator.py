"""Stage-8 tests: the orchestrator end to end, on the real Stage-3 world.

One happy path and five failure paths, each asserting the audit trail, the
state machine, idempotency, and the absence/presence of money actions.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from app import datagen
from app.ai.client import StubAIClient
from app.audit.chain import verify_audit_chain
from app.datagen import generate
from app.orchestrator import Orchestrator, format_timeline
from app.policy.playbooks import load_playbooks
from app.store.models import Case, CaseState, Dispute, DisputeStatus, ReasonCode
from app.store.repo import Repository
from app.tools.payments_adapter import SimulatorAdapter


class OrchestratorTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name)
        generate(seed=42, out_dir=cls.out)
        cls.gt = json.loads((cls.out / "ground_truth.json").read_text())["labels"]
        cls.split = json.loads((cls.out / "split.json").read_text())
        cls.sim_now = datetime.fromisoformat(cls.split["sim_now"])
        cls.pb = load_playbooks()
        cls.master_db = cls.out / "dataset.db"

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        # each test mutates world state (dispute status, cases) -> fresh copy
        import shutil
        self.dbdir = tempfile.TemporaryDirectory()
        self.db = Path(self.dbdir.name) / "world.db"
        shutil.copy(self.master_db, self.db)
        self.repo = Repository(self.db)
        self.addCleanup(self.repo.close)
        self.addCleanup(self.dbdir.cleanup)
        self.sleeps: list[float] = []

    def orch(self, failures=None, now=None) -> Orchestrator:
        outcomes = {d: g["gt_outcome_if_fought"] for d, g in self.gt.items()}
        adapter = SimulatorAdapter(self.repo, outcomes=outcomes,
                                   failures=failures)
        return Orchestrator(self.repo, adapter, ai_client=StubAIClient(),
                            playbooks=self.pb, now=now or self.sim_now,
                            sleep=self.sleeps.append, backoff_base_s=1.0)

    def dev_dispute_id(self, scenario: str, mvp_only=True) -> str:
        for did in self.split["dev"]:
            if self.gt[did]["scenario"] != scenario:
                continue
            if mvp_only and (self.repo.get_dispute(did).reason_code.value
                             not in self.pb.reason_codes):
                continue
            return did
        self.fail(f"no dev dispute for {scenario}")

    def event(self, dispute_id: str) -> dict:
        return {"event": "dispute.created", "dispute_id": dispute_id,
                "arrival": self.split["sim_now"]}

    def steps(self, case_id: str) -> list[str]:
        return [e.step for e in self.repo.read_audit(case_id)]


class TestHappyPath(OrchestratorTestBase):
    def test_hinglish_dispute_end_to_end(self):
        did = self.dev_dispute_id(datagen.HINGLISH)
        result = self.orch().process_event(self.event(did))

        self.assertIs(result.final_state, CaseState.CLOSED)
        steps = self.steps(result.case.id)
        for expected in ("CASE_CREATED", "LINK_COMPLETED", "GATHER_STARTED",
                         "GATHER_COMPLETED", "EVIDENCE_EXTRACTED",
                         "EVIDENCE_ADMITTED", "DECISION_MADE", "DRAFT_CREATED",
                         "DRAFT_VALIDATED", "ACTION_SUBMITTED", "CASE_CLOSED"):
            self.assertIn(expected, steps)
        # ordering sanity: decision precedes drafting precedes action
        self.assertLess(steps.index("DECISION_MADE"), steps.index("DRAFT_CREATED"))
        self.assertLess(steps.index("DRAFT_VALIDATED"),
                        steps.index("ACTION_SUBMITTED"))

        # the simulated contest actually moved the dispute
        self.assertIs(self.repo.get_dispute(did).status,
                      DisputeStatus.UNDER_REVIEW)
        # decision + evidence persisted with verdicts
        self.assertTrue(self.repo.list_decisions_for_case(result.case.id))
        evidence = self.repo.list_evidence_for_case(result.case.id)
        self.assertTrue(evidence)
        self.assertTrue(all(e.gate_verdict is not None for e in evidence))
        # the audit chain over the whole run verifies
        report = verify_audit_chain(self.repo, result.case.id)
        self.assertTrue(report.valid, report.to_text())
        # the representment travelled inside the submitted bundle
        submitted = next(json.loads(e.payload_json)
                         for e in self.repo.read_audit(result.case.id)
                         if e.step == "ACTION_SUBMITTED")
        self.assertIn("representment", submitted["request"])
        self.assertIn("[E", submitted["request"]["representment"])

    def test_timeline_reconstruction(self):
        did = self.dev_dispute_id(datagen.CLEAN)
        result = self.orch().process_event(self.event(did))
        text = format_timeline(self.repo, result.case.id)
        self.assertIn("LINK_COMPLETED", text)
        self.assertIn("[SIMULATED]", text)
        self.assertIn("FIGHT via fight_ev_positive", text)

    def test_hopeless_case_accepts_and_closes(self):
        did = self.dev_dispute_id(datagen.HOPELESS)
        result = self.orch().process_event(self.event(did))
        self.assertIs(result.final_state, CaseState.CLOSED)
        self.assertIs(self.repo.get_dispute(did).status, DisputeStatus.ACCEPTED)
        self.assertNotIn("DRAFT_CREATED", self.steps(result.case.id))


class TestFailurePaths(OrchestratorTestBase):
    def test_1_pincode_mismatch_escalates_with_both_pincodes_no_money(self):
        did = self.dev_dispute_id(datagen.CONFLICT_PIN)
        result = self.orch().process_event(self.event(did))
        self.assertIs(result.final_state, CaseState.ESCALATED)
        s = result.escalation_summary
        self.assertIn("HUMAN REVIEW", s)
        self.assertIn("pincode mismatch", s)
        self.assertIn("No payment action was executed.", s)
        # both pincodes appear (from the gate's precise failure reason)
        import re
        self.assertGreaterEqual(len(set(re.findall(r"\b\d{6}\b", s))), 2)
        self.assertIn("EVIDENCE_REJECTED", self.steps(result.case.id))
        self.assertIsNone(self.repo.get_action_by_idempotency_key(did))
        self.assertTrue(verify_audit_chain(self.repo, result.case.id).valid)

    def test_2_low_ai_confidence_escalates_without_guessing(self):
        did = self.dev_dispute_id(datagen.AMBIGUOUS, mvp_only=False)
        # ambiguous disputes carry an unresolvable payment_id + twin orders
        result = self.orch().process_event(self.event(did))
        self.assertIs(result.final_state, CaseState.ESCALATED)
        self.assertIn("never guesses", result.escalation_summary)
        self.assertIn("candidate", result.escalation_summary)
        self.assertIn("AI reasoning:", result.escalation_summary)
        payload = next(json.loads(e.payload_json)
                       for e in self.repo.read_audit(result.case.id)
                       if e.step == "CASE_ESCALATED")
        self.assertLess(payload["confidence"], 0.85)
        self.assertFalse(payload["money_action_taken"])
        # case never got past linking
        self.assertNotIn("GATHER_STARTED", self.steps(result.case.id))

    def test_3_api_failure_retries_then_escalates_same_key_no_duplicate(self):
        did = self.dev_dispute_id(datagen.CLEAN)
        result = self.orch(failures={did: "always"}).process_event(self.event(did))
        self.assertIs(result.final_state, CaseState.ESCALATED)
        self.assertEqual(self.sleeps, [1.0, 2.0])       # exponential backoff
        steps = self.steps(result.case.id)
        self.assertEqual(steps.count("ACTION_FAILED"), 3)
        self.assertIsNone(self.repo.get_action_by_idempotency_key(did))
        self.assertIn("idempotency key: " + did, result.escalation_summary)
        self.assertIn("retries: 3", result.escalation_summary)
        payload = next(json.loads(e.payload_json)
                       for e in self.repo.read_audit(result.case.id)
                       if e.step == "CASE_ESCALATED")
        self.assertTrue(payload["has_draft"])          # prepared bundle survives

    def test_3b_transient_failure_then_recovery_single_action(self):
        did = self.dev_dispute_id(datagen.CLEAN)
        result = self.orch(failures={did: 1}).process_event(self.event(did))
        self.assertIs(result.final_state, CaseState.CLOSED)
        steps = self.steps(result.case.id)
        self.assertEqual(steps.count("ACTION_FAILED"), 1)
        self.assertEqual(steps.count("ACTION_SUBMITTED"), 1)
        self.assertEqual(self.sleeps, [1.0])
        n = self.repo.conn.execute(
            "SELECT COUNT(*) c FROM actions WHERE idempotency_key = ?",
            (did,)).fetchone()["c"]
        self.assertEqual(n, 1)

    def test_4_duplicate_webhook_one_case_one_workflow_one_action(self):
        did = self.dev_dispute_id(datagen.CLEAN)
        orch = self.orch()
        first = orch.process_event(self.event(did))
        second = orch.process_event(self.event(did))
        self.assertEqual(first.case.id, second.case.id)
        self.assertIs(second.final_state, CaseState.CLOSED)
        steps = self.steps(first.case.id)
        self.assertIn("WEBHOOK_DUPLICATE", steps)
        self.assertIn("RUN_REFUSED", steps)             # workflow not restarted
        self.assertEqual(steps.count("ACTION_SUBMITTED"), 1)
        self.assertEqual(steps.count("CASE_CREATED"), 1)
        cases = self.repo.conn.execute(
            "SELECT COUNT(*) c FROM cases WHERE dispute_id = ?",
            (did,)).fetchone()["c"]
        self.assertEqual(cases, 1)

    def test_5_expired_deadline_blocks_everything(self):
        self.repo.add_dispute(Dispute(
            "disp_expired", "pay_0001",
            self.repo.get_order_by_payment("pay_0001").amount,
            ReasonCode.GOODS_NOT_RECEIVED,
            (self.sim_now - timedelta(hours=5)).isoformat(timespec="seconds")))
        result = self.orch().process_event(self.event("disp_expired"))
        self.assertIs(result.final_state, CaseState.ESCALATED)
        self.assertIn("deadline passed", result.escalation_summary)
        self.assertIn("Hours remaining: 0", result.escalation_summary)
        steps = self.steps(result.case.id)
        for banned in ("ACTION_SUBMITTED", "ACTION_FAILED", "LINK_COMPLETED"):
            self.assertNotIn(banned, steps)
        self.assertIsNone(self.repo.get_action_by_idempotency_key("disp_expired"))

    def test_deadline_kill_switch_before_decide(self):
        # a dispute with <24h left never reaches a money action autonomously
        for did in self.split["dev"]:
            g = self.gt[did]
            if (g["scenario"] == datagen.DELAYED
                    and g["hours_left_at_sim_now"] < 24
                    and self.repo.get_dispute(did).reason_code.value
                    in self.pb.reason_codes):
                result = self.orch().process_event(self.event(did))
                self.assertIs(result.final_state, CaseState.ESCALATED)
                self.assertIn("kill-switch", result.escalation_summary)
                self.assertIsNone(self.repo.get_action_by_idempotency_key(did))
                return
        self.skipTest("no <24h delayed dispute in dev split")

    def test_unsupported_reason_code_escalates(self):
        did = self.dev_dispute_id(datagen.PARTIAL_REFUND, mvp_only=False)
        self.assertNotIn(self.repo.get_dispute(did).reason_code.value,
                         self.pb.reason_codes)
        result = self.orch().process_event(self.event(did))
        self.assertIs(result.final_state, CaseState.ESCALATED)
        self.assertIn("unsupported reason code", result.escalation_summary)


class TestOrchestratorIntegrity(OrchestratorTestBase):
    def test_invalid_events_rejected(self):
        orch = self.orch()
        for bad in ({}, {"event": "x"}, {"event": "dispute.created"},
                    {"event": "dispute.created", "dispute_id": "disp_ghost"}):
            with self.assertRaises((ValueError, KeyError)):
                orch.handle_event(bad)

    def test_terminal_cases_are_never_resumed(self):
        did = self.dev_dispute_id(datagen.CONFLICT_PIN)
        orch = self.orch()
        first = orch.process_event(self.event(did))
        self.assertIs(first.final_state, CaseState.ESCALATED)
        again = orch.run_case(first.case.id)
        self.assertIs(again.final_state, CaseState.ESCALATED)
        self.assertIn("RUN_REFUSED", self.steps(first.case.id))
        self.assertIsNone(self.repo.get_action_by_idempotency_key(did))

    def test_batch_over_dev_events_all_terminal_all_chains_valid(self):
        events = [json.loads(l) for l in
                  (self.out / "events.jsonl").read_text().splitlines() if l]
        dev = set(self.split["dev"])
        orch = self.orch()
        processed = 0
        for ev in events:
            if ev["dispute_id"] not in dev or processed >= 25:
                continue
            result = orch.process_event(ev)
            self.assertIn(result.final_state,
                          (CaseState.CLOSED, CaseState.ESCALATED))
            self.assertTrue(
                verify_audit_chain(self.repo, result.case.id).valid,
                result.case.id)
            processed += 1
        self.assertGreaterEqual(processed, 20)

    def test_orchestrator_module_contains_no_policy_math(self):
        """Boundary check: no EV formulas, caps, or completeness math in the
        orchestrator — those belong to policy."""
        import inspect
        from app import orchestrator as om
        src = inspect.getsource(om)
        for banned in ("p_win *", "ev_fight =", "completeness =",
                       "COMPLETENESS_FIGHT_FLOOR", "AUTO_ACCEPT_CAP"):
            self.assertNotIn(banned, src, banned)
