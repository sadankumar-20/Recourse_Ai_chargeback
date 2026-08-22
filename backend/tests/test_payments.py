"""Stage-7 tests: execution lane — adapters, idempotency, safety, integration."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.audit.chain import verify_audit_chain
from app.store.models import (
    Actor,
    Case,
    Dispute,
    DisputeStatus,
    Merchant,
    Order,
    ReasonCode,
)
from app.store.repo import Repository
from app.tools.executor import execute_action
from app.tools.payments_adapter import (
    NotSupported,
    PaymentsError,
    RazorpayTestAdapter,
    SimulatorAdapter,
    TransientPaymentsError,
    get_payments_adapter,
)


class ExecutionTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "t.db"
        self.repo = Repository(self.db)
        self.addCleanup(self.repo.close)
        self.addCleanup(self.tmp.cleanup)
        self.repo.add_merchant(Merchant("m_1", "Shop", 2000, 10000))
        for n, amount in (("1", 3499), ("2", 5200)):
            self.repo.add_order(Order(
                f"ord_{n}", "m_1", f"pay_{n}", amount, "a@b.c",
                "12 MG Road, Bengaluru 560038",
                "2026-08-01T10:00:00+00:00", "2026-08-04T10:00:00+00:00"))
            self.repo.add_dispute(Dispute(
                f"disp_{n}", f"pay_{n}", amount,
                ReasonCode.GOODS_NOT_RECEIVED, "2026-08-27T12:00:00+00:00"))
            self.repo.add_case(Case(f"case_{n}", f"disp_{n}"))

    def sim(self, **kw) -> SimulatorAdapter:
        return SimulatorAdapter(self.repo,
                                outcomes={"disp_1": "won", "disp_2": "lost"},
                                **kw)


class TestSimulatorAdapter(ExecutionTestBase):
    def test_lookups_are_labeled_simulated(self):
        adapter = self.sim()
        pay = adapter.fetch_payment("pay_1")
        self.assertTrue(pay.simulated)
        self.assertEqual(pay.data["amount"], 3499)
        refunds = adapter.fetch_refunds("pay_1")
        self.assertEqual(refunds.data["refunds"], [])
        with self.assertRaises(PaymentsError):
            adapter.fetch_payment("pay_ghost")

    def test_contest_lifecycle_is_deterministic(self):
        adapter = self.sim()
        res = adapter.contest_dispute("disp_1", {"evidence": [1, 2]})
        self.assertEqual(res.data["status"], "under_review")
        self.assertIn("SIMULATED", res.data["note"])
        self.assertEqual(adapter.tick("disp_1").data["status"], "won")
        # fresh adapter instance, same injected outcomes, same world -> same path
        self.assertEqual(self.sim().dispute_status("disp_1").data["status"], "won")

    def test_accept_lifecycle(self):
        adapter = self.sim()
        self.assertEqual(adapter.accept_dispute("disp_2").data["status"],
                         "accepted")
        self.assertEqual(adapter.dispute_status("disp_2").data["status"],
                         "accepted")

    def test_fallback_outcome_is_stable(self):
        a = SimulatorAdapter._fallback_outcome("disp_x")
        for _ in range(5):
            self.assertEqual(SimulatorAdapter._fallback_outcome("disp_x"), a)

    def test_already_actioned_dispute_refuses_second_action(self):
        """Defense in depth even if the executor is bypassed."""
        adapter = self.sim()
        adapter.contest_dispute("disp_1", {})
        with self.assertRaises(PaymentsError) as cm:
            adapter.contest_dispute("disp_1", {})
        self.assertIn("at most one money action", str(cm.exception))
        with self.assertRaises(PaymentsError):
            adapter.accept_dispute("disp_1")

    def test_failure_injection(self):
        adapter = self.sim(failures={"disp_1": 1})
        with self.assertRaises(TransientPaymentsError) as cm:
            adapter.contest_dispute("disp_1", {})
        self.assertEqual(cm.exception.status, 503)
        self.assertEqual(adapter.contest_dispute("disp_1", {}).data["status"],
                         "under_review")
        always = self.sim(failures={"disp_2": "always"})
        for _ in range(3):
            with self.assertRaises(TransientPaymentsError):
                always.accept_dispute("disp_2")


class TestRazorpayTestAdapter(unittest.TestCase):
    def test_missing_credentials_fail_loudly_no_silent_fallback(self):
        with self.assertRaises(PaymentsError) as cm:
            RazorpayTestAdapter(key_id="", key_secret="")
        self.assertIn("RAZORPAY_KEY_ID", str(cm.exception))

    def test_dispute_lifecycle_is_honestly_not_supported(self):
        adapter = RazorpayTestAdapter(key_id="rzp_test_x", key_secret="s")
        with self.assertRaises(NotSupported) as cm:
            adapter.contest_dispute("disp_1", {})
        self.assertIn("simulator", str(cm.exception))
        with self.assertRaises(NotSupported):
            adapter.accept_dispute("disp_1")

    def test_provider_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository(Path(tmp) / "t.db")
            self.assertIsInstance(get_payments_adapter(repo, "simulator"),
                                  SimulatorAdapter)
            with self.assertRaises(PaymentsError):
                get_payments_adapter(repo, "razorpay_test")   # no creds in env
            with self.assertRaises(PaymentsError):
                get_payments_adapter(repo, "paypal")
            repo.close()


class TestExecutorIdempotency(ExecutionTestBase):
    def test_duplicate_contest_returns_original_and_audits_distinctly(self):
        adapter = self.sim()
        first = execute_action(self.repo, adapter, case_id="case_1",
                               dispute_id="disp_1", action_type="contest",
                               payload={"evidence": ["E1"]}, actor=Actor.AGENT)
        second = execute_action(self.repo, adapter, case_id="case_1",
                                dispute_id="disp_1", action_type="contest",
                                payload={"evidence": ["E1"]}, actor=Actor.AGENT)
        self.assertFalse(first.duplicate)
        self.assertTrue(second.duplicate)
        self.assertEqual(second.action.id, first.action.id)
        rows = self.repo.conn.execute(
            "SELECT COUNT(*) c FROM actions WHERE idempotency_key='disp_1'"
        ).fetchone()["c"]
        self.assertEqual(rows, 1)
        steps = [a.step for a in self.repo.read_audit("case_1")]
        self.assertEqual(steps, ["ACTION_SUBMITTED", "ACTION_DUPLICATE"])

    def test_duplicate_accept(self):
        adapter = self.sim()
        execute_action(self.repo, adapter, case_id="case_2",
                       dispute_id="disp_2", action_type="accept",
                       payload={}, actor=Actor.AGENT)
        dup = execute_action(self.repo, adapter, case_id="case_2",
                             dispute_id="disp_2", action_type="accept",
                             payload={}, actor=Actor.HUMAN)
        self.assertTrue(dup.duplicate)
        self.assertEqual(dup.action.type, "accept")

    def test_conflicting_second_action_returns_original_not_a_new_submission(self):
        """One money action per dispute EVER: accept-after-contest is refused
        by idempotency, and the audit trail proves what was attempted."""
        adapter = self.sim()
        execute_action(self.repo, adapter, case_id="case_1",
                       dispute_id="disp_1", action_type="contest",
                       payload={}, actor=Actor.AGENT)
        res = execute_action(self.repo, adapter, case_id="case_1",
                             dispute_id="disp_1", action_type="accept",
                             payload={}, actor=Actor.HUMAN)
        self.assertTrue(res.duplicate)
        self.assertEqual(res.action.type, "contest")     # the original stands
        dup_entry = json.loads(self.repo.read_audit("case_1")[-1].payload_json)
        self.assertEqual(dup_entry["attempted_action"], "accept")
        self.assertEqual(dup_entry["original_action_type"], "contest")

    def test_idempotency_survives_restart(self):
        adapter = self.sim()
        execute_action(self.repo, adapter, case_id="case_1",
                       dispute_id="disp_1", action_type="contest",
                       payload={}, actor=Actor.AGENT)
        self.repo.close()
        repo2 = Repository(self.db)                       # "restart"
        self.addCleanup(repo2.close)
        res = execute_action(repo2, SimulatorAdapter(repo2), case_id="case_1",
                             dispute_id="disp_1", action_type="contest",
                             payload={}, actor=Actor.AGENT)
        self.assertTrue(res.duplicate)

    def test_disputes_have_independent_idempotency(self):
        adapter = self.sim()
        r1 = execute_action(self.repo, adapter, case_id="case_1",
                            dispute_id="disp_1", action_type="contest",
                            payload={}, actor=Actor.AGENT)
        r2 = execute_action(self.repo, adapter, case_id="case_2",
                            dispute_id="disp_2", action_type="accept",
                            payload={}, actor=Actor.AGENT)
        self.assertFalse(r1.duplicate)
        self.assertFalse(r2.duplicate)


class TestExecutorSafety(ExecutionTestBase):
    def test_only_contest_and_accept_are_executable(self):
        for bad in ("ESCALATE", "FIGHT", "refund", "decide", ""):
            with self.assertRaises(ValueError):
                execute_action(self.repo, self.sim(), case_id="case_1",
                               dispute_id="disp_1", action_type=bad,
                               payload={}, actor=Actor.AGENT)

    def test_adapter_exposes_no_decision_capability(self):
        for adapter_cls in (SimulatorAdapter, RazorpayTestAdapter):
            for m in dir(adapter_cls):
                self.assertNotIn("decide", m.lower())
                self.assertNotIn("escalate", m.lower())

    def test_transient_failure_creates_no_action_and_is_audited(self):
        adapter = self.sim(failures={"disp_1": "always"})
        with self.assertRaises(TransientPaymentsError):
            execute_action(self.repo, adapter, case_id="case_1",
                           dispute_id="disp_1", action_type="contest",
                           payload={}, actor=Actor.AGENT)
        self.assertIsNone(self.repo.get_action_by_idempotency_key("disp_1"))
        entries = self.repo.read_audit("case_1")
        self.assertEqual(entries[-1].step, "ACTION_FAILED")
        self.assertEqual(json.loads(entries[-1].payload_json)["status"], 503)

    def test_secrets_are_redacted_from_audit(self):
        execute_action(self.repo, self.sim(), case_id="case_1",
                       dispute_id="disp_1", action_type="contest",
                       payload={"evidence": ["E1"],
                                "api_key": "sk-live-supersecret",
                                "headers": {"Authorization": "Bearer abc"}},
                       actor=Actor.AGENT)
        dumped = " ".join(e.payload_json for e in self.repo.read_audit("case_1"))
        self.assertNotIn("supersecret", dumped)
        self.assertNotIn("Bearer abc", dumped)
        self.assertIn("[REDACTED]", dumped)


class TestDecisionToExecutionIntegration(ExecutionTestBase):
    def test_decision_to_execution_to_verified_chain(self):
        from datetime import datetime, timezone
        from app.policy.decide import decide
        from app.policy.playbooks import load_playbooks
        from app.store.models import DecisionAction
        from tests.test_decide import full_pass   # reuse verdict fixtures

        pb = load_playbooks()
        dispute = self.repo.get_dispute("disp_1")
        outcome = decide(dispute=dispute,
                         playbook=pb.for_reason(dispute.reason_code),
                         playbook_version=pb.version, verdicts=full_pass(),
                         now=datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc),
                         has_shipment=True)
        self.assertIs(outcome.action, DecisionAction.FIGHT)
        self.repo.add_decision(outcome.to_decision("dec_1", "case_1"))

        res = execute_action(self.repo, self.sim(), case_id="case_1",
                             dispute_id="disp_1", action_type="contest",
                             payload={"evidence": ["E1", "E2", "E3"]},
                             actor=Actor.AGENT,
                             decision_meta=outcome.to_dict())
        self.assertFalse(res.duplicate)
        # the audit entry carries the decision math and policy versions
        submitted = json.loads(self.repo.read_audit("case_1")[-1].payload_json)
        self.assertEqual(submitted["decision"]["thresholds_version"], "v1")
        self.assertEqual(submitted["decision"]["rule_fired"], "fight_ev_positive")

        # duplicate attempt -> original result, chain still valid, provable
        execute_action(self.repo, self.sim(), case_id="case_1",
                       dispute_id="disp_1", action_type="contest",
                       payload={}, actor=Actor.AGENT)
        report = verify_audit_chain(self.repo, "case_1")
        self.assertTrue(report.valid, report.to_text())
        steps = [e.step for e in self.repo.read_audit("case_1")]
        self.assertEqual(steps.count("ACTION_SUBMITTED"), 1)
        self.assertEqual(steps.count("ACTION_DUPLICATE"), 1)
