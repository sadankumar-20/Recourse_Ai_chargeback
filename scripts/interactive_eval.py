#!/usr/bin/env python3
"""R4 measurement: the interactive loop, on dev-split missing_pod cases with
the courier deliberately blinded (status=in_transit) so every case must ask.
For each: intake (natural language) -> NEEDS_INPUT -> simulated merchant
upload (a POD constructed from the merchant's own shipment/order records) ->
resume -> outcome. Writes evals/interactive_metrics.json (deterministic).
Held-out stays frozen for eval v2."""
import io
import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.api import create_app                              # noqa: E402
from app.audit.chain import verify_audit_chain              # noqa: E402
from app.policy.playbooks import load_playbooks             # noqa: E402
from app.store.repo import Repository                       # noqa: E402

DATA = ROOT / "data"
split = json.loads((DATA / "split.json").read_text())
gt = json.loads((DATA / "ground_truth.json").read_text())["labels"]
pb = load_playbooks()

with tempfile.TemporaryDirectory() as tmp:
    db = Path(tmp) / "w.db"
    shutil.copy(DATA / "dataset.db", db)
    repo = Repository(db)
    app = create_app(db, data_dir=DATA)
    app.testing = True
    c = app.test_client()
    m = {"cases": 0, "needs_input_raised": 0, "asks_specific": 0,
         "resolved_after_upload": 0, "resumed_to_closed": 0,
         "resumed_to_escalated": 0, "duplicate_uploads_deduped": 0,
         "actions_total": 0, "deadline_violations": 0, "chains_invalid": 0}
    for did in split["dev"]:
        if gt[did]["scenario"] != "missing_pod":
            continue
        d = repo.get_dispute(did)
        if d.reason_code.value not in pb.reason_codes:
            continue
        order = repo.get_order_by_payment(d.payment_id)
        with repo.conn:
            repo.conn.execute("UPDATE shipments SET status='in_transit' "
                              "WHERE order_id = ?", (order.id,))
        m["cases"] += 1
        created = c.post("/intake", json={
            "text": f"The customer says they never received order "
                    f"#{order.id.removeprefix('ord_')} but it was "
                    f"dispatched."}).get_json()
        case_id = created["case_id"]
        if created["state"] != "needs_input":
            continue
        m["needs_input_raised"] += 1
        req = created["needs_input"]
        ship = repo.list_shipments_for_order(order.id)[0]
        if ship.awb in (req["action"] or ""):
            m["asks_specific"] += 1
        delivered = (datetime.fromisoformat(ship.ship_date)
                     + timedelta(hours=60)).isoformat(timespec="seconds")
        pod = (f"PROOF OF DELIVERY\nCourier: {ship.courier}\n"
               f"AWB: {ship.awb}\nDelivered: {delivered}\n"
               f"Receiver: Merchant Records\nDelivery OTP verified: NO\n"
               f"Address: {order.address}\n")
        up = lambda: c.post(
            f"/cases/{case_id}/upload?kind=pod",
            data={"file": (io.BytesIO(pod.encode()), "pod.txt",
                           "text/plain")},
            content_type="multipart/form-data").get_json()
        first, dup = up(), up()
        if dup["duplicate"]:
            m["duplicate_uploads_deduped"] += 1
        state = c.post(f"/cases/{case_id}/resume").get_json()["state"]
        if state == "closed":
            m["resumed_to_closed"] += 1
            m["resolved_after_upload"] += 1
        elif state == "escalated":
            m["resumed_to_escalated"] += 1
        if not verify_audit_chain(repo, case_id).valid:
            m["chains_invalid"] += 1
    m["actions_total"] = repo.conn.execute(
        "SELECT COUNT(*) c FROM actions").fetchone()["c"]
    repo.close()

m["needs_input_resolution_rate"] = round(
    m["resolved_after_upload"] / max(1, m["needs_input_raised"]), 3)
(ROOT / "evals" / "interactive_metrics.json").write_text(
    json.dumps(m, indent=1))
for k, v in m.items():
    print(f"{k:<32} {v}")
print("-> evals/interactive_metrics.json")
