"""Stage-10 tests: the API boundary.

Seeds a small demo world by running real orchestrator cases (one closed
fight, one pincode escalation, one accept, one ambiguous escalation, one
expired escalation), then exercises every endpoint plus the full human
approval matrix and the security boundaries.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from app import datagen
from app.ai.client import StubAIClient
from app.api import create_app
from app.datagen import generate
from app.orchestrator import Orchestrator
from app.policy.playbooks import load_playbooks
from app.store.models import CaseState, Dispute, ReasonCode
from app.store.repo import Repository
from app.tools.payments_adapter import SimulatorAdapter


class ApiTestBase(unittest.TestCase):
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
        self.db = Path(self.dbdir.name) / "world.db"
        shutil.copy(self.data / "dataset.db", self.db)
        self.repo = Repository(self.db)
        self.addCleanup(self.repo.close)
        self.addCleanup(self.dbdir.cleanup)
        orch = Orchestrator(self.repo, SimulatorAdapter(self.repo),
                            ai_client=StubAIClient(), playbooks=self.pb,
                            now=self.sim_now, sleep=lambda s: None)
        self.seeded = {}
        for label, scenario, mvp in (("closed_fight", datagen.HINGLISH, True),
                                     ("escalated_pin", datagen.CONFLICT_PIN, True),
                                     ("accepted", datagen.HOPELESS, True),
                                     ("escalated_ambiguous", datagen.AMBIGUOUS, False)):
            did = self._pick(scenario, mvp)
            res = orch.process_event({"event": "dispute.created",
                                      "dispute_id": did})
            self.seeded[label] = res.case.id
        # expired escalation for the deadline-safety matrix
        self.repo.add_dispute(Dispute(
            "disp_expired", "pay_0001",
            self.repo.get_order_by_payment("pay_0001").amount,
            ReasonCode.GOODS_NOT_RECEIVED,
            (self.sim_now - timedelta(hours=3)).isoformat(timespec="seconds")))
        res = orch.process_event({"event": "dispute.created",
                                  "dispute_id": "disp_expired"})
        self.seeded["escalated_expired"] = res.case.id

        app = create_app(self.db, data_dir=self.data,
                         eval_metrics_path=Path(__file__).resolve()
                         .parents[2] / "evals" / "metrics.json")
        app.testing = True
        self.client = app.test_client()

    def _pick(self, scenario, mvp_only=True):
        for did in self.split["dev"]:
            if self.gt[did]["scenario"] != scenario:
                continue
            if mvp_only and (self.repo.get_dispute(did).reason_code.value
                             not in self.pb.reason_codes):
                continue
            return did
        self.fail(scenario)


class TestEndpoints(ApiTestBase):
    def test_health(self):
        body = self.client.get("/health").get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["clock_mode"], "pinned_to_synthetic_world")
        self.assertGreaterEqual(body["counts"]["cases"], 5)

    def test_webhook_creates_and_runs_case(self):
        did = self._pick(datagen.CLEAN)
        resp = self.client.post("/webhooks/dispute", json={
            "event": "dispute.created", "dispute_id": did})
        self.assertEqual(resp.status_code, 201)
        body = resp.get_json()
        self.assertEqual(body["state"], "closed")
        # appears in the queue with full row data
        queue = self.client.get("/cases").get_json()["cases"]
        row = next(c for c in queue if c["case_id"] == body["case_id"])
        self.assertEqual(row["decision"], "FIGHT")
        self.assertGreater(row["amount"], 0)

    def test_invalid_webhook_rejected(self):
        for bad in ({}, {"event": "dispute.created", "dispute_id": "ghost"}):
            self.assertEqual(
                self.client.post("/webhooks/dispute", json=bad).status_code, 400)

    def test_case_queue_sorted_and_filterable(self):
        body = self.client.get("/cases").get_json()
        self.assertGreaterEqual(body["total"], 5)
        esc = self.client.get("/cases?state=escalated").get_json()["cases"]
        self.assertTrue(esc)
        self.assertTrue(all(c["escalated"] for c in esc))

    def test_case_detail_complete(self):
        body = self.client.get(
            f"/cases/{self.seeded['closed_fight']}").get_json()
        self.assertEqual(body["state"], "closed")
        self.assertIsNotNone(body["order"])
        self.assertEqual(body["decision_math"]["action"], "FIGHT")
        self.assertIn("ev_fight", body["decision_math"])
        self.assertIn("[E", body["draft"]["text"])
        self.assertTrue(body["draft"]["display_map"])
        self.assertTrue(body["audit_chain"]["valid"])
        self.assertEqual(body["execution"]["type"], "contest")
        self.assertEqual(body["allowed_human_actions"], [])
        self.assertEqual(
            self.client.get("/cases/ghost").status_code, 404)

    def test_evidence_endpoint_with_live_checks(self):
        body = self.client.get(
            f"/cases/{self.seeded['escalated_pin']}/evidence").get_json()
        by_key = {e["key"]: e for e in body["evidence"]}
        bad = by_key["address_match"]
        self.assertEqual(bad["verdict"], "FAIL")
        self.assertIn("pincode mismatch", bad["fail_reason"])
        self.assertTrue(bad["checks"], "live gate replay must populate checks")
        failed_checks = [c for c in bad["checks"] if not c["passed"]]
        self.assertTrue(any("pincode" in (c["detail"] or "")
                            for c in failed_checks))
        good = by_key["pod"]
        self.assertEqual(good["verdict"], "PASS")
        self.assertTrue(all(c["passed"] for c in good["checks"]))
        self.assertIn(good["quoted_span"].split(":")[0], ("Delivered",))

    def test_audit_timeline_and_chain(self):
        body = self.client.get(
            f"/cases/{self.seeded['closed_fight']}/audit").get_json()
        steps = [e["step"] for e in body["entries"]]
        self.assertIn("DECISION_MADE", steps)
        self.assertIn("ACTION_SUBMITTED", steps)
        self.assertTrue(body["chain"]["valid"])
        self.assertEqual(body["chain"]["entries"], len(body["entries"]))

    def test_metrics_from_committed_artifact_with_coverage_gaps(self):
        body = self.client.get("/metrics").get_json()
        ev = body["evaluation"]
        self.assertEqual(ev["cases_evaluated"], 40)
        self.assertEqual(ev["deadline_compliance"]["violations"], 0)
        self.assertTrue(body["coverage_gaps"])
        for code, gap in body["coverage_gaps"].items():
            self.assertGreater(gap["cases"], 0)
            self.assertGreater(gap["amount_at_risk"], 0)
            self.assertIn("playbook", gap["needs"])


class TestHumanApproval(ApiTestBase):
    def approve(self, case_id, action="FIGHT", actor="reviewer-1"):
        return self.client.post(f"/cases/{case_id}/approve",
                                json={"action": action, "actor": actor})

    def test_valid_fight_approval_executes_and_closes(self):
        cid = self.seeded["escalated_pin"]      # has admitted awb+pod evidence
        resp = self.approve(cid)
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertFalse(body["duplicate"])
        self.assertEqual(body["state"], "closed")
        audit = self.client.get(f"/cases/{cid}/audit").get_json()
        steps = [e["step"] for e in audit["entries"]]
        self.assertIn("HUMAN_APPROVED", steps)
        self.assertIn("ACTION_SUBMITTED", steps)
        self.assertTrue(audit["chain"]["valid"])
        submitted = next(e["payload"] for e in audit["entries"]
                         if e["step"] == "ACTION_SUBMITTED")
        self.assertEqual(submitted["actor"], "human")

    def test_duplicate_approval_is_idempotent(self):
        cid = self.seeded["escalated_pin"]
        first = self.approve(cid).get_json()
        self.assertFalse(first["duplicate"])
        # case is now closed -> second click is rejected as non-escalated,
        # and even a crafted replay against the executor cannot double-act
        second = self.approve(cid)
        self.assertEqual(second.status_code, 409)
        r = Repository(self.db)
        n = r.conn.execute("SELECT COUNT(*) c FROM actions").fetchone()["c"]
        r.close()
        self.assertEqual(n, 3)   # closed_fight + accepted + this approval

    def test_expired_case_cannot_be_approved_server_side(self):
        resp = self.approve(self.seeded["escalated_expired"])
        self.assertEqual(resp.status_code, 409)
        self.assertIn("deadline passed", resp.get_json()["error"])
        r = Repository(self.db)
        self.assertIsNone(r.get_action_by_idempotency_key("disp_expired"))
        r.close()

    def test_fight_without_admitted_evidence_refused(self):
        cid = self.seeded["escalated_ambiguous"]   # escalated before extraction
        resp = self.approve(cid, action="FIGHT")
        self.assertEqual(resp.status_code, 409)
        self.assertIn("admitted evidence", resp.get_json()["error"])

    def test_validation_matrix(self):
        self.assertEqual(self.approve("ghost").status_code, 404)
        self.assertEqual(
            self.approve(self.seeded["closed_fight"]).status_code, 409)
        self.assertEqual(
            self.approve(self.seeded["escalated_pin"],
                         action="REFUND").status_code, 400)
        resp = self.client.post(
            f"/cases/{self.seeded['escalated_pin']}/approve",
            json={"action": "FIGHT"})
        self.assertEqual(resp.status_code, 400)   # actor required

    def test_reject_closes_without_money(self):
        cid = self.seeded["escalated_ambiguous"]
        resp = self.client.post(f"/cases/{cid}/reject", json={
            "actor": "reviewer-1", "reason": "duplicate of an offline ticket"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["state"], "closed")
        audit = self.client.get(f"/cases/{cid}/audit").get_json()
        rejected = next(e["payload"] for e in audit["entries"]
                        if e["step"] == "CASE_REJECTED")
        self.assertEqual(rejected["actor_name"], "reviewer-1")
        self.assertTrue(audit["chain"]["valid"])
        r = Repository(self.db)
        case = r.get_case(cid)
        did = case.dispute_id
        self.assertIsNone(r.get_action_by_idempotency_key(did))
        r.close()
        # rejection requires actor + reason
        self.assertEqual(self.client.post(
            f"/cases/{self.seeded['escalated_expired']}/reject",
            json={"actor": "x"}).status_code, 400)


class TestSecurityBoundaries(ApiTestBase):
    def test_no_direct_adapter_action_path_in_api(self):
        import inspect
        from app import api as api_mod
        src = inspect.getsource(api_mod)
        self.assertNotIn("contest_dispute(", src)
        self.assertNotIn("accept_dispute(", src)
        self.assertIn("execute_action(", src)     # only via the executor

    def test_ai_package_cannot_import_api(self):
        for py in (Path("app/ai").glob("*.py")):
            self.assertNotIn("api", [m.split(".")[-1] for m in
                                     py.read_text().split()
                                     if m.startswith(("from", "import"))])
        # (the strong version is the Stage-6 AST test; this is a smoke check)

    def test_no_secrets_in_responses(self):
        for path in ("/health", "/cases",
                     f"/cases/{self.seeded['closed_fight']}",
                     f"/cases/{self.seeded['closed_fight']}/audit", "/metrics"):
            text = self.client.get(path).get_data(as_text=True).lower()
            for banned in ("key_secret", "anthropic_api_key", "sk-live"):
                self.assertNotIn(banned, text, path)

    def test_frontend_exists_and_uses_real_endpoints(self):
        from app.api import FRONTEND_DIR
        js = (FRONTEND_DIR / "app.js").read_text()
        for endpoint in ("/cases", "/metrics", "/approve", "/reject",
                         "/evidence", "/audit"):
            self.assertIn(endpoint, js)
        self.assertNotIn("contest_dispute", js)
        html = (FRONTEND_DIR / "index.html").read_text()
        self.assertIn("app.js", html)
