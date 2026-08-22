# Engineering decision record

## ADR-001 — Stdlib-first stack (Flask + sqlite3 + unittest) instead of FastAPI + SQLAlchemy + pydantic + pytest

**Date:** 2026-08-23 · **Status:** accepted

**Problem.** The build environment has no network egress; `pip install` cannot
fetch FastAPI, SQLAlchemy, pydantic, or pytest. Preinstalled and available:
Python 3.12 stdlib, Flask, PyYAML, requests, jinja2.

**Options considered.**
1. Vendor wheels — impossible without network access.
2. Write the FastAPI stack anyway, untested — violates the project's own rule
   that every stage must run and be verified; unacceptable.
3. Keep the architecture, swap the libraries — Flask for the REST surface,
   stdlib `sqlite3` behind a thin repository layer, stdlib `unittest`,
   hand-written validators for LLM output.

**Decision.** Option 3. The spec's real substance is architectural — the
AI / policy / execution three-lane separation, the Admissibility Gate,
citation-constrained drafting, the append-only hash-chained audit log, monetary
caps and the deadline kill-switch. None of that depends on a specific web or
ORM framework.

**Why this is not a downgrade.**
- Raw `sqlite3` makes the append-only audit table and its hash chain fully
  transparent — no ORM magic between us and an integrity guarantee.
- Replacing pydantic with explicit, tested validator functions *strengthens*
  the "treat LLM output as untrusted input" requirement: validation is code we
  own and unit-test, not an annotation.
- `unittest` is less ergonomic than pytest but equally rigorous.

**Consequences.**
- `backend/app/ai/schemas.py` (later stage) will carry hand-rolled validators
  with adversarial tests (malformed JSON, wrong types, fabricated fields).
- If the project is later moved to a networked environment, the HTTP layer is
  thin enough to port to FastAPI in an afternoon; nothing in `policy/`,
  `store/`, or `audit/` would change.

## ADR-002 — No agent framework

Explicit state machine in `orchestrator.py` per spec §14/§30. Frameworks hide
exactly the thing this project must showcase: where AI proposals stop and
deterministic policy begins. Rejected: LangChain/LlamaIndex-style orchestration
(also unavailable offline, but rejected on principle regardless).

## ADR-003 — LLM access behind an interface with an offline deterministic stub

Tests must pass with zero network. `ai/` will define an `LLMClient` protocol
with (a) a real Anthropic HTTP implementation used when `ANTHROPIC_API_KEY`
is present, and (b) a deterministic `StubLLM` used in tests and offline demos.
The orchestrator cannot tell them apart; every call is audited either way.
This mirrors the payments `SimulatorAdapter` honesty rule in spec §11.
