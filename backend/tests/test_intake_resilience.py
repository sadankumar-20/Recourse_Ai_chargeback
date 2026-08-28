"""Provider failures must never surface as HTML 500s: the intake route
returns structured JSON, and a case created before an engine failure is
returned honestly rather than lost."""
import importlib
import os
import unittest


class TestIntakeResilience(unittest.TestCase):
    def test_provider_failure_is_structured_json_not_html_500(self):
        os.environ["RECOURSE_AI_PROVIDER"] = "anthropic"
        os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            from app import config
            importlib.reload(config)
            from app import api as api_mod
            importlib.reload(api_mod)
            import tempfile
            app = api_mod.create_app(tempfile.mktemp(suffix=".db"))
            app.testing = True
            c = app.test_client()
            r = c.post("/intake", json={"text":
                       "Customer says order #0100 never arrived, but we "
                       "dispatched it on time and the courier shows "
                       "delivered"})
            self.assertEqual(r.status_code, 503)
            self.assertEqual(r.content_type, "application/json")
            body = r.get_json()
            self.assertEqual(body["error_type"], "provider_unavailable")
            self.assertNotIn("Traceback", r.get_data(as_text=True))
        finally:
            os.environ["RECOURSE_AI_PROVIDER"] = "stub"
            from app import config
            importlib.reload(config)
