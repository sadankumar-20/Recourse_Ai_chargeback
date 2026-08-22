"""Decision report: oracle extraction -> gate -> decision engine over a
generated world, plus agreement against ground truth.

Dev split only — the held-out 40 stay untouched until the final evaluation
stage. Ground truth is read HERE (the eval layer) and nowhere else; the
decision engine itself never sees labels.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from ..policy.decide import decide
from ..policy.gate import GateContext, admit_all, case_preconditions
from ..policy.playbooks import PlaybookError, load_playbooks
from ..store.repo import Repository
from .oracle import build_candidates


def decide_dispute(repo: Repository, dispute, playbooks, sim_now: datetime):
    """Run the full deterministic pipeline for one dispute.

    Returns (outcome, skip_reason). skip_reason is set when the pipeline
    cannot reach a decision: deferred reason code, or unresolvable order —
    both of which the future orchestrator escalates before deciding."""
    try:
        rp = playbooks.for_reason(dispute.reason_code)
    except PlaybookError:
        return None, f"deferred reason code: {dispute.reason_code.value}"
    order = repo.get_order_by_payment(dispute.payment_id)
    if order is None:
        return None, "order unresolvable by payment_id"

    candidates, _notes = build_candidates(repo, dispute,
                                          checklist_keys=tuple(rp.rules))
    shipments = repo.list_shipments_for_order(order.id)
    docs = {}
    for ship in shipments:
        if ship.pod_doc_id:
            docs[ship.pod_doc_id] = repo.get_document(ship.pod_doc_id)
    for row in repo.conn.execute(
            "SELECT id FROM documents WHERE source = ?",
            (f"mailbox:{order.customer_email}",)).fetchall():
        docs[row["id"]] = repo.get_document(row["id"])
    ctx = GateContext(dispute=dispute, order=order, shipments=shipments,
                      refunds=repo.list_refunds_for_order(order.id),
                      documents=docs, playbooks=playbooks, now=sim_now)
    verdicts = admit_all(candidates, ctx)
    preconditions_ok = all(c.passed for c in case_preconditions(ctx))
    outcome = decide(dispute=dispute, playbook=rp,
                     playbook_version=playbooks.version, verdicts=verdicts,
                     now=sim_now, has_shipment=bool(shipments),
                     preconditions_ok=preconditions_ok)
    return outcome, None


def run_decision_report(out_dir: str | Path, split_name: str = "dev") -> dict:
    out = Path(out_dir)
    repo = Repository(out / "dataset.db")
    try:
        split = json.loads((out / "split.json").read_text())
        gt = json.loads((out / "ground_truth.json").read_text())["labels"]
        sim_now = datetime.fromisoformat(split["sim_now"])
        playbooks = load_playbooks()

        actions: Counter = Counter()
        rules: Counter = Counter()
        skipped: Counter = Counter()
        agree = disagree = 0
        disagreements: list[dict] = []

        for dispute_id in split[split_name]:
            dispute = repo.get_dispute(dispute_id)
            outcome, skip = decide_dispute(repo, dispute, playbooks, sim_now)
            if outcome is None:
                skipped[skip] += 1
                continue
            actions[outcome.action.value] += 1
            rules[outcome.rule_fired] += 1
            expected = gt[dispute_id]["gt_correct_action"]
            if outcome.action.value == expected:
                agree += 1
            else:
                disagree += 1
                disagreements.append({
                    "dispute": dispute_id,
                    "scenario": gt[dispute_id]["scenario"],
                    "expected": expected, "got": outcome.action.value,
                    "rule": outcome.rule_fired,
                    "reasons": list(outcome.reasons)[:2],
                })

        decided = agree + disagree
        return {
            "split": split_name,
            "disputes_in_split": len(split[split_name]),
            "decided": decided,
            "skipped": dict(skipped),
            "actions": dict(actions),
            "rules_fired": dict(rules.most_common()),
            "agreement_with_ground_truth": agree,
            "disagreements": disagreements,
            "agreement_rate": round(agree / decided, 4) if decided else None,
            "playbook_version": playbooks.version,
        }
    finally:
        repo.close()


def format_decision_report(r: dict) -> str:
    lines = [
        f"Decision report over '{r['split']}' split "
        f"(playbook {r['playbook_version']})",
        f"Disputes: {r['disputes_in_split']}  decided: {r['decided']}  "
        f"skipped: {sum(r['skipped'].values())}",
        "",
        "Skipped (escalated pre-decision by the future orchestrator):",
    ]
    for why, n in r["skipped"].items():
        lines.append(f"  - {why}: {n}")
    lines += ["", "Actions: " + ", ".join(f"{k}={v}" for k, v in
                                          sorted(r["actions"].items())),
              "Rules fired: " + ", ".join(f"{k}={v}" for k, v in
                                          r["rules_fired"].items()),
              "",
              f"Agreement with ground truth: "
              f"{r['agreement_with_ground_truth']}/{r['decided']} "
              f"({(r['agreement_rate'] or 0) * 100:.1f}%)"]
    if r["disagreements"]:
        lines.append("Disagreements:")
        for d in r["disagreements"]:
            lines.append(f"  - {d['dispute']} [{d['scenario']}]: expected "
                         f"{d['expected']}, got {d['got']} via {d['rule']}")
    return "\n".join(lines)
