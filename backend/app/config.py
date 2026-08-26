"""Central configuration for Recourse.

One module, plain constants, environment-overridable where it matters.
Monetary values are in INR paise-free rupees (int) to avoid float money math.
Thresholds here are *defaults*; the policy engine records the version of the
threshold set used for every decision (spec §18: versioned playbooks/thresholds).
"""

from __future__ import annotations

import os
from pathlib import Path

# --- Paths -----------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"
DB_PATH = Path(os.environ.get("RECOURSE_DB", REPO_ROOT / "recourse.db"))

# --- Policy thresholds (spec §30 policy package) ----------------------------
THRESHOLDS_VERSION = "v1"
AUTO_ACCEPT_CAP_INR = 2_000          # ACCEPT allowed autonomously only <= this
ESCALATION_AMOUNT_CAP_INR = 10_000   # any dispute above this always needs a human
CONTEST_FEE_INR = 500                # fee burned when a contest is lost
COMPLETENESS_FIGHT_FLOOR = 0.75
COMPLETENESS_ACCEPT_CEILING = 0.25
LINK_CONFIDENCE_FLOOR = 0.85
DEADLINE_ESCALATE_HOURS = 24         # T-24h: undecided open cases force-escalate
API_FAILURE_ESCALATE_HOURS = 12      # T-12h: still-failing submission -> human
MAX_SUBMIT_RETRIES = 3

# --- LLM -------------------------------------------------------------------
# Provider: "stub" (deterministic, offline — default so tests and local dev
# never touch the network) or "anthropic" (real API; fails loudly without a
# key — never silently falls back to the stub).
AI_PROVIDER = os.environ.get("RECOURSE_AI_PROVIDER", "stub")

# R2: "fixed" = the Stage-8 predefined gather path; "agentic" = the bounded
# investigation loop (planner + read-only tools). Both feed the SAME
# extraction, gate, and decision engine. Default stays fixed until the A/B
# and eval v2 justify flipping it.
INVESTIGATION_MODE = os.environ.get("RECOURSE_INVESTIGATION", "fixed")

# R3: local, offline, deterministic knowledge base. Disabling degrades
# gracefully: search_knowledge returns a structured error and the planner
# moves on; nothing else depends on it.
KNOWLEDGE_ENABLED = os.environ.get("RECOURSE_KNOWLEDGE", "true").lower() == "true"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
LLM_MODEL = os.environ.get("RECOURSE_LLM_MODEL", "claude-sonnet-4-6")
AI_MAX_TOKENS = 2000

# --- Adapters ---------------------------------------------------------------
# "simulator" (labeled mock, full dispute lifecycle) | "razorpay_test"
PAYMENTS_ADAPTER = os.environ.get("RECOURSE_PAYMENTS_ADAPTER", "simulator")
RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
