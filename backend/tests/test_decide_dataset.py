"""Stage-5 dataset tests: gate -> decision engine over the Stage-3 world.

Asserts per-scenario decision behavior on real generated data and full
agreement with ground truth on the dev split. Framing matters: with oracle
extraction and labels derived from the same policy caps, 100% agreement is a
CONSISTENCY check of the deterministic pipeline — the LLM stages will be the
first place genuine disagreement can appear, and the eval harness will
measure exactly that.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from app import datagen
from app.datagen import generate
from app.evals.decision_report import decide_dispute, run_decision_report
from app.policy.playbooks import load_playbooks
from app.store.models import DecisionAction
from app.store.repo import Repository


class DecisionDatasetBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name)
        generate(seed=42, out_dir=cls.out)
        cls.gt = json.loads((cls.out / "ground_truth.json").read_text())["labels"]
        cls.split = json.loads((cls.out / "split.json").read_text())
        cls.sim_now = datetime.fromisoformat(cls.split["sim_now"])
        cls.pb = load_playbooks()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.repo = Repository(self.out / "dataset.db")
        self.addCleanup(self.repo.close)

    def outcomes_for(self, scenario: str):
        for did in self.split["dev"]:
            if self.gt[did]["scenario"] != scenario:
                continue
            dispute = self.repo.get_dispute(did)
            outcome, skip = decide_dispute(self.repo, dispute, self.pb,
                                           self.sim_now)
            yield did, outcome, skip


class TestDecisionsOnDataset(DecisionDatasetBase):
    def test_full_dev_split_agreement_with_ground_truth(self):
        r = run_decision_report(self.out, "dev")
        self.assertEqual(r["disputes_in_split"], 80)
        self.assertEqual(r["decided"] + sum(r["skipped"].values()), 80)
        self.assertEqual(r["disagreements"], [],
                         "deterministic pipeline must reproduce ground truth")
        self.assertEqual(r["agreement_rate"], 1.0)
        self.assertGreater(r["decided"], 50)

    def test_clean_and_hinglish_fight(self):
        n = 0
        for did, outcome, skip in self.outcomes_for(datagen.CLEAN):
            if skip:                       # clean cases with deferred reasons
                self.assertIn("deferred reason code", skip, did)
                continue
            self.assertIs(outcome.action, DecisionAction.FIGHT, did)
            self.assertEqual(outcome.completeness, 1.0, did)
            n += 1
        for did, outcome, skip in self.outcomes_for(datagen.HINGLISH):
            self.assertIsNone(skip, did)
            self.assertIs(outcome.action, DecisionAction.FIGHT, did)
            n += 1
        self.assertGreater(n, 10)

    def test_pincode_mismatch_escalates_with_the_gate_reason_attached(self):
        found = 0
        for did, outcome, skip in self.outcomes_for(datagen.CONFLICT_PIN):
            self.assertIsNone(skip, did)
            self.assertIs(outcome.action, DecisionAction.ESCALATE, did)
            missing = dict(outcome.missing_required)
            self.assertIn("address_match", missing, did)
            self.assertIn("pincode mismatch", missing["address_match"], did)
            found += 1
        self.assertGreater(found, 0)

    def test_missing_pod_escalates_as_recoverable_never_accepts(self):
        for did, outcome, skip in self.outcomes_for(datagen.MISSING_POD):
            self.assertIsNone(skip, did)
            self.assertIs(outcome.action, DecisionAction.ESCALATE, did)
            self.assertNotEqual(outcome.rule_fired, "concede_hopeless", did)
            if any("recoverable" in r for r in outcome.reasons):
                continue
            self.fail(f"{did}: escalation should mention recoverability")

    def test_hopeless_unshipped_low_value_accepts(self):
        accepted = 0
        for did, outcome, skip in self.outcomes_for(datagen.HOPELESS):
            if skip:                        # fraud-coded hopeless are deferred
                continue
            self.assertIs(outcome.action, DecisionAction.ACCEPT, did)
            self.assertEqual(outcome.rule_fired, "concede_hopeless", did)
            accepted += 1
        self.assertGreater(accepted, 0)

    def test_delayed_split_by_kill_switch(self):
        for did, outcome, skip in self.outcomes_for(datagen.DELAYED):
            self.assertIsNone(skip, did)
            hours = self.gt[did]["hours_left_at_sim_now"]
            if hours < 24:
                self.assertIs(outcome.action, DecisionAction.ESCALATE, did)
                self.assertEqual(outcome.rule_fired, "deadline_kill_switch", did)
            else:
                self.assertIs(outcome.action, DecisionAction.FIGHT, did)

    def test_ambiguous_orders_never_reach_the_decision_engine(self):
        for did, outcome, skip in self.outcomes_for(datagen.AMBIGUOUS):
            self.assertIsNone(outcome, did)
            self.assertEqual(skip, "order unresolvable by payment_id", did)

    def test_every_decision_carries_versioned_math(self):
        for did in self.split["dev"][:20]:
            dispute = self.repo.get_dispute(did)
            outcome, skip = decide_dispute(self.repo, dispute, self.pb,
                                           self.sim_now)
            if outcome is None:
                continue
            d = outcome.to_dict()
            self.assertEqual(d["thresholds_version"], "v1", did)
            self.assertEqual(d["playbook_version"], "v1", did)
            self.assertAlmostEqual(
                d["ev_fight"],
                round(d["p_win"] * dispute.amount - 500, 2), places=2, msg=did)
