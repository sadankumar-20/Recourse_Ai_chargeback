# Recourse — Architecture

One sentence: **the AI can recommend, the policy engine can verify, only the
execution layer can move money — and every step lands in a tamper-evident
audit chain.**

## The three lanes

```
                       ┌──────────────────────────────────────────┐
                       │  Dashboard (frontend/, dependency-free)  │
                       │  computes nothing; renders the API only  │
                       └───────────────────┬──────────────────────┘
                                           │ REST
                       ┌───────────────────▼──────────────────────┐
                       │        API  (backend/app/api.py)         │
                       │  human approve/reject → executor only    │
                       └───────────────────┬──────────────────────┘
                                           │
                       ┌───────────────────▼──────────────────────┐
                       │   ORCHESTRATOR (backend/app/orchestrator)│
                       │   coordination only: state walk, deadline│
                       │   guards, retries, escalation summaries  │
                       └──────┬───────────────┬──────────────┬────┘
              proposals only  │   verdicts &  │              │ approved
              (untrusted)     │   decisions   │              │ actions only
        ┌─────────────────────▼──┐  ┌─────────▼──────────┐  ┌▼──────────────────┐
        │  AI LANE  (app/ai)     │  │ POLICY LANE        │  │ EXECUTION LANE    │
        │  link · extract · draft│  │ (app/policy)       │  │ (app/tools)       │
        │  1 focused prompt each │  │ playbooks.yaml     │  │ executor:         │
        │  strict schema → one   │  │ ADMISSIBILITY GATE │  │  idempotency_key  │
        │  repair → LowConfidence│─▶│ decide (EV, caps,  │─▶│  = dispute_id     │
        │  cannot import DB or   │  │  kill-switch)      │  │  ONE money action │
        │  tools (AST-enforced)  │  │ citations validator│  │  per dispute EVER │
        │                        │  │ zero LLM imports   │  │ adapters: rzp_test│
        │                        │  │ (AST-enforced)     │  │  | simulator      │
        └────────────────────────┘  └────────────────────┘  └───────────────────┘
                    │                        │                       │
                    └────────────┬───────────┴───────────┬──────────┘
                                 ▼                       ▼
                       ┌──────────────────────────────────────────┐
                       │  STORE (app/store) + AUDIT (app/audit)   │
                       │  §12 schema · append-only per-case       │
                       │  SHA-256 hash chain touching EVERY arrow │
                       └──────────────────────────────────────────┘

          ┌───────────────────────────────────────────────────────┐
          │  EVAL (app/evals + evals/run_eval.py) wraps the whole  │
          │  pipeline as a black box over the 40 FROZEN held-out   │
          │  disputes; ground truth enters only after terminality  │
          └───────────────────────────────────────────────────────┘
```

**Dependency direction** (test-enforced): Frontend → API → Orchestrator →
{AI, Policy, Execution} → Store/Audit. No lane imports the orchestrator; the
AI lane cannot import the store's repository, sqlite3, or the tools lane;
the policy lane imports no AI and no network libraries.

## Where the Admissibility Gate sits

Between AI extraction and everything money-adjacent. Extraction is a
*proposal*; the gate is *admission*. One anti-fabrication pattern applied
uniformly: every claimed field must (1) appear **verbatim** in the source
document and (2) match the **system of record** (shipment AWB, order
pincode, order − refunds arithmetic). Drafts may only cite admitted
exhibits; a deterministic citation validator is the final authority with no
bypass. Hallucinated evidence in a filing is structurally impossible, not
prompted away.

## The single choke point for money

`tools/executor.execute_action` is the only code path that reaches an
adapter's contest/accept. It enforces persisted idempotency
(`idempotency_key = dispute_id` → one money action per dispute, *ever*,
including conflicting second actions), audits every path
(SUBMITTED / DUPLICATE / FAILED), and is used identically by the agent
(actor=agent) and the human-approval API (actor=human).

## Deterministic safety ladder

1. amount > ₹10,000 → human, always
2. deadline passed → all money actions hard-blocked (server-side too)
3. < 24h left → force-escalate before deciding
4. unreconciled amounts → escalate (the case file itself is suspect)
5. concede only when *nothing to fight with ever existed* (no shipment)
6. fight only when required evidence is admitted AND EV(fight) > EV(accept)
7. otherwise → escalate with a merchant-ready summary naming exactly what is
   missing and the hours remaining

## Invariants and the tests that enforce them

| Invariant | Enforced by |
|---|---|
| AI cannot touch DB/execution | `test_ai.TestStructuralPurity` (AST scan) |
| Policy has zero LLM/network imports | `test_gate.TestPolicyPurity` (AST scan) |
| Orchestrator contains no policy math | `test_orchestrator...no_policy_math` |
| One money action per dispute, ever | `test_payments.TestExecutorIdempotency` |
| Failed evidence preserved, never deleted | `test_gate` + `test_orchestrator` |
| Drafts cite only admitted evidence | `test_ai.TestDrafting` + `policy/citations` |
| No submission after the deadline | `test_orchestrator` + `test_api` (crafted requests refused) |
| Audit is append-only and tamper-evident | `test_store` + `test_audit_chain` (5 tamper modes) |
| Held-out set frozen, gt never leaks into the pipeline | `test_eval` (split tampering, anti-leakage) |
| API acts only through the executor | `test_api.TestSecurityBoundaries` |
| Same seed ⇒ byte-identical world; two eval runs ⇒ identical metrics | `test_datagen` + `test_eval` |

## Honest integration map

| Surface | Real or simulated |
|---|---|
| Anthropic LLM | Real when keyed (`RECOURSE_AI_PROVIDER=anthropic`); faithful deterministic stub otherwise — tests are 100% offline |
| Razorpay payment/refund lookups | Real test-mode HTTP when credentialed |
| Dispute contest/accept lifecycle | Labeled simulator; the real adapter raises `NotSupported` because Razorpay test mode cannot create synthetic disputes — we do not fake API responses |
| Dataset | Synthetic, scenario-driven, seed-reproducible; ground truth stored **outside** the app DB |

Full decision history: [`docs/decisions.md`](decisions.md) (ADR-001…012).


## FINAL SYSTEM DIAGRAM (R8)

```
USER ──► INTAKE (verbatim-first) ──► ORCHESTRATOR ──► AGENTIC INVESTIGATOR   [AI-CONTROLLED]
                                                        │ requests-as-data
                        ┌───────────────────────────────┤
                        │ read-only, budgeted, audited  ▼
                        │  search_orders · get_order · get_dispute · get_shipments
                        │  get_refunds · search_inbox · read_document
                        │  fetch_tracking · search_knowledge · find_similar_cases
                        ▼
                    EVIDENCE (user upload · tracking API · vision transcription ·
                              simulator/Razorpay-test data · KB citations)
                        ▼
                ADMISSIBILITY GATE  ── verbatim + system-of-record checks   [DETERMINISTIC]
                        ▼
                DECISION ENGINE  ── FIGHT / ACCEPT / ESCALATE (EV, versioned) [DETERMINISTIC]
                        ▼
                EXECUTOR ── the ONLY money writer, idempotent               [EXECUTION]
                        ▼
                AUDIT HASH CHAIN ── every step, tamper-evident              [AUDIT]
```
