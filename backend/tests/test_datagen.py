"""Stage-3 tests: synthetic dataset generation.

Covers: deterministic generation (same seed => byte-identical world, different
seed => different world), record counts, reason-code coverage, split integrity
(sizes, disjointness, stratification), structural presence of every injected
imperfection, ground-truth completeness, referential consistency, and the
validator rejecting deliberately corrupted data.

The full world is generated once per class (setUpClass) to keep the suite
fast; the determinism test generates two more, smaller worlds.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import datagen
from app.datagen import DatasetError, generate, validate_dataset
from app.store.models import ReasonCode, Refund
from app.store.repo import Repository


def _world_fingerprint(out_dir: Path) -> str:
    """Canonical, ordered dump of every artifact — equality means identity."""
    conn = sqlite3.connect(out_dir / "dataset.db")
    conn.row_factory = sqlite3.Row
    parts = []
    for table in ("merchants", "orders", "refunds", "shipments", "documents",
                  "disputes"):
        rows = conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
        parts.append(json.dumps([dict(r) for r in rows], sort_keys=True))
    conn.close()
    parts.append((out_dir / "ground_truth.json").read_text())
    parts.append((out_dir / "split.json").read_text())
    parts.append((out_dir / "events.jsonl").read_text())
    return "\n".join(parts)


class DatasetTestBase(unittest.TestCase):
    """Generates the canonical seed-42 world once for all read-only tests."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name)
        cls.stats = generate(seed=42, out_dir=cls.out)
        cls.gt = json.loads((cls.out / "ground_truth.json").read_text())["labels"]
        cls.split = json.loads((cls.out / "split.json").read_text())
        cls.events = [json.loads(l) for l in
                      (cls.out / "events.jsonl").read_text().splitlines() if l]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def repo(self) -> Repository:
        r = Repository(self.out / "dataset.db")
        self.addCleanup(r.close)
        return r


class TestCountsAndSplit(DatasetTestBase):
    def test_expected_record_counts(self):
        self.assertEqual(self.stats["orders"], 800)
        self.assertEqual(self.stats["disputes"], 120)
        self.assertEqual(len(self.gt), 120)

    def test_all_reason_codes_present(self):
        repo = self.repo()
        rows = repo.conn.execute(
            "SELECT DISTINCT reason_code FROM disputes").fetchall()
        found = {r["reason_code"] for r in rows}
        self.assertEqual(found, {rc.value for rc in ReasonCode})

    def test_split_sizes_and_disjointness(self):
        dev, held = set(self.split["dev"]), set(self.split["held_out"])
        self.assertEqual(len(dev), 80)
        self.assertEqual(len(held), 40)
        self.assertFalse(dev & held, "dev and held-out must not overlap")
        self.assertEqual(dev | held, set(self.gt))

    def test_split_is_stratified_across_scenarios(self):
        """Every scenario appears in BOTH sets, so held-out covers all failure
        modes and the dev set can demo every case."""
        scen_of = {d: g["scenario"] for d, g in self.gt.items()}
        dev_scen = {scen_of[d] for d in self.split["dev"]}
        held_scen = {scen_of[d] for d in self.split["held_out"]}
        all_scen = set(scen_of.values())
        self.assertEqual(dev_scen, all_scen)
        self.assertEqual(held_scen, all_scen)

    def test_split_file_carries_do_not_tune_warning(self):
        self.assertIn("never tune", (self.out / "split.json").read_text().lower())


