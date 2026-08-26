# Recourse

**Agentic AI for merchant dispute recovery.**

When a chargeback arrives, Recourse doesn't blindly fight it. It
investigates: searches the merchant's records, checks shipment and payment
data, queries the courier's own tracking, retrieves relevant policy, asks
the merchant for missing evidence, verifies every claim deterministically,
calculates whether action is justified, executes only through a bounded
idempotent payment layer — and leaves a cryptographically verifiable trail.

> **AI investigates. Policy decides. Execution acts. Audit proves.**

This is not a chatbot. It is a bounded investigation system where the LLM
never touches money, policy, or truth.

## The problem

A real dispute never arrives as a clean record with obvious evidence. It
arrives as fragments: a customer email + a payment + an order + a shipment
+ courier tracking + a POD that may be missing + refund history + policy
requirements + a hard network deadline. Chargeback teams don't lose money
because they can't generate text — they lose it because assembling and
verifying this evidence is slow, and deadlines don't wait.

## Why not just an LLM?

An LLM can read the email and produce a plausible answer. A merchant cannot
safely let it decide "fight and move money." So Recourse separates powers:

```
UNTRUSTED INPUT -> AI INVESTIGATION -> EVIDENCE -> ADMISSIBILITY GATE
      -> DECISION ENGINE -> EXECUTOR -> AUDIT CHAIN
      (AI-controlled)      (deterministic)  (only money writer) (proof)
```

- The **investigator** (LLM or deterministic planner) chooses read-only,
  budget-metered, audited tools. It may say "check tracking"; it can never
  say "therefore FIGHT".
- The **Admissibility Gate** verifies every evidence claim: verbatim quote
  in a linked source document, AWB vs shipments, pincode vs order, amounts
  vs records. Vision transcriptions, uploads, tracking records, and KB
  citations all pass the same bar — a lying vision transcription is
  *inadmissible*, not persuasive (tested).
- The **decision engine** computes FIGHT / ACCEPT / ESCALATE from versioned
  playbooks and EV math. RAG cannot change it (ablation-proven: zero
  decision changes).
- The **executor** is the only financial writer: one idempotent action per
  dispute, ever. Deadlines are server-authoritative.
- The **audit hash chain** records every step tamper-evidently; the UI's
  Investigation Ledger renders only these events.

## The agentic loop (and when not to use it)

Fixed pipeline: link -> gather -> extract -> gate -> decide. Agentic:
understand -> plan -> read-only tool -> observe -> verify -> identify gaps
-> query tracking / ask the merchant (NEEDS_INPUT) -> resume -> finish.
Eval v2 shows clean, complete cases are handled identically by the fixed
path at zero tool cost — so batch replay defaults fixed, interactive intake
runs agentic. Use AI where the mess is.

## Evaluation (evals/v2_report.md — honest numbers, run twice, byte-identical)

| metric | fixed | agentic |
|---|---|---|
| automation | 42% | 60% |
| fixed escalations resolved by agent | — | **7** (0 regressions) |
| additional gate-admitted exhibits | — | +21 |
| avg / max tool calls (budget 12) | 0 | 3.27 / 6 |
| invalid tool calls / budget violations | 0 | 0 |
| deadline violations / invalid chains | 0 | 0 |
| prompt-injection vectors blocked | — | 4/4, unsafe actions 0 |
| recoverable-gap resolution (courier blinded) | — | 7/7 |
| net money delta on v1 labels | — | **−₹3,500** |

**The negative number, explained (not hidden):** the 7 recovered
cases are missing-POD disputes whose frozen v1 outcome labels price fights
at a 10% win rate — labels authored under the *fixed pipeline's* capability
assumption, i.e. for a world where that evidence couldn't exist. Eval v2
therefore demonstrates an agentic **capability** improvement but does not
claim positive incremental revenue from those labels. We found the
limitation instead of tuning the model until the graph looked good.

## Real vs simulated integrations

| Surface | Default | Real mode | Labeling |
|---|---|---|---|
| AI (planner/triage/drafts) | deterministic stub | `RECOURSE_AI_PROVIDER=anthropic` + `ANTHROPIC_API_KEY` | /health: real/simulator |
| Tracking | simulator | `RECOURSE_TRACKING=aftership` + `AFTERSHIP_API_KEY` | provenance `tracking_api` vs `simulator`; loud failure, never silent fallback |
| Vision (POD images) | unavailable (honest 415) | Anthropic vision via the AI key | provenance `vision_transcribed` |
| Payments | simulator (labeled) | Razorpay test-mode lookups; dispute lifecycle honestly `NotSupported` | every action response carries `simulated` |
| Knowledge | local versioned KB | local (by design) | provenance `kb_local` |

## Quickstart

```bash
git clone https://github.com/sadankumar-20/Recourse_Ai_chargeback && cd Recourse_Ai_chargeback
python3 data/generate.py --seed 42          # deterministic world (no deps)
cd backend && python3 -m unittest discover -s tests && cd ..   # 299 green, zero network
python3 scripts/demo_seed.py                # seeds cases incl. a live NEEDS_INPUT demo
python3 scripts/serve.py                    # open http://localhost:8000
python3 evals/run_eval_v2.py                # the proof, reproduced on your machine
```

## Environment variables

`RECOURSE_AI_PROVIDER` (stub|anthropic), `ANTHROPIC_API_KEY`,
`RECOURSE_TRACKING` (simulator|aftership), `AFTERSHIP_API_KEY`,
`RECOURSE_KNOWLEDGE` (true|false — graceful structured error when off),
`RECOURSE_INVESTIGATION` (fixed|agentic). All optional; every absence is
labeled, never silently faked.

## Known limitations

Synthetic evaluation world at modest scale; v1 outcome-label limitation
above; real Anthropic/AfterShip paths implemented and offline-tested to the
transport boundary but requiring credentials to exercise live; browser
voice depends on the browser engine; the ledger's liveness is
reveal-plus-polling, not streaming (the orchestrator is synchronous).

## Documentation map

`docs/ARCHITECTURE.md` · `docs/DEMO_SCRIPT.md` · `docs/PANEL_STORY.md` ·
`docs/PANEL_QA.md` · `docs/BUILD_TIMELINE.md` · `docs/VIDEO_SCRIPT.md` ·
`docs/decisions.md` (ADR-001…019, including the negative finding) ·
`docs/DEPLOY.md`.

*A student project for the Razorpay Student AI Builder program — not an
official Razorpay product; an architecture demonstration for merchant
revenue-recovery workflows.*
