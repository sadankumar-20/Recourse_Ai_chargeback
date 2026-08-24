# Recourse — an AI chargeback defense agent

**The AI can recommend. The policy engine can verify. Only the execution
layer can move money — and every step lands in a tamper-evident audit
chain.**

When a customer files a chargeback, an Indian SMB merchant has 4–7 days to
assemble courier proof, order records, and email evidence — or lose by
default. Recourse turns those scattered, messy records (including Hinglish
email threads) into a deadline-safe, evidence-verified defense: it fights
the disputes worth fighting, concedes the hopeless ones for less than the
fee, and escalates to a human with a summary naming exactly what's missing
and the hours remaining.

Built stage-by-stage for the Razorpay Student AI Builder Program; the git
history is the engineering story. Full design record: `docs/decisions.md`
(ADR-001…012) · [`ARCHITECTURE.md`](ARCHITECTURE.md) ·
[`docs/DEMO.md`](docs/DEMO.md).

## Quickstart

```bash
python3 -m pip install -r requirements.txt        # PyYAML + Flask
python3 data/generate.py --seed 42                # deterministic messy world
cd backend && python3 -m unittest discover -s tests && cd ..   # 207 tests, offline
python3 evals/run_eval.py --ablate-gate           # frozen held-out evaluation
python3 scripts/demo_seed.py && python3 scripts/serve.py       # dashboard :8000
```

Zero network required: the LLM defaults to a faithful deterministic stub and
payments to a labeled simulator. For the real model:
`RECOURSE_AI_PROVIDER=anthropic ANTHROPIC_API_KEY=…` (fails loudly if
unkeyed — never a silent fallback).

## What the agent does (spec §8, end to end)

webhook intake (duplicate deliveries idempotent) → deterministic-then-AI
order linking (confidence < 0.85 escalates; the system never guesses) →
document gathering → AI evidence extraction (verbatim quotes, untranslated)
→ **the Admissibility Gate** → deterministic FIGHT/ACCEPT/ESCALATE with full
EV math persisted → citation-locked drafting → idempotent execution with
exponential-backoff retries → CLOSED, or ESCALATED with a merchant-ready
summary. Humans approve/reject escalations through the same executor, same
idempotency, same audit chain.

### The Admissibility Gate (the differentiator)

The AI proposes evidence; only deterministic code admits it. Every claimed
field must appear **verbatim** in its source document *and* match the system
of record (shipment AWB, order pincode, order − refunds arithmetic).
Drafts may only cite admitted exhibits — a deterministic citation validator
has final authority, no bypass. **Hallucinated evidence in a money-bearing
filing is structurally impossible, not prompted away.** The eval's ablation
shows what gate-off would have shipped; adversarial tests show fabricated
quotes dying at the gate and fabricated documents dying even earlier, at
schema validation.

## Investigation modes (R2)

`RECOURSE_INVESTIGATION=fixed` (default) runs the Stage-8 predefined gather
path; `agentic` runs a bounded planner-and-tools loop: the model (or the
deterministic offline planner) decides what to check next via read-only,
budget-metered, audited tools, notices gaps (e.g. a missing POD), and can
query the courier's own tracking record to recover them. Both modes feed the
same unchanged extraction, Admissibility Gate, and decision engine. Dev-split
A/B (`evals/agentic_ab.json`): 11 missing-POD cases recovered, +33 admitted
evidence items, 3.1 avg tool calls, zero invalid requests or violations —
with the frozen held-out eval proven byte-identical.

## Held-out results (frozen 40 disputes, never tuned on, offline stub)

| metric | result |
|---|---|
| Decision agreement with ground truth | **75.0%** — every miss is a documented coverage gap; **zero wrong fights, zero wrong accepts** |
| Evidence extraction precision | **98.6%** |
| Automation rate / escalation rate | 42.5% / 57.5% (escalation precision reported strictly, coverage gaps counted against it) |
| Deadline compliance | **40/40** |
| Audit chains verified | **40/40** |
| Net recovered | **₹64,989** (+ ₹1,997 conceded deliberately; ₹153,559 escalated pending human action) |

Honest headline: contest-everything nets ₹137,556 on this synthetic set —
more than Recourse. The entire gap is money the agent refuses to fight
without a deterministic playbook (3 of 6 reason codes are deferred v1
scope), ₹45,969 of it ground-truth-winnable. `evals/report.md` prices that
coverage gap instead of hiding it; the dashboard's Evaluation tab renders it
head-on.

## Honest integration map

| Surface | Real or simulated |
|---|---|
| Anthropic LLM | Real when keyed; deterministic stub by default (tests 100% offline) |
| Razorpay payment/refund lookups | Real test-mode HTTP when credentialed (`RECOURSE_PAYMENTS_ADAPTER=razorpay_test`) |
| Dispute contest/accept lifecycle | Labeled simulator — Razorpay test mode cannot create synthetic disputes, so the real adapter raises `NotSupported` rather than faking responses |
| Dataset | Synthetic, scenario-driven (11 failure scenarios at spec §13 rates), byte-identical per seed; ground truth lives **outside** the app DB |

## Repository map

```
backend/app/ai/          AI lane: link · extract · draft (untrusted output,
                         one repair, LowConfidence; cannot import DB/tools)
backend/app/policy/      playbooks.yaml · Admissibility Gate · decision
                         engine · citation validator (zero LLM imports)
backend/app/tools/       executor (one money action per dispute, ever) +
                         payments adapters
backend/app/audit/       per-case SHA-256 hash chain + verifier
backend/app/store/       §12 schema, append-only audit API, schema-version
                         stamped worlds
backend/app/orchestrator.py  the §8 state machine — coordination only
backend/app/api.py       REST boundary + human approve/reject
backend/app/evals/       oracle · reports · held-out harness
frontend/                dependency-free dashboard (docket UI, evidence
                         exhibits, clickable citations, chain badge)
data/generate.py         deterministic world builder (seed 42)
evals/run_eval.py        frozen evaluation → metrics.json + report.md
scripts/                 demo_seed · serve · gate/decision reports · ai_smoke
docs/                    decisions.md (ADR-001…012) · DEMO.md · spec
```

## Verification

207 tests, all offline, ~7s: adversarial gate tests (19), tamper detection
(5 modes), executor idempotency across restarts, orchestrator failure drills
(retries, duplicate webhooks, expired deadlines, kill-switch), the human
approval matrix, anti-leakage and frozen-split protection, byte-identical
regeneration, and two-run eval determinism. Architectural invariants are
enforced by AST-scanning tests, not convention — the full invariant→test map
is in `ARCHITECTURE.md`.

## Known limitations

Synthetic world (architecture behavior under controlled messiness, not
production performance); the committed eval uses the deterministic stub —
re-run with the real provider for model-level numbers; ground truth derives
from the same policy caps the engine uses (consistency + coverage, not
independent judgment); 3 of 6 reason codes deferred by v1 scope and priced
in the report; frontend logic is exercised via API integration and static
checks (no browser harness in the build environment — the JS deliberately
computes nothing).