class TestImperfectionsPresent(DatasetTestBase):
    def test_scenario_quotas_met(self):
        for scen, quota in datagen.SCENARIO_QUOTAS.items():
            self.assertEqual(self.stats["scenario_counts"][scen], quota, scen)

    def test_missing_pod_structurally_true(self):
        repo = self.repo()
        for did, g in self.gt.items():
            if g["scenario"] != datagen.MISSING_POD:
                continue
            ships = repo.list_shipments_for_order(g["order_id"])
            self.assertTrue(ships, did)
            self.assertIsNone(ships[0].pod_doc_id, did)

    def test_pincode_mismatch_pods(self):
        repo = self.repo()
        checked = 0
        for did, g in self.gt.items():
            if g["scenario"] != datagen.CONFLICT_PIN:
                continue
            order = repo.get_order(g["order_id"])
            ship = repo.list_shipments_for_order(order.id)[0]
            pod = repo.get_document(ship.pod_doc_id)
            order_pin = order.address.rsplit(" ", 1)[-1]
            self.assertNotIn(order_pin, pod.raw_text, did)
            checked += 1
        self.assertEqual(checked, datagen.SCENARIO_QUOTAS[datagen.CONFLICT_PIN])

    def test_duplicate_webhooks_delivered_twice(self):
        counts: dict[str, int] = {}
        for e in self.events:
            counts[e["dispute_id"]] = counts.get(e["dispute_id"], 0) + 1
        for did, g in self.gt.items():
            expected = 2 if g["scenario"] == datagen.DUP_EVENT else 1
            self.assertEqual(counts[did], expected, did)

    def test_partial_refunds_reconcile(self):
        repo = self.repo()
        n = 0
        for did, g in self.gt.items():
            if g["scenario"] != datagen.PARTIAL_REFUND:
                continue
            order = repo.get_order(g["order_id"])
            dispute = repo.get_dispute(did)
            refunds = sum(r.amount for r in repo.list_refunds_for_order(order.id))
            self.assertGreater(refunds, 0, did)
            self.assertEqual(order.amount - refunds, dispute.amount, did)
            n += 1
        self.assertEqual(n, datagen.SCENARIO_QUOTAS[datagen.PARTIAL_REFUND])

    def test_hinglish_admissions_contain_canonical_marker(self):
        repo = self.repo()
        for did, g in self.gt.items():
            if g["scenario"] != datagen.HINGLISH:
                continue
            order = repo.get_order(g["order_id"])
            row = repo.conn.execute(
                "SELECT raw_text FROM documents WHERE source = ?",
                (f"mailbox:{order.customer_email}",)).fetchone()
            self.assertIsNotNone(row, did)
            self.assertTrue(any(m in row["raw_text"]
                                for m in datagen.HINGLISH_MARKERS), did)

    def test_delayed_disputes_under_36_hours(self):
        for did, g in self.gt.items():
            if g["scenario"] == datagen.DELAYED:
                self.assertLessEqual(g["hours_left_at_sim_now"], 36.0, did)

    def test_ambiguous_disputes_have_unresolvable_payment_and_twin(self):
        repo = self.repo()
        for did, g in self.gt.items():
            if g["scenario"] != datagen.AMBIGUOUS:
                continue
            dispute = repo.get_dispute(did)
            self.assertIsNone(repo.get_order_by_payment(dispute.payment_id), did)
            order = repo.get_order(g["order_id"])
            twins = repo.conn.execute(
                "SELECT COUNT(*) c FROM orders WHERE customer_email = ? AND amount = ?",
                (order.customer_email, order.amount)).fetchone()["c"]
            self.assertGreaterEqual(twins, 2, did)


class TestGroundTruth(DatasetTestBase):
    def test_every_dispute_labelled_with_valid_vocab(self):
        for did, g in self.gt.items():
            self.assertIn(g["gt_correct_action"], ("FIGHT", "ACCEPT", "ESCALATE"), did)
            self.assertIn(g["gt_outcome_if_fought"], ("won", "lost"), did)
            self.assertIsInstance(g["gt_evidence_present"], list, did)

    def test_labels_respect_policy_caps(self):
        """Ground truth must be consistent with the config caps it derives from."""
        repo = self.repo()
        for did, g in self.gt.items():
            dispute = repo.get_dispute(did)
            if dispute.amount > 10_000:
                self.assertEqual(g["gt_correct_action"], "ESCALATE", did)
            if g["gt_correct_action"] == "ACCEPT":
                self.assertLessEqual(dispute.amount, 2_000, did)
            if g["hours_left_at_sim_now"] < 24:
                self.assertEqual(g["gt_correct_action"], "ESCALATE", did)

    def test_ground_truth_not_in_app_database(self):
        """Structural hiding: no gt column/table exists in the app DB."""
        repo = self.repo()
        tables = repo.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        for t in tables:
            self.assertNotIn("truth", t["name"].lower())
            cols = repo.conn.execute(f"PRAGMA table_info({t['name']})").fetchall()
            for c in cols:
                self.assertFalse(c["name"].startswith("gt_"),
                                 f"{t['name']}.{c['name']} leaks ground truth")


class TestDeterminismAndValidation(unittest.TestCase):
    def test_same_seed_reproduces_identical_world(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            generate(seed=7, out_dir=a, n_orders=200, n_disputes=120)
            generate(seed=7, out_dir=b, n_orders=200, n_disputes=120)
            self.assertEqual(_world_fingerprint(Path(a)), _world_fingerprint(Path(b)))

    def test_different_seed_produces_different_world(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            generate(seed=7, out_dir=a, n_orders=200, n_disputes=120)
            generate(seed=8, out_dir=b, n_orders=200, n_disputes=120)
            self.assertNotEqual(_world_fingerprint(Path(a)), _world_fingerprint(Path(b)))

    def test_validator_rejects_corrupted_data(self):
        with tempfile.TemporaryDirectory() as d:
            generate(seed=11, out_dir=d, n_orders=200, n_disputes=120)
            # Tamper: a refund larger than any order amount — passes the schema
            # CHECK (amount > 0) but violates the dataset invariant
            # sum(refunds) <= order.amount, which validation must catch.
            repo = Repository(Path(d) / "dataset.db")
            order_id = repo.conn.execute("SELECT id FROM orders LIMIT 1").fetchone()["id"]
            repo.add_refund(Refund(id="rf_evil", order_id=order_id,
                                   amount=10_000_000,
                                   created_at="2026-08-20T00:00:00+00:00"))
            repo.close()
            with self.assertRaises(DatasetError):
                validate_dataset(d)

    def test_validator_rejects_split_tampering(self):
        with tempfile.TemporaryDirectory() as d:
            generate(seed=11, out_dir=d, n_orders=200, n_disputes=120)
            split_path = Path(d) / "split.json"
            split = json.loads(split_path.read_text())
            split["dev"].append(split["held_out"][0])  # create an overlap
            split_path.write_text(json.dumps(split))
            with self.assertRaises(DatasetError):
                validate_dataset(d)
