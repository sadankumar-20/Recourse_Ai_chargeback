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

## ADR-004 — Dataset ground truth lives outside the application database

The eval labels (`gt_correct_action`, `gt_evidence_present`,
`gt_outcome_if_fought`) are written to `data/ground_truth.json`, never into
`dataset.db`. Hiding is structural: no repository method, API endpoint, or
dashboard query can leak labels that are not in the database at all. A test
(`test_ground_truth_not_in_app_database`) asserts no `gt_*` column or
truth-named table ever appears in the app schema. Only the eval harness may
read the JSON.

Two related conventions, chosen to avoid changing the frozen §12 schema:
- `documents` has no `order_id`; PODs link via `shipments.pod_doc_id`, and
  email threads carry `source = "mailbox:<customer_email>"` — the same way a
  real support mailbox is searched.
- Duplicate dispute webhooks are a *delivery* phenomenon, not a storage one
  (the PK forbids two identical dispute rows), so the generator emits
  `data/events.jsonl` — the webhook feed the orchestrator will later consume —
  in which duplicate-event disputes appear twice.

## ADR-005 — Scenario-driven generation instead of post-hoc corruption

Each dispute is generated from one of 11 named scenarios with fixed quotas
matching spec §13 rates. Ground truth is *derived* from the scenario's facts
using the same config caps the policy engine will use (amount > cap →
ESCALATE, <24h → ESCALATE, hopeless ≤ auto-accept cap → ACCEPT, complete
evidence → FIGHT), so evaluation later is principled rather than circular.
The split is stratified per scenario so the 40 held-out disputes cover every
failure mode. Bug found & fixed during this stage: the Hinglish marker list
had drifted from the admission templates (15/20 counted); markers are now the
single source of truth and validation asserts count == quota.

## ADR-006 — Admissibility Gate design (Stage 4)

**Extraction is separated from admission.** The AI may propose evidence; only
`policy/gate.py` may admit it. The gate holds zero LLM/network imports — a
test parses the policy package's ASTs and fails on any banned import.

**One anti-fabrication pattern, applied uniformly:** every claimed field must
(1) appear VERBATIM in the source document and (2) match the system of record
(shipment AWB, order pincode, order − refunds arithmetic). An AI that invents
a value fails (1); an AI that faithfully quotes a document contradicting the
records fails (2). No semantic similarity, no LLM verification, no confidence
override — a failed gate cannot be overridden by anything upstream.

**Verdicts are structured and preserved.** Each verdict carries the full
per-check trail (PASS rows included on failures), the playbook version, and a
precise first-failure reason ("pincode mismatch: POD shows delivery to
560083, order address is 560038"). Failed evidence is marked inadmissible,
never deleted, so the dashboard can show PASS — verified next to FAIL — exact
reason.

**Smallest §12 extension:** `evidence.evidence_key` (model + column) — the
playbook checklist is keyed, and candidates must declare which checklist item
they claim to satisfy; without it the gate cannot map candidates to rules.

**Config errors are not evidence failures.** An unsupported reason code or an
unknown check name raises PlaybookError (fail-loud), while evidence-level
problems always return structured FAIL verdicts.

**Fix made during the stage:** the oracle extractor originally proposed every
extractable key; the gate correctly rejected off-checklist keys (7 FAILs on
dev data). Spec §8 step 6 defines extraction as checklist-driven, so the
oracle now takes the reason code's checklist — the gate's off-checklist
rejection stays, covered by an adversarial test.

## ADR-007 — Decision engine (Stage 5)

**Rule ladder, first match wins:** deadline_passed → amount_over_cap →
deadline_kill_switch → precondition_failed → concede_hopeless →
fight_ev_positive → needs_human. Each outcome records the exact rule fired,
the full EV math, satisfied/missing required keys (with the gate's precise
failure reasons attached), and both the thresholds version and playbook
version — the audit stage persists all of it.

**Concede only when there is nothing to fight with.** ACCEPT additionally
requires that no shipment ever existed. If a shipment exists but proof is
missing, the case escalates as *recoverable* — the merchant email can name
exactly which document to fetch. Conceding recoverable cases would quietly
donate money; fighting hopeless ones burns fees. This one boolean encodes the
difference.

**EV model, stated honestly:** EV(fight) = p_win × amount − fee (fee charged
on every fight — conservative), EV(accept) = −amount, p_win from versioned
playbook bands selected by completeness = satisfied required / required.
Boundary behavior is unit-tested on both sides (₹270 escalates as
uneconomical, ₹271 fights, at p_win 0.85 / fee ₹500).

**On the 100% dev-split agreement (59/59 decided, 21 skipped visibly):** this
is a CONSISTENCY check, not a performance claim — extraction was the
deterministic oracle and the labels were derived from the same policy caps.
Its value: any future regression in gate, playbook, or decision logic breaks
a test loudly. Genuine disagreement becomes possible only when the LLM
replaces the oracle; measuring that is precisely the eval stage's job.

## ADR-008 — AI layer design (Stage 6)

**AI only at the ambiguity boundary.** Three narrow functions — link (rank
candidates when exact matching fails), extract (messy docs → candidate
evidence), draft (admitted evidence → cited narrative). Each has one focused,
versioned prompt (ai/prompts/*.md, version + sha256 recorded per call). The
AI never decides FIGHT/ACCEPT/ESCALATE, never reads the DB, never touches
execution — the last two are AST-test-enforced (no app.tools / app.store.repo
/ sqlite3 imports in the ai package).

**Every LLM response is untrusted input.** Hand-rolled strict validators
(ADR-001): parse → validate → on failure exactly ONE repair retry carrying
the validator's exact error → still invalid raises LowConfidence with the
full call records. No silent coercion, no silent defaults. Link proposals
cannot name an order outside the candidate set; extraction cannot cite an
unprovided document or skip a required field; drafts answer to the
deterministic policy/citations validator with no bypass.

**Two providers, loud selection.** AnthropicClient (env-keyed, never logged)
and a FAITHFUL deterministic StubAIClient so the whole pipeline runs and is
tested offline; ScriptedAIClient injects adversarial outputs in tests.
Requesting "anthropic" without a key raises AIConfigError — the system never
silently downgrades to the stub.

**Two defense layers, demonstrated by ablation tests:** a fabrication citing
a nonexistent document dies at schema validation; a schema-valid fabrication
(real doc id, invented quote) dies at the Admissibility Gate with "not found
verbatim". "AI-only" would have believed both.

**Bugs found and fixed during the stage:**
1. The citation validator's sentence splitter broke drafts at periods INSIDE
   quoted evidence (Hinglish spans like "...chhota hai. refund kar do"),
   causing false uncited-fact violations and spurious LowConfidence. The
   splitter is now quote-aware; regression test added.
2. The first ablation test targeted a documentless case, where fabrication is
   blocked at schema (no valid doc id exists) before reaching the gate — the
   test's premise was wrong, not the code. Rewritten as two tests showing
   both defense layers explicitly.

**Observability (minimal on purpose):** every call yields an AICallRecord
(provider, model, prompt version+sha, attempt, latency, tokens, validation
result). The orchestrator/audit stage will persist them; no secrets can enter
records (tested).
