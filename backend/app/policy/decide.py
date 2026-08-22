"""The decision engine (spec §8 step 8, §30.3): FIGHT / ACCEPT / ESCALATE.

Pure, deterministic, zero LLM imports (AST-enforced with the rest of the
policy package). Consumes ONLY gate output and system-of-record facts —
never raw AI text. Every decision carries its complete math and the exact
rule that fired, so the audit log and the dashboard can replay it.

Rule ordering (first match wins) — deliberately mirrors the caps the dataset
ground truth was derived from, so evaluation is principled:

  1. deadline already passed            -> ESCALATE (acting is prohibited)
  2. amount > escalation cap            -> ESCALATE (humans own big money)
  3. hours left < kill-switch threshold -> ESCALATE (no last-minute autonomy)
  4. money precondition failed          -> ESCALATE (unreconciled amounts are
                                           a linking problem, not a case)
  5. concede: completeness <= ceiling AND amount <= auto-accept cap AND no
     shipment ever existed              -> ACCEPT (nothing to fight with;
     if a shipment exists but proof is missing, we ask the merchant instead)
  6. fight: completeness >= floor AND EV(fight) > EV(accept)  -> FIGHT
  7. otherwise                          -> ESCALATE, with precise missing-item
                                           reasons for the merchant email

EV model (integer-rupee inputs, float EV):
  EV(fight)  = p_win * amount - contest_fee     (fee burned iff we lose;
               conservative: charged on every fight)
  EV(accept) = -amount
  p_win comes from the reason code's versioned playbook bands, selected by
  completeness = satisfied required keys / required keys.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from ..config import (
    AUTO_ACCEPT_CAP_INR,
    COMPLETENESS_ACCEPT_CEILING,
    COMPLETENESS_FIGHT_FLOOR,
    CONTEST_FEE_INR,
    DEADLINE_ESCALATE_HOURS,
    ESCALATION_AMOUNT_CAP_INR,
    THRESHOLDS_VERSION,
)
from ..store.models import Decision, DecisionAction, Dispute
from .gate import Verdict
from .playbooks import ReasonPlaybook


@dataclass(frozen=True)
class DecisionOutcome:
    action: DecisionAction
    rule_fired: str                     # machine-readable name of the winning rule
    completeness: float
    p_win: float
    ev_fight: float
    ev_accept: float
    hours_left: float
    satisfied_required: tuple[str, ...]
    missing_required: tuple[tuple[str, str], ...]   # (key, why)
    reasons: tuple[str, ...]            # human-readable justification lines
    thresholds_version: str
    playbook_version: str

    def to_dict(self) -> dict:
        return {
            "action": self.action.value,
            "rule_fired": self.rule_fired,
            "completeness": round(self.completeness, 4),
            "p_win": self.p_win,
            "ev_fight": round(self.ev_fight, 2),
            "ev_accept": round(self.ev_accept, 2),
            "hours_left": round(self.hours_left, 1),
            "satisfied_required": list(self.satisfied_required),
            "missing_required": [list(m) for m in self.missing_required],
            "reasons": list(self.reasons),
            "thresholds_version": self.thresholds_version,
            "playbook_version": self.playbook_version,
        }

    def to_decision(self, decision_id: str, case_id: str) -> Decision:
        """Map to the §12 persistence model."""
        return Decision(
            id=decision_id, case_id=case_id, action=self.action,
            completeness=self.completeness, p_win=self.p_win,
            ev_fight=self.ev_fight, ev_accept=self.ev_accept,
            thresholds_version=self.thresholds_version,
        )


def _completeness(playbook: ReasonPlaybook, verdicts: Sequence[Verdict]
                  ) -> tuple[float, tuple[str, ...], tuple[tuple[str, str], ...]]:
    """A required key is satisfied iff at least one PASS verdict carries it.
    Duplicate PASSes count once; a FAIL alongside a PASS does not un-satisfy."""
    passed_keys = {v.evidence_key for v in verdicts if v.status.value == "PASS"}
    fail_reason_by_key: dict[str, str] = {}
    for v in verdicts:
        if v.status.value == "FAIL" and v.evidence_key not in fail_reason_by_key:
            fail_reason_by_key[v.evidence_key] = v.failure_reason or "failed"

    satisfied, missing = [], []
    for key in playbook.required_keys:
        if key in passed_keys:
            satisfied.append(key)
        elif key in fail_reason_by_key:
            missing.append((key, f"inadmissible: {fail_reason_by_key[key]}"))
        else:
            missing.append((key, "no candidate evidence was found"))
    total = len(playbook.required_keys)
    return (len(satisfied) / total if total else 0.0,
            tuple(satisfied), tuple(missing))


def _p_win(playbook: ReasonPlaybook, completeness: float) -> float:
    for band in playbook.p_win_bands:        # validated strictly descending
        if completeness >= band.min_completeness:
            return band.p_win
    return playbook.p_win_bands[-1].p_win    # unreachable: last band covers 0.0


def decide(*, dispute: Dispute, playbook: ReasonPlaybook, playbook_version: str,
           verdicts: Sequence[Verdict], now: datetime, has_shipment: bool,
           preconditions_ok: bool = True,
           auto_accept_cap: int = AUTO_ACCEPT_CAP_INR,
           escalation_cap: int = ESCALATION_AMOUNT_CAP_INR,
           contest_fee: int = CONTEST_FEE_INR) -> DecisionOutcome:
    """Deterministically choose FIGHT / ACCEPT / ESCALATE for one case."""
    respond_by = datetime.fromisoformat(dispute.respond_by)
    hours_left = (respond_by - now).total_seconds() / 3600.0
    completeness, satisfied, missing = _completeness(playbook, verdicts)
    p_win = _p_win(playbook, completeness)
    ev_fight = p_win * dispute.amount - contest_fee
    ev_accept = -float(dispute.amount)

    def outcome(action: DecisionAction, rule: str, *reasons: str) -> DecisionOutcome:
        return DecisionOutcome(
            action=action, rule_fired=rule, completeness=completeness,
            p_win=p_win, ev_fight=ev_fight, ev_accept=ev_accept,
            hours_left=hours_left, satisfied_required=satisfied,
            missing_required=missing, reasons=tuple(reasons),
            thresholds_version=THRESHOLDS_VERSION,
            playbook_version=playbook_version)

    def _missing_lines() -> list[str]:
        return [f"missing required '{key}': {why}" for key, why in missing]

    # 1. deadline already passed — acting is prohibited, a human must triage
    if hours_left <= 0:
        return outcome(DecisionAction.ESCALATE, "deadline_passed",
                       f"deadline passed {abs(hours_left):.1f}h ago "
                       f"({dispute.respond_by}) — submission is prohibited")

    # 2. big money is always human-approved
    if dispute.amount > escalation_cap:
        return outcome(DecisionAction.ESCALATE, "amount_over_cap",
                       f"disputed \u20b9{dispute.amount} exceeds the "
                       f"\u20b9{escalation_cap} autonomous cap — human approval required")

    # 3. deadline kill-switch: no last-minute autonomy
    if hours_left < DEADLINE_ESCALATE_HOURS:
        return outcome(DecisionAction.ESCALATE, "deadline_kill_switch",
                       f"only {hours_left:.1f}h left (< {DEADLINE_ESCALATE_HOURS}h "
                       f"kill-switch) — a human must handle last-minute cases")

    # 4. unreconciled money means the case file itself is suspect
    if not preconditions_ok:
        return outcome(DecisionAction.ESCALATE, "precondition_failed",
                       "case precondition failed: disputed amount does not "
                       "reconcile against order minus refunds")

    # 5. concede only when there is provably nothing to fight with
    if (completeness <= COMPLETENESS_ACCEPT_CEILING
            and dispute.amount <= auto_accept_cap and not has_shipment):
        return outcome(DecisionAction.ACCEPT, "concede_hopeless",
                       f"no shipment ever existed and no required evidence is "
                       f"admissible (completeness {completeness:.2f})",
                       f"\u20b9{dispute.amount} <= \u20b9{auto_accept_cap} "
                       f"auto-accept cap; conceding beats burning a "
                       f"\u20b9{contest_fee} fee on an unwinnable contest")

    # 6. fight when the evidence is there and the economics work
    if completeness >= COMPLETENESS_FIGHT_FLOOR and ev_fight > ev_accept:
        return outcome(DecisionAction.FIGHT, "fight_ev_positive",
                       f"required evidence admitted: {', '.join(satisfied)} "
                       f"(completeness {completeness:.2f} >= "
                       f"{COMPLETENESS_FIGHT_FLOOR})",
                       f"EV(fight) = {p_win} x \u20b9{dispute.amount} - "
                       f"\u20b9{contest_fee} = \u20b9{ev_fight:.0f} > "
                       f"EV(accept) = \u20b9{ev_accept:.0f}")

    # 7. everything else goes to a human, with actionable reasons
    reasons: list[str] = []
    if completeness < COMPLETENESS_FIGHT_FLOOR:
        reasons.append(f"evidence incomplete: completeness {completeness:.2f} "
                       f"< {COMPLETENESS_FIGHT_FLOOR} floor")
        reasons.extend(_missing_lines())
        if has_shipment:
            reasons.append("a shipment exists — the missing proof may be "
                           "recoverable from the merchant or courier")
    if completeness >= COMPLETENESS_FIGHT_FLOOR and ev_fight <= ev_accept:
        reasons.append(f"fighting is uneconomical: EV(fight) \u20b9{ev_fight:.0f} "
                       f"<= EV(accept) \u20b9{ev_accept:.0f} at p_win {p_win}")
    return outcome(DecisionAction.ESCALATE, "needs_human", *reasons)
