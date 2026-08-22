"""Run the Admissibility Gate over a generated Stage-3 world and summarize.

Dev-split disputes only (the held-out 40 stay untouched — the do-not-tune rule
applies even to deterministic components, since gate behavior informs playbook
tuning). Extraction is the deterministic oracle, so every PASS/FAIL below is
attributable to the GATE and the DATA, not to any model.

Nothing here is manipulated to flatter the gate: the summary is derived by
running the real pipeline over the real generated artifacts.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from ..policy.gate import GateContext, admit_all
from ..policy.playbooks import PlaybookError, load_playbooks
from ..store.models import GateVerdict
from ..store.repo import Repository
from .oracle import build_candidates


def run_gate_report(out_dir: str | Path, split_name: str = "dev") -> dict:
    out = Path(out_dir)
    repo = Repository(out / "dataset.db")
    try:
        split = json.loads((out / "split.json").read_text())
        sim_now = datetime.fromisoformat(split["sim_now"])
        playbooks = load_playbooks()

        checked = passed = failed = 0
        no_playbook: Counter = Counter()
        failure_reasons: Counter = Counter()
        notes_counter: Counter = Counter()
        disputes_seen = 0

        for dispute_id in split[split_name]:
            dispute = repo.get_dispute(dispute_id)
            disputes_seen += 1
            try:
                rp = playbooks.for_reason(dispute.reason_code)
            except PlaybookError:
                no_playbook[dispute.reason_code.value] += 1
                continue

            candidates, notes = build_candidates(
                repo, dispute, checklist_keys=tuple(rp.rules))
            for n in notes:
                notes_counter[n.split(":")[0]] += 1
            if not candidates:
                continue
            order = repo.get_order_by_payment(dispute.payment_id)
            ctx = GateContext(
                dispute=dispute, order=order,
                shipments=repo.list_shipments_for_order(order.id),
                refunds=repo.list_refunds_for_order(order.id),
                documents={d.id: d for d in _docs_for(repo, order)},
                playbooks=playbooks, now=sim_now)
            for v in admit_all(candidates, ctx):
                checked += 1
                if v.status is GateVerdict.PASS:
                    passed += 1
                else:
                    failed += 1
                    failure_reasons[_bucket(v.failure_reason)] += 1

        return {
            "split": split_name,
            "disputes_in_split": disputes_seen,
            "disputes_deferred_no_playbook": dict(no_playbook),
            "evidence_checked": checked,
            "passed": passed,
            "failed": failed,
            "failure_reasons": dict(failure_reasons.most_common()),
            "extraction_notes": dict(notes_counter.most_common()),
            "playbook_version": playbooks.version,
        }
    finally:
        repo.close()


def _docs_for(repo: Repository, order) -> list:
    docs = []
    for ship in repo.list_shipments_for_order(order.id):
        if ship.pod_doc_id:
            d = repo.get_document(ship.pod_doc_id)
            if d:
                docs.append(d)
    rows = repo.conn.execute(
        "SELECT id FROM documents WHERE source = ?",
        (f"mailbox:{order.customer_email}",)).fetchall()
    docs.extend(repo.get_document(r["id"]) for r in rows)
    return docs


def _bucket(reason: str) -> str:
    for prefix, bucket in (
        ("pincode mismatch", "pincode mismatch"),
        ("AWB mismatch", "AWB mismatch"),
        ("quoted span not found", "quote not found"),
        ("amount mismatch", "amount mismatch"),
        ("timestamp incoherent", "timestamp incoherent"),
        ("duplicate evidence", "duplicate evidence"),
    ):
        if reason and reason.startswith(prefix):
            return bucket
    return (reason or "unknown").split(":")[0][:60]


def format_gate_report(r: dict) -> str:
    lines = [
        f"Gate report over '{r['split']}' split "
        f"(playbook {r['playbook_version']})",
        f"Disputes in split: {r['disputes_in_split']}",
        f"Deferred (no playbook yet): {r['disputes_deferred_no_playbook']}",
        "",
        f"Evidence candidates checked: {r['evidence_checked']}",
        f"Passed: {r['passed']}",
        f"Failed: {r['failed']}",
        "",
        "Top failure reasons:",
    ]
    for reason, n in r["failure_reasons"].items():
        lines.append(f"  - {reason}: {n}")
    if not r["failure_reasons"]:
        lines.append("  (none)")
    lines.append("")
    lines.append("Extraction gaps (oracle notes):")
    for note, n in r["extraction_notes"].items():
        lines.append(f"  - {note}: {n}")
    return "\n".join(lines)
