"""Vercel serverless entrypoint for the Recourse demo.

Serverless honesty: Vercel's filesystem is read-only except /tmp, which is
EPHEMERAL. So this entrypoint bootstraps a deterministic demo world into
/tmp on cold start (seed 42 + the curated demo cases, ~2s) and serves the
normal Flask app against it. Approvals and webhooks work while an instance
is warm; on a cold start the world rebuilds identically. This is a demo
deployment mode, not persistence — /health reports it.
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

TMP = Path("/tmp/recourse")
DATA = TMP / "data"
DB = TMP / "demo.db"


def _bootstrap() -> None:
    if DB.exists():
        return
    TMP.mkdir(parents=True, exist_ok=True)
    from app import datagen
    from app.ai.client import get_client
    from app.datagen import generate
    from app.orchestrator import Orchestrator
    from app.policy.playbooks import load_playbooks
    from app.store.repo import Repository
    from app.tools.payments_adapter import SimulatorAdapter

    generate(seed=42, out_dir=DATA)
    shutil.copy(DATA / "dataset.db", DB)

    split = json.loads((DATA / "split.json").read_text())
    gt = json.loads((DATA / "ground_truth.json").read_text())["labels"]
    repo = Repository(DB)
    pb = load_playbooks()
    orch = Orchestrator(
        repo,
        SimulatorAdapter(repo, outcomes={d: g["gt_outcome_if_fought"]
                                         for d, g in gt.items()}),
        ai_client=get_client(), playbooks=pb,
        now=datetime.fromisoformat(split["sim_now"]), sleep=lambda s: None,
        investigation_mode="agentic")   # R6: rich Investigation Ledgers
    wanted = (datagen.CLEAN, datagen.HINGLISH, datagen.CONFLICT_PIN,
              datagen.HOPELESS, datagen.AMBIGUOUS, datagen.DELAYED)
    for scenario in wanted:
        for did in split["dev"]:
            if gt[did]["scenario"] != scenario:
                continue
            reason = repo.get_dispute(did).reason_code.value
            if scenario != datagen.AMBIGUOUS and reason not in pb.reason_codes:
                continue
            orch.process_event({"event": "dispute.created", "dispute_id": did,
                                "arrival": split["sim_now"]})
            break

    # R6: one LIVE interactive case — courier blinded so the agent pauses in
    # needs_input and the deployed cockpit opens on the ask-panel demo
    # (upload a POD, resume, watch the unchanged gate admit it).
    from app.intake import submit_intake
    for did in split["dev"]:
        if gt[did]["scenario"] != datagen.MISSING_POD:
            continue
        d = repo.get_dispute(did)
        if d.reason_code.value not in pb.reason_codes \
                or repo.get_case_by_dispute(did):
            continue
        order = repo.get_order_by_payment(d.payment_id)
        with repo.conn:
            repo.conn.execute("UPDATE shipments SET status='in_transit' "
                              "WHERE order_id = ?", (order.id,))
        res = submit_intake(
            repo, f"The customer says they never received order "
                  f"#{order.id.removeprefix('ord_')}, but we dispatched it.",
            get_client(), now=datetime.fromisoformat(split["sim_now"]))
        orch.run_case(res.case.id)
        break
    repo.close()


_bootstrap()

from app.api import create_app  # noqa: E402

app = create_app(DB, data_dir=DATA,
                 eval_metrics_path=ROOT / "evals" / "metrics.json")
