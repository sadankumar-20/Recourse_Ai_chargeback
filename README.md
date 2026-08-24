# Recourse — AI Chargeback Defense Agent

An agent that turns a merchant's scattered order, delivery, and email records into a
deadline-safe, evidence-verified chargeback defense — fighting the disputes worth
fighting, safely conceding the ones that aren't, and structurally unable to cite
evidence it cannot prove exists.

Built for the Razorpay Student AI Builder Program. Single source of truth:
[`recourse_spec.md`](../recourse_spec.md) (§8 workflow, §12 data model, §30 build spec).

## Architecture (three-lane separation)

```
AI lane        backend/app/ai/       LLM reasoning only: link, extract, draft.
                                     Output is UNTRUSTED until validated.
Policy lane    backend/app/policy/   Deterministic: playbooks, Admissibility Gate,
                                     decision math, citation validator. No LLM imports.
Execution lane backend/app/tools/    Payments adapter (Razorpay test mode | labeled
                                     simulator), shipping mock, mail store.
                                     Only the orchestrator may act, post-policy.
```

Supporting: `store/` (SQLite repositories), `audit/` (append-only hash-chained log),
`evals/` (held-out evaluation harness), `frontend/` (dashboard).

## Honest integration table

| Call | Real or mock |
|---|---|
| Anthropic LLM API | Real when `ANTHROPIC_API_KEY` set; deterministic stub in tests |
| Razorpay payments/refunds fetch | Real test-mode HTTP (`RazorpayTestAdapter`) when credentials are configured |
| Dispute contest/accept lifecycle | Labeled simulator (`SimulatorAdapter`) — Razorpay test mode cannot create synthetic disputes to contest, so the real adapter raises `NotSupported` instead of faking responses |
| Shipping (Shiprocket-shaped) & email store | Mock over the synthetic dataset |

Every adapter response records which implementation served it.

## End-to-end lifecycle

`Orchestrator.process_event({"event": "dispute.created", "dispute_id": ...})`
drives one case through the §8 machine: intake (duplicate webhooks
idempotent) → deterministic-then-AI linking (confidence < 0.85 escalates,
never guesses) → gather → AI extraction → Admissibility Gate (failed evidence
preserved with exact reasons) → deterministic decision (full EV math
persisted) → citation-validated draft (FIGHT only) → executor with
exponential-backoff retries under one idempotency key → CLOSED, or ESCALATED
with a merchant-ready summary. Every step lands in the per-case audit hash
chain; `format_timeline(repo, case_id)` reconstructs the whole run from it.
Deadlines are enforced deterministically: T−24h force-escalate before
deciding, hard block on all money actions after respond_by.

## Dataset

```bash
python3 data/generate.py --seed 42
```

Generates a deterministic, deliberately messy world: 800 orders, 751
shipments, 746 documents (PODs + Hinglish/English email threads), 120 disputes
across 6 reason codes and 11 failure scenarios, a webhook feed with duplicate
deliveries (`data/events.jsonl`), hidden eval labels
(`data/ground_truth.json`, never stored in the app DB), and a frozen,
stratified 80-dev / 40-held-out split (`data/split.json` — committed; the
held-out set is for final evaluation only, never for tuning).

## AI configuration

`RECOURSE_AI_PROVIDER=stub` (default — deterministic, offline, no key needed)
or `anthropic` with `ANTHROPIC_API_KEY` set (fails loudly if missing; never
silently falls back). Model: `RECOURSE_LLM_MODEL` (default claude-sonnet-4-6).
Live smoke test: `scripts/ai_smoke.py`. The test suite never touches the
network.

## Payments configuration

`RECOURSE_PAYMENTS_ADAPTER=simulator` (default) or `razorpay_test` with
`RAZORPAY_KEY_ID`/`RAZORPAY_KEY_SECRET` (fails loudly if missing). Every
contest/accept is idempotent (`idempotency_key = dispute_id`: one money
action per dispute, ever) and lands in a per-case SHA-256 audit hash chain —
`verify_audit_chain(repo, case_id)` detects any modification, deletion, or
reordering and reports exactly where the chain broke.

## Evaluation

```bash
python3 data/generate.py --seed 42     # build the frozen world
python3 evals/run_eval.py --ablate-gate
```

Replays the 40 frozen held-out disputes (never used for tuning; the harness
fails loudly on any split alteration) through the real orchestrator, ingests
simulated outcomes, and writes `evals/metrics.json` + `evals/report.md`.
Baselines: never-contest (net ₹0) and contest-everything (fights blind, fee
on losses). The gate ablation (`--ablate-gate`) is analysis-only — it counts
what inadmissible evidence would have shipped and which decisions would flip
with the gate off; the production pipeline always keeps the gate on.
Committed results (offline stub provider, seed 42): 75% decision agreement —
every miss a documented coverage gap, zero wrong fights/accepts — 42.5%
automation, 0 deadline violations, 40/40 audit chains valid, net ₹64,989
recovered with ₹153,559 escalated pending human action. See
`evals/report.md` for the full honest breakdown including where
contest-everything beats us and why.

## Dashboard & demo

```bash
python3 data/generate.py --seed 42     # build the world
python3 scripts/demo_seed.py           # run curated cases into demo.db
python3 scripts/serve.py               # http://127.0.0.1:8000
```

Case queue with deadline urgency, docket-style case files (evidence exhibits
with live deterministic check panels, decision math, citation-locked drafts
where clicking [E3] flashes the exhibit), tamper-evident audit timelines, a
human review panel (approve fight / accept / reject — all server-validated,
idempotent, executed through the same executor as the agent), and the
held-out evaluation view including exactly where the system stops and what
that coverage costs.

## Running

```bash
python3 -m pip install -r requirements.txt   # PyYAML + Flask
cd backend
python -m unittest discover -s tests -v   # all tests, offline
```

(App entrypoint, dataset generator, and eval harness arrive in later stages —
see git history; each commit is one verified stage.)

## Environment notes

Developed against Python 3.12 stdlib + Flask. See `docs/decisions.md` for why
this deviates from the FastAPI/SQLAlchemy/pydantic stack named in the spec —
the architecture and invariants are unchanged.
