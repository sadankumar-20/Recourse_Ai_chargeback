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

## ADR-009 — Execution lane and audit hash chain (Stage 7)

**Adapter pattern with honest capabilities.** PaymentsAdapter is a small
interface (lookups + contest + accept + status). SimulatorAdapter provides
the full deterministic dispute lifecycle over the Stage-2 store, labels every
response simulated, and supports controlled 503 injection. RazorpayTestAdapter
does real test-mode HTTP for payment/refund lookups only; contest/accept
raise NotSupported because Razorpay test mode cannot create synthetic
disputes to contest — faking those responses would misrepresent the
integration, so we don't (README carries the real-vs-simulated table).

**One money action per dispute, ever.** idempotency_key = dispute_id, checked
against the PERSISTED actions table (UNIQUE constraint as a second net, and
the simulator refuses non-open disputes as a third). This deliberately makes
a CONFLICTING second action (accept after contest) return the original too —
the strongest single-submission invariant, provable from the audit trail
(ACTION_SUBMITTED then ACTION_DUPLICATE with attempted vs original type).

**Executor is the single writer.** tools/executor.py validates the action
type (only contest/accept are executable — ESCALATE is a human task, not an
API call), enforces idempotency, executes via the adapter, persists the
ActionRecord, and audits every path: ACTION_SUBMITTED, ACTION_DUPLICATE,
ACTION_FAILED (transient failures create NO action row and re-raise for the
future orchestrator's retry loop). Adapters stay side-effect-minimal; the AI
package still cannot import any of this (Stage-6 AST test).

**Hash chain.** Per-case: entry_hash = SHA256(prev_hash | case_id | step |
canonical_payload | at), genesis 64 zeros, canonical_json = sorted keys +
compact separators. Redaction runs BEFORE hashing so stored bytes == hashed
bytes and secrets never enter the trail. verify_audit_chain recomputes from
genesis and reports the exact seq and reason on the first break; tamper tests
cover modified payload, tampered prev/entry hash, deleted middle entry,
reordered entries, and per-case isolation. The Stage-2 append-only API shape
plus the chain means tampering requires raw SQL — and is detected anyway.

## ADR-010 — Orchestrator (Stage 8)

**Coordination only, enforced by test.** The orchestrator owns the state walk,
deadline guards, the retry loop, duplicate-webhook idempotency, and escalation
summaries — and nothing else. A boundary test asserts the module contains no
EV formulas, caps, or completeness math; a dependency test asserts no lane
(ai/policy/tools) imports the orchestrator.

**One escalation exit.** Every failure mode — low AI confidence, LowConfidence
after repair, gate rejections that sink the decision, unsupported reason
codes, ambiguous links, exhausted retries, deadline danger — raises an
internal _Escalate signal handled in exactly one place, which writes the
merchant-facing summary (dispute, amount, hours, precise reason, missing/
conflicting items, whether any money action already happened) and the
CASE_ESCALATED audit entry with structured extras.

**Retry semantics.** Exponential backoff (base × 2^(attempt−1), injectable
sleep for tests), MAX 3 attempts, and the idempotency key is the dispute id
on every attempt — a retry can never mint a second action, and the executor
audits every failed attempt (ACTION_FAILED) plus the eventual submission or
the escalation carrying the prepared bundle.

**Deadline rules, deterministically.** After respond_by: hard block before
any adapter call. Before DECIDE: T−24h force-escalates (belt and braces with
the decision engine's own kill-switch). At ACT: an already-approved action
may execute inside the last 24h but never past the deadline.

**Terminal cases are never resumed.** Part 16's "ESCALATED → ACT invalid"
and the Stage-2 model's escalated→acted transition are reconciled: the
transition exists ONLY for a future explicit human-actor approval path; the
orchestrator refuses terminal cases outright (RUN_REFUSED audited) — tested.

**Duplicate webhooks.** Second delivery of the same dispute_id returns the
existing case, audits WEBHOOK_DUPLICATE, and cannot restart the workflow or
duplicate money (the actions-table idempotency is the last line anyway).

## ADR-011 — Evaluation methodology (Stage 9)

**The evaluator replays the real system.** run_eval feeds the held-out
events (duplicates included) through the actual Stage-8 Orchestrator against
a COPY of the world; no parallel pipeline, no manual outcomes, no curation.
Ground truth enters strictly after each case is terminal; a test asserts no
lane and not the orchestrator can even reference the ground-truth file.

**Frozen-set protection is code, not policy.** The harness fails loudly on
size != 40, dev/held-out overlap, seed mismatch, split-union mismatch, or
missing artifacts — silent regeneration is impossible.

**Metadata separated from metrics.** metrics.json splits a non-deterministic
meta block (timestamps, wall time, provider) from deterministic metrics/
cases blocks; the reproducibility test compares the deterministic blocks of
two full runs for equality.

**Honest economics, honest surprise.** Fee (Rs.500) charged on lost contests,
as configured. First run surfaced that contest-everything nets MORE on this
synthetic set (Rs.137,556 vs Rs.64,989). Diagnosis (not tuning): all 10
decision disagreements and the net gap trace to the 3 deferred reason codes
— Recourse escalates Rs.153,559 to humans (Rs.45,969 gt-winnable) rather
than fighting without a playbook, while the blind baseline fights and (in
simulation) mostly wins. Reported head-on with the actionable reading:
extend playbook coverage; the pending-winnable number quantifies exactly how
much that is worth. Escalation precision is reported STRICTLY (coverage-gap
escalations counted against it).

**Ablation is analysis, never pipeline surgery.** --ablate-gate recomputes
decisions with all candidates treated as admitted; the production gate is
untouched. On held-out data with the faithful stub it catches the planted
pincode-mismatch case flipping ESCALATE->FIGHT; the fabrication-blocking
value is demonstrated by the Stage-6 adversarial ablation tests.

**Found & fixed during the stage:** schema drift between code and the
committed generated world (Stage-3 dataset.db predated Stage-4's
evidence_key column; CREATE IF NOT EXISTS never migrates). Fix is
structural: PRAGMA user_version stamping with SchemaVersionError carrying
regeneration instructions — stale worlds now fail clearly, not cryptically.

## ADR-012 — API and dashboard (Stage 10)

**The API is a boundary, not a brain.** Flask routes read the store, replay
the deterministic gate for live check panels (the gate is pure, so
re-verification is free and honest — the UI shows live results, not stored
screenshots), and execute human decisions ONLY through the Stage-7 executor.
A test asserts the API never calls adapter action methods directly.

**Human approval = the reserved transition, exercised.** POST /approve
validates server-side (escalated-only, deadline not passed, FIGHT requires
gate-admitted evidence, attributable actor required), audits HUMAN_APPROVED,
executes with actor=HUMAN under the SAME idempotency key as the agent, and
walks ESCALATED->ACTED->CLOSED. POST /reject closes with zero money actions.
The frontend only shows buttons the backend reports as allowed — and the
backend re-checks everything anyway (a crafted request cannot bypass the
deadline or evidence rules; tested).

**Frontend without a build step.** npm has no network in this environment,
so the dashboard is a dependency-free static SPA (vanilla JS + CSS) served
by Flask — an honest constraint turned into a feature: zero supply chain,
instant load, one file each. Design per the frontend-design pass: ledger-
paper docket aesthetic, serif docket headers, monospace for everything
evidentiary, and the signature element — evidence exhibit cards with verdict
stamps, verbatim quotes, live check ledgers, and clickable [E#] citations
that flash the exhibit (the page's single animation; reduced-motion
respected). Every number on screen comes from the API; the UI computes
nothing.

**Demo clock honesty.** The synthetic world is frozen at sim_now; the API
pins its clock there when a data dir is supplied and says so in /health —
deadline countdowns in the demo are real relative to the world, not faked.

**Coverage gaps as a first-class screen.** The metrics view renders the
Stage-9 finding head-on: where contest-everything beats us, why (deferred
reason codes), and the exact winnable amount waiting on playbook coverage.
