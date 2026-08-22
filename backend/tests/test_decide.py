"""Stage-5 tests: the deterministic decision engine.

Focus areas: rule precedence, exact EV arithmetic at boundaries, monetary
caps, the deadline kill-switch, the concede-only-when-nothing-to-fight-with
rule, and audit-ready math recording.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app.config import (
    AUTO_ACCEPT_CAP_INR,
    CONTEST_FEE_INR,
    ESCALATION_AMOUNT_CAP_INR,
    THRESHOLDS_VERSION,
)
from app.policy.decide import decide
from app.policy.gate import CheckResult, Verdict
from app.policy.playbooks import load_playbooks
from app.store.models import DecisionAction, Dispute, DisputeStatus, GateVerdict, ReasonCode

PB = load_playbooks()
GNR = PB.for_reason(ReasonCode.GOODS_NOT_RECEIVED)   # required: awb, pod, address_match
NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)


def dispute(amount=3499, hours_left=72.0):
    return Dispute(id="disp_x", payment_id="pay_x", amount=amount,
                   reason_code=ReasonCode.GOODS_NOT_RECEIVED,
                   respond_by=(NOW + timedelta(hours=hours_left)).isoformat(
                       timespec="seconds"),
                   status=DisputeStatus.OPEN)


def verdict(key, passed=True, reason=None, eid="E1"):
    status = GateVerdict.PASS if passed else GateVerdict.FAIL
    return Verdict(status=status, evidence_id=eid, evidence_key=key,
                   playbook_version=PB.version,
                   checks=(CheckResult("x", passed, reason),),
                   failure_reason=None if passed else reason)


def full_pass():
    return [verdict("awb", eid="E1"), verdict("pod", eid="E2"),
            verdict("address_match", eid="E3")]


def run(*, amount=3499, hours_left=72.0, verdicts=None, has_shipment=True,
        preconditions_ok=True):
    return decide(dispute=dispute(amount, hours_left), playbook=GNR,
                  playbook_version=PB.version,
                  verdicts=full_pass() if verdicts is None else verdicts,
                  now=NOW, has_shipment=has_shipment,
                  preconditions_ok=preconditions_ok)


class TestRulePrecedence(unittest.TestCase):
    def test_complete_evidence_fights_with_exact_math(self):
        out = run(amount=3499)
        self.assertIs(out.action, DecisionAction.FIGHT)
        self.assertEqual(out.rule_fired, "fight_ev_positive")
        self.assertEqual(out.completeness, 1.0)
        self.assertEqual(out.p_win, 0.85)                       # band 1.0
        self.assertAlmostEqual(out.ev_fight, 0.85 * 3499 - CONTEST_FEE_INR)
        self.assertAlmostEqual(out.ev_accept, -3499.0)
        self.assertEqual(out.thresholds_version, THRESHOLDS_VERSION)
        self.assertEqual(out.playbook_version, "v1")
        self.assertEqual(out.satisfied_required, ("awb", "pod", "address_match"))
        self.assertEqual(out.missing_required, ())

    def test_amount_over_cap_escalates_even_with_perfect_evidence(self):
        out = run(amount=ESCALATION_AMOUNT_CAP_INR + 1)
        self.assertIs(out.action, DecisionAction.ESCALATE)
        self.assertEqual(out.rule_fired, "amount_over_cap")
        self.assertIn(str(ESCALATION_AMOUNT_CAP_INR), out.reasons[0])

    def test_deadline_kill_switch(self):
        self.assertIs(run(hours_left=23.9).action, DecisionAction.ESCALATE)
        self.assertEqual(run(hours_left=23.9).rule_fired, "deadline_kill_switch")
        self.assertIs(run(hours_left=24.1).action, DecisionAction.FIGHT)

    def test_deadline_already_passed(self):
        out = run(hours_left=-2.0)
        self.assertIs(out.action, DecisionAction.ESCALATE)
        self.assertEqual(out.rule_fired, "deadline_passed")
        self.assertIn("prohibited", out.reasons[0])

    def test_precondition_failure_blocks_even_accept(self):
        out = run(amount=1500, verdicts=[], has_shipment=False,
                  preconditions_ok=False)
        self.assertIs(out.action, DecisionAction.ESCALATE)
        self.assertEqual(out.rule_fired, "precondition_failed")

    def test_precedence_order_amount_beats_kill_switch(self):
        out = run(amount=ESCALATION_AMOUNT_CAP_INR + 1, hours_left=10.0)
        self.assertEqual(out.rule_fired, "amount_over_cap")


class TestConcedeRule(unittest.TestCase):
    def test_hopeless_low_value_unshipped_is_accepted(self):
        out = run(amount=1500, verdicts=[], has_shipment=False)
        self.assertIs(out.action, DecisionAction.ACCEPT)
        self.assertEqual(out.rule_fired, "concede_hopeless")
        self.assertEqual(out.completeness, 0.0)

    def test_missing_proof_with_existing_shipment_escalates_not_accepts(self):
        """The panel-worthy distinction: nothing-to-fight-with vs recoverable."""
        out = run(amount=1500, verdicts=[], has_shipment=True)
        self.assertIs(out.action, DecisionAction.ESCALATE)
        self.assertEqual(out.rule_fired, "needs_human")
        self.assertTrue(any("recoverable" in r for r in out.reasons))
        self.assertEqual(len(out.missing_required), 3)
        self.assertTrue(all(why == "no candidate evidence was found"
                            for _, why in out.missing_required))

    def test_hopeless_but_above_accept_cap_escalates(self):
        out = run(amount=AUTO_ACCEPT_CAP_INR + 1, verdicts=[], has_shipment=False)
        self.assertIs(out.action, DecisionAction.ESCALATE)


class TestEvBoundary(unittest.TestCase):
    """EV(fight) > EV(accept)  <=>  (p_win+1)*amount > fee.
    At p_win 0.85, fee 500: amount 270 -> 499.5 (escalate), 271 -> 501.35 (fight)."""

    def test_uneconomical_tiny_dispute_escalates(self):
        out = run(amount=270)
        self.assertIs(out.action, DecisionAction.ESCALATE)
        self.assertEqual(out.rule_fired, "needs_human")
        self.assertTrue(any("uneconomical" in r for r in out.reasons))

    def test_just_over_the_ev_boundary_fights(self):
        out = run(amount=271)
        self.assertIs(out.action, DecisionAction.FIGHT)


class TestCompletenessSemantics(unittest.TestCase):
    def test_partial_evidence_escalates_with_precise_missing_reasons(self):
        verdicts = [verdict("awb", eid="E1"), verdict("pod", eid="E2"),
                    verdict("address_match", passed=False, eid="E3",
                            reason="pincode mismatch: POD shows delivery to "
                                   "560083, order address is 560038")]
        out = run(verdicts=verdicts)
        self.assertIs(out.action, DecisionAction.ESCALATE)
        self.assertAlmostEqual(out.completeness, 2 / 3)
        self.assertEqual(out.p_win, 0.10)          # 2/3 < 0.75 band
        missing = dict(out.missing_required)
        self.assertIn("pincode mismatch", missing["address_match"])
        self.assertTrue(any("missing required 'address_match'" in r
                            for r in out.reasons))

    def test_duplicate_pass_counts_once_and_fail_beside_pass_still_satisfies(self):
        verdicts = full_pass() + [verdict("pod", eid="E4"),
                                  verdict("pod", passed=False, eid="E5",
                                          reason="duplicate evidence")]
        out = run(verdicts=verdicts)
        self.assertEqual(out.completeness, 1.0)
        self.assertIs(out.action, DecisionAction.FIGHT)

    def test_optional_evidence_does_not_change_completeness(self):
        verdicts = full_pass() + [verdict("admission_email", eid="E9")]
        self.assertEqual(run(verdicts=verdicts).completeness, 1.0)


class TestAuditReadiness(unittest.TestCase):
    def test_to_dict_carries_the_full_math(self):
        d = run(amount=3499).to_dict()
        for key in ("action", "rule_fired", "completeness", "p_win", "ev_fight",
                    "ev_accept", "hours_left", "satisfied_required",
                    "missing_required", "reasons", "thresholds_version",
                    "playbook_version"):
            self.assertIn(key, d)

    def test_to_decision_persists_via_stage2_store(self):
        import tempfile
        from pathlib import Path
        from app.store.models import Case, Merchant, Order
        from app.store.repo import Repository
        out = run(amount=3499)
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository(Path(tmp) / "t.db")
            repo.add_merchant(Merchant("m_1", "M", 2000, 10000))
            repo.add_order(Order("ord_1", "m_1", "pay_x", 3499, "a@b.c", "addr 560001",
                                 "2026-08-01T00:00:00+00:00", "2026-08-04T00:00:00+00:00"))
            repo.add_dispute(dispute())
            repo.add_case(Case("case_1", "disp_x"))
            repo.add_decision(out.to_decision("dec_1", "case_1"))
            stored = repo.list_decisions_for_case("case_1")[0]
            self.assertIs(stored.action, DecisionAction.FIGHT)
            self.assertEqual(stored.p_win, out.p_win)
            self.assertEqual(stored.thresholds_version, THRESHOLDS_VERSION)
            repo.close()
