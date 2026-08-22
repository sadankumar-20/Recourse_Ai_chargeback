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

## Running

```bash
python3 -m pip install -r requirements.txt   # PyYAML (+ Flask for later stages)
cd backend
python -m unittest discover -s tests -v   # all tests, offline
```

(App entrypoint, dataset generator, and eval harness arrive in later stages —
see git history; each commit is one verified stage.)

## Environment notes

Developed against Python 3.12 stdlib + Flask. See `docs/decisions.md` for why
this deviates from the FastAPI/SQLAlchemy/pydantic stack named in the spec —
the architecture and invariants are unchanged.
