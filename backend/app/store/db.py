"""SQLite schema and connection handling for Recourse.

Design notes
------------
- ``PRAGMA foreign_keys = ON`` must be set per connection: SQLite ships with
  FK enforcement OFF, which silently permits orphaned rows. We refuse to hand
  out a connection without it.
- Enum vocabularies are enforced twice on purpose: the repository validates
  via the Python enums (nice errors), and CHECK constraints below enforce the
  same vocabulary at the storage layer so even raw SQL cannot persist an
  invalid state. Defense-in-depth for a system that gates money decisions.
- Money columns are INTEGER rupees. No REAL money columns exist; the only
  REAL columns are probabilities/expected values on ``decisions``.
- ``audit_log`` carries ``prev_hash``/``entry_hash`` columns already so the
  schema will not need migrating when the hash-chain stage lands.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# Bump whenever the schema changes. Generated worlds carry this stamp in
# PRAGMA user_version; opening a world built with a different schema raises a
# clear, actionable error instead of a cryptic "no such column" later.
# History: 1 = Stage 2 original; 2 = Stage 4 added evidence.evidence_key;
# 3 = R1 added documents.provenance, disputes.provenance, and the
#     'needs_input' case state (interactive gap resolution);
# 4 = R4 added 'user_submitted' to documents.provenance (the intake
#     narrative is a stored document with its own origin).
SCHEMA_VERSION = 4


class SchemaVersionError(RuntimeError):
    pass


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS merchants (
    id                    TEXT PRIMARY KEY,
    name                  TEXT NOT NULL,
    auto_accept_cap       INTEGER NOT NULL CHECK (auto_accept_cap >= 0),
    escalation_amount_cap INTEGER NOT NULL CHECK (escalation_amount_cap >= auto_accept_cap)
);

CREATE TABLE IF NOT EXISTS orders (
    id               TEXT PRIMARY KEY,
    merchant_id      TEXT NOT NULL REFERENCES merchants(id),
    payment_id       TEXT NOT NULL UNIQUE,
    amount           INTEGER NOT NULL CHECK (amount > 0),
    customer_email   TEXT NOT NULL,
    address          TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    promised_ship_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS refunds (
    id         TEXT PRIMARY KEY,
    order_id   TEXT NOT NULL REFERENCES orders(id),
    amount     INTEGER NOT NULL CHECK (amount > 0),
    created_at TEXT NOT NULL
);

-- documents before shipments: shipments.pod_doc_id references documents.
CREATE TABLE IF NOT EXISTS documents (
    id         TEXT PRIMARY KEY,
    case_id    TEXT REFERENCES cases(id),          -- nullable until attached
    type       TEXT NOT NULL CHECK (type IN ('email','pod','invoice','log')),
    raw_text   TEXT NOT NULL,
    source     TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    provenance TEXT NOT NULL DEFAULT 'simulator' CHECK (provenance IN
        ('simulator','user_upload','user_submitted','razorpay_test',
         'tracking_api','vision_transcribed'))
);

CREATE TABLE IF NOT EXISTS shipments (
    id         TEXT PRIMARY KEY,
    order_id   TEXT NOT NULL REFERENCES orders(id),
    awb        TEXT NOT NULL,
    courier    TEXT NOT NULL,
    ship_date  TEXT NOT NULL,
    status     TEXT NOT NULL,
    pod_doc_id TEXT REFERENCES documents(id)
);

CREATE TABLE IF NOT EXISTS disputes (
    id          TEXT PRIMARY KEY,
    payment_id  TEXT NOT NULL,   -- deliberately NOT an FK: linking a dispute
                                 -- to an order is the agent's job (§8 step 3)
    amount      INTEGER NOT NULL CHECK (amount > 0),
    reason_code TEXT NOT NULL CHECK (reason_code IN
        ('goods_not_received','not_as_described','duplicate',
         'fraud','credit_not_processed','cancelled_recurring')),
    respond_by  TEXT NOT NULL,
    provenance  TEXT NOT NULL DEFAULT 'simulator' CHECK (provenance IN
        ('simulator','user_submitted','razorpay_test')),
    status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN
        ('open','under_review','won','lost','accepted','expired'))
);

CREATE TABLE IF NOT EXISTS cases (
    id              TEXT PRIMARY KEY,
    dispute_id      TEXT NOT NULL UNIQUE REFERENCES disputes(id),
    state           TEXT NOT NULL DEFAULT 'intake' CHECK (state IN
        ('intake','linking','gathering','needs_input','gated','decided',
         'acted','closed','escalated')),
    linked_order_id TEXT REFERENCES orders(id),
    link_confidence REAL CHECK (link_confidence IS NULL
                                OR (link_confidence >= 0.0 AND link_confidence <= 1.0))
);

CREATE TABLE IF NOT EXISTS evidence (
    id            TEXT PRIMARY KEY,
    case_id       TEXT NOT NULL REFERENCES cases(id),
    evidence_key  TEXT NOT NULL,
    claim         TEXT NOT NULL,
    source_doc_id TEXT NOT NULL REFERENCES documents(id),
    quoted_span   TEXT NOT NULL,
    fields_json   TEXT NOT NULL,
    gate_verdict  TEXT CHECK (gate_verdict IS NULL OR gate_verdict IN ('PASS','FAIL')),
    fail_reason   TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
    id                 TEXT PRIMARY KEY,
    case_id            TEXT NOT NULL REFERENCES cases(id),
    action             TEXT NOT NULL CHECK (action IN ('FIGHT','ACCEPT','ESCALATE')),
    completeness       REAL NOT NULL CHECK (completeness >= 0.0 AND completeness <= 1.0),
    p_win              REAL NOT NULL CHECK (p_win >= 0.0 AND p_win <= 1.0),
    ev_fight           REAL NOT NULL,
    ev_accept          REAL NOT NULL,
    thresholds_version TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS actions (
    id              TEXT PRIMARY KEY,
    case_id         TEXT NOT NULL REFERENCES cases(id),
    type            TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_json    TEXT NOT NULL,
    response_json   TEXT NOT NULL,
    actor           TEXT NOT NULL CHECK (actor IN ('agent','human')),
    at              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outcomes (
    id               TEXT PRIMARY KEY,
    case_id          TEXT NOT NULL UNIQUE REFERENCES cases(id),
    result           TEXT NOT NULL CHECK (result IN ('won','lost','accepted','expired')),
    amount_recovered INTEGER NOT NULL CHECK (amount_recovered >= 0)
);

CREATE TABLE IF NOT EXISTS audit_log (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,  -- authoritative ordering
    case_id      TEXT NOT NULL REFERENCES cases(id),
    step         TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    at           TEXT NOT NULL,
    prev_hash    TEXT,   -- populated by the dedicated audit stage
    entry_hash   TEXT
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection with FK enforcement on and named-column rows."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(db_path: str | Path) -> None:
    """Create the schema (idempotent) and enforce the version stamp."""
    conn = connect(db_path)
    try:
        has_tables = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0] > 0
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if has_tables and version != SCHEMA_VERSION:
            raise SchemaVersionError(
                f"database {db_path} was created with schema version "
                f"{version}, but this code expects {SCHEMA_VERSION}. "
                f"Regenerate the world: python3 data/generate.py --seed 42")
        conn.executescript(SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    finally:
        conn.close()
