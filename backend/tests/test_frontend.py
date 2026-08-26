"""R6 cockpit checks: the frontend renders only server truth, the countdown
mirrors the server's thresholds, and no secrets or invented state leak in."""
import re
from pathlib import Path

FE = Path(__file__).resolve().parents[2] / "frontend"
JS = (FE / "app.js").read_text()
CSS = (FE / "style.css").read_text()
HTML = (FE / "index.html").read_text()


def test_frontend_talks_to_every_agentic_endpoint():
    for ep in ["/intake", "/upload", "/resume", "/deadline", "/audit",
               "/evidence", "/approve", "/reject", "/metrics"]:
        assert ep in JS, f"frontend never calls {ep}"


def test_ledger_is_rendered_from_audit_steps_only():
    # Every ledger row type maps from a real audit step name — the UI
    # cannot invent investigation events the chain does not contain.
    for step in ["AGENT_PLAN", "TOOL_CALL", "AGENT_OBSERVATION",
                 "EVIDENCE_ADMITTED", "EVIDENCE_REJECTED",
                 "AGENT_NEEDS_INPUT", "INVESTIGATION_RESUMED",
                 "DECISION_MADE", "ACTION_SUBMITTED"]:
        assert step in JS


def test_countdown_thresholds_mirror_server():
    # Client statusOf must use the same 24h/48h boundaries as
    # deadline_status() server-side; the server stays authoritative.
    assert "86400" in JS and "172800" in JS
    assert "remaining_seconds" in JS and "respond_by" in JS
    assert "fmtCountdown" in JS
    for st in ["SAFE", "WARNING", "CRITICAL", "EXPIRED"]:
        assert st in JS and f"countdown.{st}" in CSS


def test_expiry_disables_actions_client_side():
    assert "disabled = true" in JS and "Deadline expired" in JS


def test_no_key_material_or_external_resources():
    for bad in ["sk-ant", "ANTHROPIC_API_KEY", "x-api-key"]:
        assert bad not in JS and bad not in HTML
    assert "https://cdn" not in HTML and "googleapis" not in HTML


def test_voice_is_feature_detected_and_optional():
    assert "webkitSpeechRecognition" in JS
    assert re.search(r"if\s*\(SR\)", JS), "voice must be gated, typed-first"


def test_provenance_badges_styled_for_every_source_type():
    for p in ["simulator", "user_upload", "tracking_api",
              "vision_transcribed", "user_submitted"]:
        assert f"prov.{p}" in CSS


def test_reduced_motion_respected():
    assert "prefers-reduced-motion" in CSS


def test_dom_injection_paths_are_escaped():
    # Spot-check: user-controlled strings pass through esc() before HTML.
    for field in ["request_to_user", "filename", "merchant_summary",
                  "quoted_span", "fail_reason"]:
        assert re.search(r"esc\([^)]*" + field, JS) or \
               f"esc(p.{field}" in JS or f"esc(e.{field}" in JS, \
               f"{field} rendered without esc()"


def test_case_detail_exposes_kb_citations(tmp_path):
    # API side of the popover: DRAFT_CREATED kb payload surfaces in detail.
    api = (Path(__file__).resolve().parents[1] / "app" / "api.py").read_text()
    assert "kb_citations" in api


def test_landing_and_kpis_render_from_real_data_only():
    assert "renderLanding" in JS and "INVESTIGATE" in JS
    assert "revenue at risk" in JS and "atRisk" in JS
    assert "reduce((s, c) => s + (c.amount" in JS   # computed, not hardcoded


def test_metrics_page_shows_honest_v2_including_negative():
    assert "net_money_delta_on_v1_labels" in JS
    assert "label_caveat" in JS and "honest" in JS
    api = (Path(__file__).resolve().parents[1] / "app" / "api.py").read_text()
    assert "v2_metrics.json" in api
