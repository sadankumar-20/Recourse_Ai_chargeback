"""Stage-9 tests: the evaluation harness itself.

Covers frozen-set protection (fail-loud on any alteration), anti-leakage
(the pipeline never sees ground truth), metric arithmetic, the mandatory
not-confidently-handled table, the gate ablation, and reproducibility with
non-deterministic metadata separated from deterministic metrics.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.datagen import generate
from app.evals.harness import EvalError, format_report, run_eval


class EvalHarnessBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.data = Path(cls.tmp.name) / "data"
        generate(seed=42, out_dir=cls.data)
        cls.outdir = Path(cls.tmp.name) / "evals"
        cls.result = run_eval(cls.data, cls.outdir, ablate_gate=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()


class TestFrozenSetProtection(EvalHarnessBase):
    def test_exactly_40_cases_no_overlap(self):
        self.assertEqual(self.result["metrics"]["cases_evaluated"], 40)
        split = json.loads((self.data / "split.json").read_text())
        self.assertEqual(len(split["held_out"]), 40)
        self.assertFalse(set(split["held_out"]) & set(split["dev"]))
        evaluated = {c["dispute_id"] for c in self.result["cases"]}
        self.assertEqual(evaluated, set(split["held_out"]))

    def test_altered_split_fails_loudly(self):
        with tempfile.TemporaryDirectory() as d:
            generate(seed=7, out_dir=d, n_orders=200, n_disputes=120)
            sp = Path(d) / "split.json"
            split = json.loads(sp.read_text())

            def try_eval():
                with tempfile.TemporaryDirectory() as o:
                    run_eval(d, o)

            # wrong size
            split_bad = dict(split); split_bad["held_out"] = split["held_out"][:39]
            sp.write_text(json.dumps(split_bad))
            with self.assertRaises(EvalError): try_eval()
            # overlap
            split_bad = dict(split)
            split_bad["dev"] = split["dev"] + [split["held_out"][0]]
            sp.write_text(json.dumps(split_bad))
            with self.assertRaises(EvalError): try_eval()
            # wrong seed
            split_bad = dict(split); split_bad["seed"] = 99
            sp.write_text(json.dumps(split_bad))
            with self.assertRaises(EvalError): try_eval()

    def test_missing_artifacts_fail_with_instructions(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(EvalError) as cm:
                run_eval(d, d)
            self.assertIn("generate.py", str(cm.exception))

    def test_source_world_is_never_mutated(self):
        # eval must run on a copy: the source db has no cases after eval
        import sqlite3
        conn = sqlite3.connect(self.data / "dataset.db")
        n = conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0]
        conn.close()
        self.assertEqual(n, 0, "run_eval mutated the source world")


class TestAntiLeakage(EvalHarnessBase):
    def test_orchestrator_and_lanes_never_import_ground_truth(self):
        """Ground truth is consumed only by the eval layer, strictly after
        the agent finishes. The pipeline cannot even load the file."""
        import ast
        for pkg in ("app/ai", "app/policy", "app/tools"):
            for py in Path(pkg).glob("*.py"):
                self.assertNotIn("ground_truth", py.read_text(), py)
        src = Path("app/orchestrator.py").read_text()
        self.assertNotIn("ground_truth", src)
        self.assertNotIn("gt_", src)

    def test_gt_consulted_only_after_terminal_state(self):
        # structural: harness passes gt into _evaluate_case, never into
        # Orchestrator; verify by signature inspection
        import inspect
        from app.evals import harness
        src = inspect.getsource(harness.run_eval)
        # the orchestrator construction line must not reference gt labels
        orch_line = next(l for l in src.splitlines() if "Orchestrator(" in l)
        self.assertNotIn("gt", orch_line)


class TestMetrics(EvalHarnessBase):
    def test_decision_metrics_consistent(self):
        m = self.result["metrics"]["decision"]
        cases = self.result["cases"]
        self.assertEqual(m["correct"],
                         sum(1 for c in cases if c["action_correct"]))
        total_in_matrix = sum(sum(r.values())
                              for r in m["confusion_matrix"].values())
        self.assertEqual(total_in_matrix, 40)
        self.assertAlmostEqual(m["accuracy"], m["correct"] / 40, places=4)

    def test_every_wrong_decision_is_a_documented_coverage_gap(self):
        """The stage's honest headline: no judgment errors, only deferred
        reason codes."""
        wrong = [c for c in self.result["cases"] if not c["action_correct"]]
        self.assertTrue(wrong, "expected coverage-gap escalations to exist")
        for c in wrong:
            self.assertEqual(c["agent_action"], "ESCALATE", c["dispute_id"])
            self.assertIn("unsupported reason code",
                          c["escalation_reason"] or "", c["dispute_id"])

    def test_extraction_precision_and_recall_present_and_bounded(self):
        e = self.result["metrics"]["extraction"]
        for k in ("precision", "recall_vs_all_gt_evidence",
                  "recall_vs_playbook_extractable"):
            self.assertIsNotNone(e[k])
            self.assertGreaterEqual(e[k], 0.0)
            self.assertLessEqual(e[k], 1.0)
        self.assertGreater(e["precision"], 0.9)

    def test_automation_escalation_sum_to_one(self):
        a = self.result["metrics"]["automation"]
        self.assertAlmostEqual(a["automation_rate"] + a["escalation_rate"],
                               1.0, places=4)
        self.assertIsNotNone(a["escalation_precision_strict"])

    def test_deadline_compliance_is_total(self):
        d = self.result["metrics"]["deadline_compliance"]
        self.assertEqual(d["violations"], 0, d["violation_ids"])
        self.assertEqual(d["rate"], 1.0)

    def test_all_audit_chains_valid(self):
        a = self.result["metrics"]["audit"]
        self.assertEqual(a["chains_valid"], a["chains_total"])

    def test_money_arithmetic(self):
        r = self.result["metrics"]["money"]["recourse"]
        cases = self.result["cases"]
        self.assertEqual(r["recovered"],
                         sum(c["amount_recovered"] for c in cases))
        self.assertEqual(r["false_fight_cost_total"],
                         sum(c["false_fight_cost"] for c in cases))
        self.assertEqual(r["net"], r["recovered"] - r["fees_paid_on_losses"])
        self.assertEqual(r["escalated_amount_pending"],
                         sum(c["amount"] for c in cases if c["escalated"]))
        never = self.result["metrics"]["money"]["baseline_never_contest"]
        self.assertEqual(never, {"recovered": 0, "fees": 0, "net": 0})
        ca = self.result["metrics"]["money"]["baseline_contest_all"]
        self.assertEqual(ca["net"],
                         ca["recovered"] - ca["fees_paid_on_losses"])

    def test_not_confidently_handled_table_present_and_complete(self):
        report = (self.outdir / "report.md").read_text()
        self.assertIn("## Not confidently handled", report)
        for c in self.result["cases"]:
            if c["escalated"]:
                self.assertIn(c["dispute_id"], report)

    def test_gate_ablation_present_and_meaningful(self):
        g = self.result["metrics"]["gate_ablation"]
        self.assertIn("ABLATION", g["label"])
        self.assertGreater(g["inadmissible_candidates_that_would_ship"], 0)
        self.assertGreater(len(g["decisions_that_would_flip"]), 0)
        for flip in g["decisions_that_would_flip"]:
            self.assertEqual(flip["gate_on"], "ESCALATE")
            self.assertEqual(flip["gate_off"], "FIGHT")

    def test_report_and_metrics_agree(self):
        report = (self.outdir / "report.md").read_text()
        m = self.result["metrics"]
        self.assertIn(f"{m['decision']['accuracy']*100:.1f}%", report)
        self.assertIn(f"{m['money']['recourse']['recovered']:,}", report)
        self.assertIn("Limitations", report)
        self.assertNotIn("production-ready", report.lower())


class TestReproducibility(unittest.TestCase):
    def test_two_runs_identical_deterministic_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            generate(seed=42, out_dir=data)
            r1 = run_eval(data, Path(tmp) / "a", ablate_gate=True)
            r2 = run_eval(data, Path(tmp) / "b", ablate_gate=True)
            self.assertEqual(r1["metrics"], r2["metrics"])
            self.assertEqual(r1["cases"], r2["cases"])
            self.assertEqual(r1["config"], r2["config"])
            # meta (timestamps, wall time) is allowed to differ — separated
            # by design so the comparison above stays meaningful
