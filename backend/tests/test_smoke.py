"""Stage-1 smoke test: the package skeleton imports and config is sane.

Deliberately minimal — later stages add real behavior tests. This exists so
that from the very first commit, `python -m unittest discover` is green and
every subsequent stage has a working harness to extend.
"""
import unittest


class TestSkeleton(unittest.TestCase):
    def test_subpackages_import(self):
        import app  # noqa: F401
        import app.ai, app.audit, app.evals, app.policy, app.store, app.tools  # noqa

    def test_config_invariants(self):
        from app import config
        self.assertLess(config.AUTO_ACCEPT_CAP_INR, config.ESCALATION_AMOUNT_CAP_INR,
                        "auto-accept cap must sit below the human-approval cap")
        self.assertGreater(config.COMPLETENESS_FIGHT_FLOOR,
                           config.COMPLETENESS_ACCEPT_CEILING)
        self.assertGreaterEqual(config.DEADLINE_ESCALATE_HOURS,
                                config.API_FAILURE_ESCALATE_HOURS)
        self.assertTrue(str(config.THRESHOLDS_VERSION))
