# RECOURSE — An AI Chargeback Defense Agent
### Razorpay Student AI Builder Program · Full Project Specification

---

## 0. Why this idea won (selection analysis)

Candidate ideas considered against the scoring rubric:

| Idea | Pain | Unique | AI need | Agentic depth | Biz value | Feasible | Rzp fit | Wow | Failure-handling | MVP speed | **Total /100** |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Smart dunning / failed-payment retry (Revenue Recovery) | 8 | 4 | 5 | 6 | 8 | 8 | 9 | 5 | 6 | 8 | 67 |
| Settlement reconciliation exception agent (Finance Controller) | 8 | 6 | 6 | 6 | 8 | 7 | 9 | 5 | 7 | 7 | 69 |
| Refund-request triage & execution agent | 7 | 5 | 6 | 7 | 7 | 9 | 9 | 6 | 7 | 9 | 72 |
| **Chargeback defense (dispute representment) agent** | **9** | **8** | **9** | **9** | **9** | **8** | **9** | **9** | **9** | **7** | **86** |

**Why disputes/chargebacks:**

- **Real, quantifiable pain.** When a cardholder disputes a payment, the merchant must respond with structured evidence before a hard deadline. Miss the deadline or submit weak evidence → automatic loss of the disputed amount plus a chargeback fee. For a small Indian D2C merchant doing ₹10–50L/month, disputes are pure margin bleed, and most are lost **by default** — not because the merchant was wrong, but because assembling evidence (order record + courier proof-of-delivery + customer email thread) under a 4–7 day deadline is miserable manual work nobody owns.
- **The inputs are genuinely messy.** Order data in Shopify exports/Sheets, delivery proof in Shiprocket/Delhivery webhooks, customer intent buried in Hinglish email threads. Perfect fit for *AI handles ambiguity*.
- **The stakes are exactly where AI must NOT be trusted alone.** Deadlines, money math, "which evidence is admissible," and "is this worth fighting" must be deterministic. This project's architecture *is* the AI-plus-policy-engine principle, not a bolt-on.
- **It closes the loop.** The agent doesn't detect — it files (or safely concedes) a real dispute response via API, with an audit trail and a measured win rate.
- **It's not on the "generic" blacklist.** It is not fraud *detection* (that's the issuer's side). It is the merchant's *defense* — an overlooked, adjacent problem.
- **Existing solutions are insufficient.** Enterprise chargeback tools (Chargeflow, Justt, etc.) target US Stripe/Shopify merchants, are expensive black boxes, and don't explain *why* they fought or conceded. Razorpay's dashboard notifies merchants of disputes but the case-building remains manual. Nothing serves the Indian SMB with an explainable, bounded agent.

---

## 1. Project name
**Recourse** — every merchant deserves one.

## 2. One-line pitch
> Recourse is an AI agent that turns a merchant's scattered order, delivery, and email records into a deadline-safe, evidence-verified chargeback defense — automatically fighting the disputes worth fighting, safely conceding the ones that aren't, and never citing evidence it can't prove exists.

## 3. Razorpay track
**AI Revenue Recovery** (primary), with strong overlap into **AI Risk Manager**. Disputed revenue that would be lost by default is recovered through timely, well-evidenced representment.

## 4. The real-life problem
A customer files a chargeback: *"goods not received"* on a ₹3,499 order. Razorpay (on behalf of the card network) opens a dispute with a **respond-by deadline**. The merchant must produce specific evidence — AWB number, courier proof-of-delivery, address match, the customer's own email saying "package received, want refund anyway" — formatted as a representment, before the deadline. In reality: the founder sees the email three days late, the delivery proof is in a Shiprocket account the intern manages, the customer email is in a shared Gmail, and the deadline passes. **Default judgment: merchant loses money it was owed.**

## 5. Why the problem matters
- Industry-wide, a large share of "friendly fraud" disputes (item received but disputed anyway) are **winnable with evidence** — and lost without it.
- Each loss = disputed amount + chargeback fee + inventory already shipped. For thin-margin D2C, 10 lost disputes/month can erase the profit of 200 good orders.
- The cost isn't fraud sophistication — it's **operational friction under a deadline**. That is precisely an agent-shaped problem.

## 6. Why existing approaches are insufficient
- **Manual process:** slow, error-prone, misses deadlines; evidence requirements differ per dispute reason code and nobody memorizes them.
- **Dashboard notifications:** tell you a dispute exists; don't build the case.
- **Enterprise chargeback SaaS:** US/Stripe-centric, per-dispute pricing unaffordable for Indian SMBs, opaque ("we handled it") — no explainability, no policy control, no visibility into what evidence was used.
- **Naive LLM approach ("ask ChatGPT to write a rebuttal"):** hallucination risk is catastrophic here — a fabricated tracking number in a representment is worse than losing the dispute.

## 7. Target users
Indian SMB / D2C merchants on Razorpay (₹5L–₹5Cr monthly volume) with 5–100 disputes per month and no dedicated risk-ops person. Secondary: agencies managing multiple merchant accounts.

---

## 8. Complete agent workflow (end-to-end, closes the loop)

```
[1] TRIGGER      dispute.created webhook (Razorpay test mode / mock adapter)
                 → payload: dispute_id, payment_id, amount, reason_code, respond_by
[2] INTAKE       Deterministic: create Case in DB, start audit log, compute
                 deadline clock. Duplicate dispute_id → idempotent no-op (logged).
[3] LINK         Find the order behind the payment.
                 Deterministic first: exact match on payment_id / amount+email.
                 AI second: fuzzy match when IDs are missing/mangled
                 (e.g., customer used a different email; amount differs by a
                 partial refund). AI outputs candidate matches WITH confidence;
                 policy engine requires confidence ≥ threshold or → ESCALATE.
[4] PLAYBOOK     Deterministic: reason_code → required-evidence checklist.
                 e.g. GOODS_NOT_RECEIVED → {AWB, POD with OTP/signature,
                 ship-to address == order address, ship date ≤ promised date}
[5] GATHER       Agent calls tools: orders DB, shipping API (mock Shiprocket),
                 comms store (email threads). Raw documents attached to Case.
[6] EXTRACT (AI) LLM extracts candidate Evidence objects from messy sources:
                 {claim, source_doc_id, quoted_span, extracted_fields}.
                 e.g. from a Hinglish email thread: "customer acknowledged
                 delivery on 12 Aug" + exact quoted line.
[7] ADMISSIBILITY GATE (deterministic — the differentiator, see §20)
                 Every Evidence object is validated in code:
                 • quoted_span actually exists verbatim in source_doc
                 • timestamps coherent (POD date ≥ ship date; email ≤ today)
                 • cross-refs check out (AWB in POD == AWB on order)
                 • amounts within tolerance (order − refunds == disputed amt)
                 Only PASS evidence gets an Evidence ID and enters the case file.
                 FAIL evidence is kept, visible, and marked inadmissible+reason.
[8] DECIDE       Deterministic decision engine:
                 completeness = passed_items / checklist_items
                 EV(fight) = p_win(completeness, reason_code) × amount − fee_cost
                 → FIGHT if EV > EV(accept) AND completeness ≥ floor
                 → ACCEPT if evidence hopeless AND amount ≤ auto_accept_cap
                 → ESCALATE otherwise, or ALWAYS if amount > ₹10,000,
                   deadline < 24h, or any confidence flag was raised.
[9] DRAFT (AI)   LLM writes the representment narrative. Constraint: every
                 factual sentence must cite an admitted Evidence ID like [E3].
                 A deterministic post-validator parses the draft; any uncited
                 factual claim or unknown ID → reject, one retry with the
                 validator errors, then ESCALATE. Hallucination is structurally
                 blocked, not prompted away.
[10] ACT         Bounded action through the payments adapter:
                 • FIGHT  → POST contest with evidence bundle (test mode/mock),
                   idempotency key = dispute_id, single submission only
                 • ACCEPT → accept dispute (only under cap, logged as money
                   decision with EV math attached)
                 • ESCALATE → generate a human task + a drafted email to the
                   merchant listing EXACTLY which evidence is missing and the
                   hours remaining on the deadline
[11] RECORD      Append-only audit log of every step: inputs, AI outputs,
                 gate verdicts, decision math, API request/response, actor.
[12] MEASURE     When outcome webhook arrives (won/lost), close the loop:
                 update metrics, recovered ₹, per-reason-code win rates.
```

## 9. What the AI does (and only this)
- Fuzzy order↔dispute matching under ambiguity (with confidence output).
- Evidence **extraction** from unstructured/multilingual text into structured, source-quoted claims.
- Interpretation of customer communications (did the customer acknowledge receipt? request cancellation after shipping?).
- Drafting the representment narrative — citation-constrained.
- Summarizing the case for the human reviewer on escalation.

## 10. What deterministic code does (and always this)
- Deadline math and the deadline kill-switch (no submission attempt after respond_by; escalate at T−24h).
- Reason-code → evidence checklist mapping (a versioned YAML playbook, not a prompt).
- The Admissibility Gate: verbatim-quote verification, timestamp coherence, cross-reference checks, amount arithmetic with refund reconciliation.
- Fight/accept/escalate decision math, thresholds, and monetary caps.
- Idempotency, duplicate detection, single-submission enforcement.
- Citation validation of the AI's draft.
- All API execution, all state transitions, all audit writes.

The agent's explanation is literally: **"I recommend contesting because the customer's own email [E3] acknowledges delivery — and the policy engine verified the quote exists verbatim, the POD date precedes the email, the AWB matches the order, and the deadline allows submission."**

## 11. Tools / APIs used
| Tool | Real vs mock | Purpose |
|---|---|---|
| Razorpay test-mode API (payments, refunds; disputes endpoints where available in test mode) | Real test mode behind an adapter | Payment/refund ground truth; contest submission |
| `PaymentsAdapter` mock | Honest, clearly-labeled mock implementing the same interface | Dispute lifecycle simulation (created → under_review → won/lost) so the demo can show outcomes |
| Shipping API mock (Shiprocket-shaped) | Mock with realistic payloads incl. PODs | Delivery evidence |
| Comms store | Local mailbox of synthetic email threads | Customer communications |
| LLM API (Claude / any) | Real | Steps 3, 6, 9 only |

**Integrity rule (per Razorpay's constraint):** the adapter pattern makes real vs mocked explicit. The demo states plainly which calls hit Razorpay test mode and which hit the labeled simulator. Nothing is faked-as-real.

## 12. Data model (core tables)
```
merchants(id, name, auto_accept_cap, escalation_amount_cap)
orders(id, merchant_id, payment_id, amount, customer_email, address, created_at, promised_ship_by)
refunds(id, order_id, amount, created_at)
shipments(id, order_id, awb, courier, ship_date, status, pod_doc_id)
documents(id, case_id, type[email|pod|invoice|log], raw_text, source, fetched_at)
disputes(id, payment_id, amount, reason_code, respond_by, status)
cases(id, dispute_id, state[intake|linking|gathering|gated|decided|acted|closed|escalated], linked_order_id, link_confidence)
evidence(id, case_id, claim, source_doc_id, quoted_span, fields_json, gate_verdict[PASS|FAIL], fail_reason)
decisions(id, case_id, action[FIGHT|ACCEPT|ESCALATE], completeness, p_win, ev_fight, ev_accept, thresholds_version)
actions(id, case_id, type, idempotency_key, request_json, response_json, actor[agent|human], at)
audit_log(id, case_id, step, payload_json, at)   -- append-only
outcomes(id, case_id, result[won|lost|accepted|expired], amount_recovered)
```

## 13. Synthetic dataset design (production-realistic)
Generator script (`data/generate.py`, fixed seed) produces **120 disputes** over ~800 orders, 6 reason codes (goods_not_received, not_as_described, duplicate, fraud, credit_not_processed, cancelled_recurring). Injected imperfections, each mirroring a real production failure mode:

- **Missing fields (15%):** shipment exists but POD doc absent (courier never uploaded it).
- **Conflicting records (8%):** courier status says "delivered", customer email says "never received"; or two orders share amount+date.
- **Duplicates (5%):** same dispute webhook delivered twice; duplicate order rows from a re-import.
- **Partial refunds (10%):** disputed amount ≠ order amount until refunds are reconciled.
- **Ambiguous text:** Hinglish emails ("bhaiya parcel mil gaya but size galat hai, refund kar do"), sarcasm, threads where the key admission is 14 messages deep.
- **Delayed events (7%):** dispute webhook arrives with < 36h left on the deadline.
- **Edge cases:** address typos (pincode transposed), customer email differing from order email, an order cancelled after shipping.

Every dispute carries hidden **ground-truth labels**: `gt_correct_action`, `gt_evidence_present`, `gt_outcome_if_fought`. Split: **80 development / 40 held-out test** (held-out never touched during prompt or threshold tuning).

## 14. System architecture
Deliberately monolithic and buildable:

```
┌────────────── React (Vite) dashboard ──────────────┐
│ Case queue · Case file (evidence PASS/FAIL) ·      │
│ Decision math panel · Audit timeline · Metrics tab │
└───────────────▲────────────────────────────────────┘
                │ REST
┌───────────────┴───────────── FastAPI backend ──────┐
│ Orchestrator: explicit state machine per case      │
│  (no heavyweight agent framework — states in §8)   │
│ ├─ ai/        llm_link.py  llm_extract.py          │
│ │             llm_draft.py   (each: one focused    │
│ │             prompt, JSON-schema-validated output)│
│ ├─ policy/    gate.py  playbooks.yaml  decide.py   │
│ │             citations.py  (pure functions,       │
│ │             100% unit-tested, no LLM imports)    │
│ ├─ tools/     payments_adapter.py (razorpay_test | │
│ │             simulator)  shipping_mock.py  mail.py│
│ ├─ store/     SQLite via SQLAlchemy                │
│ ├─ audit/     append-only writer + hash chain      │
│ └─ evals/     run_eval.py → metrics.json + report  │
└────────────────────────────────────────────────────┘
Observability: structured JSON logs per case step; /metrics endpoint;
every LLM call logged with prompt hash, latency, token count.
```

## 15. Evaluation methodology
1. Freeze prompts, playbooks, thresholds → tag a release.
2. Run the full pipeline on the **40 held-out disputes** end-to-end (simulated clock).
3. Score against ground truth. Simulate outcomes: fought cases resolve per `gt_outcome_if_fought`.
4. Report **two tables**: what the agent handled autonomously, and what it escalated / could not confidently handle — with reasons. (The second table is presented proudly, not hidden.)
5. Ablation: same run with the Admissibility Gate disabled → show hallucinated/inadmissible citations that would have shipped. This one chart justifies the whole architecture.

## 16. Metrics
- **Decision accuracy:** agent action vs `gt_correct_action` (target ≥ 85% on held-out).
- **Evidence extraction:** precision / recall vs `gt_evidence_present`.
- **Hallucination rate in submitted narratives:** measured by citation validator = **0 by construction**; ablation shows the counterfactual rate.
- **Automation rate** (cases resolved without human) vs **escalation rate** — and escalation *precision* (were escalations genuinely hard?).
- **Deadline compliance: 100%** (hard invariant; a single violation fails the eval).
- **Simulated ₹ recovered** vs two baselines: "never contest" (status quo) and "contest everything blindly".
- **False-fight cost:** fees burned on lost contests.
- **Median handling time:** minutes from webhook to action (vs days manually).

## 17. Failure scenario & recovery (three demonstrated live)
1. **Missing/contradictory evidence:** POD pincode ≠ order pincode. Gate FAILs address-match with the exact reason. Agent does **not** paper over it — case → ESCALATE, and the system drafts a merchant email: *"To contest dispute #D-1042 (₹3,499, deadline in 61h) we still need: proof of delivery to pincode 560037. Courier POD shows 560073."* Fail-safe, specific, actionable.
2. **API failure on submission:** payments adapter returns 503. Retry with exponential backoff (max 3), same idempotency key; if still failing at T−12h → escalate with the prepared bundle so a human can submit manually. No duplicate submission possible.
3. **Low-confidence AI output:** order-linking confidence 0.58 < 0.85 threshold → policy engine refuses autonomous progress; human sees both candidate orders side-by-side with the AI's reasoning.

## 18. Safety & guardrails
- Monetary caps: auto-ACCEPT only ≤ ₹2,000; any case > ₹10,000 always human-approved.
- Deadline kill-switch; T−24h forced escalation if undecided.
- Single-submission invariant via idempotency keys.
- AI never calls APIs directly — it returns structured proposals; only the orchestrator executes, post-policy.
- Versioned playbooks/thresholds (decisions record which version judged them).
- No customer-facing autonomous messaging; merchant-facing drafts only.
- Reversibility: ESCALATE and drafts are reversible; the two irreversible actions (contest, accept) are exactly the two behind gates.

## 19. Audit trail design
Append-only `audit_log` with a per-case **hash chain** (each entry stores hash(prev_entry)) — tampering is detectable. Every entry: step, full input/output payloads, model+prompt version or policy version, actor. The dashboard renders it as a case timeline; the demo scrolls it. Answerable question for any action: *who/what decided this, based on which evidence, under which policy version?*

## 20. Unique differentiator — **The Admissibility Gate**
The memorable feature: **the AI is a lawyer that can only argue from admitted evidence.** Extraction (AI) is separated from admission (deterministic verification: verbatim-quote existence, timestamp coherence, cross-reference and amount checks), and generation is citation-constrained to admitted Evidence IDs with a deterministic post-validator. Hallucination in a money-bearing document isn't mitigated — it's **structurally impossible**. Panel one-liner: *"Our LLM cannot lie in a filing, because the clerk checks every citation before the court sees it."* The gate-off ablation chart is the proof.

## 21. MVP scope (must exist)
Webhook intake → deterministic+AI linking → playbooks for **3 reason codes** → tool gathering → AI extraction → Admissibility Gate → decision engine → citation-constrained drafting → contest/accept/escalate via adapter → audit log → eval harness on held-out set → dashboard (case file + audit timeline + metrics).

## 22. Stretch features
Remaining 3 reason codes; p_win calibrated from simulated history instead of playbook priors; merchant-configurable policy UI; PDF POD parsing (vision); multi-merchant tenancy; a "pre-dispute" mode that flags refund-request emails likely to become chargebacks.

## 23. 12-day implementation plan
- **D1:** Repo, schema, state machine skeleton, adapter interfaces. **D2:** Synthetic data generator + labels + split.
- **D3:** Playbooks YAML + Admissibility Gate + decision engine, all unit-tested (pure Python, no LLM yet).
- **D4:** LLM linking + extraction with JSON-schema validation. **D5:** Citation-constrained drafting + post-validator.
- **D6:** Payments adapter (Razorpay test mode + simulator), idempotency, action execution. **D7:** Audit hash-chain + failure paths (retry, escalation emails, kill-switch).
- **D8:** Eval harness + baselines + ablation. **D9:** Dashboard. **D10:** Threshold tuning on dev set only; freeze; held-out run.
- **D11:** Demo script, seeded demo cases, record video. **D12:** README, architecture diagram, buffer.

## 24. 5-minute demo storyline
0:00 the problem (one lost-dispute story, real numbers) → 0:45 webhook fires live, case appears with deadline clock → 1:15 agent links order, gathers docs; show the messy Hinglish email → 2:00 extraction + **Gate screen**: green PASS rows with verified quotes, one red FAIL with reason → 2:45 decision math panel (completeness, p_win, EV) → 3:15 draft with [E#] citations; validator badge; contest submitted (test mode), audit timeline scrolls → 3:45 **failure case live**: pincode mismatch → escalation email with exact ask → 4:20 metrics: held-out accuracy, ₹ recovered vs baselines, the gate-off ablation chart → 4:50 close: "small merchants stop losing money by default."

## 25. GitHub repository contents
`README` (problem, architecture diagram, honest real-vs-mock table, results incl. failures), `policy/` with unit tests and coverage badge, `data/generate.py` + fixed seed, `evals/` + committed `metrics.json` + eval report with the "could not handle" table, prompt files versioned, demo seed script, short ARCHITECTURE.md, LICENSE, and a `docs/decisions.md` recording why no agent framework was used.

## 26. Architecture diagram must explain
The three-lane separation (AI lane / policy lane / execution lane), where the Admissibility Gate sits between extraction and drafting, the single choke-point where irreversible actions execute, the audit writer touching every arrow, and the eval harness wrapping the whole pipeline as a black box.

## 27. What could go wrong during development
- LLM extraction returns malformed/inconsistent JSON.
- p_win priors feel arbitrary; EV decisions look hand-wavy.
- Razorpay test mode lacks a full dispute-contest lifecycle → integration gap.
- Citation validator too strict → everything escalates (automation rate collapses).
- Time sink on dashboard polish.
- Synthetic data too clean → agent looks unrealistically good.

## 28. Recovery for each
- Schema-validated outputs with one repair-retry, then escalate (this *is* the failure path, so it demos well).
- Present p_win as versioned, merchant-tunable playbook priors; show sensitivity analysis instead of pretending calibration.
- Adapter pattern + labeled simulator; state the gap explicitly — honesty is scored here.
- Tune the validator on the dev split only; track escalation precision so strictness is measured, not vibes.
- Dashboard is server-rendered tables first, styling last; the audit timeline matters more than CSS.
- Imperfection injection rates are parameters; the eval report proves messiness (counts per imperfection class).

## 29. Why this stands out to a Razorpay panel
It sits exactly on Razorpay's rails (disputes are their object model); it recovers measurable money rather than generating text; the architecture answers the question every payments company asks about LLMs — *how do you stop it from lying with money on the line?* — with a structural answer, not a prompt; failures are demonstrated, measured, and designed-for; and the whole thing is honest: real test-mode where possible, labeled simulation where not, held-out metrics, and a proud "what we couldn't handle" table. It reads like an internal Razorpay risk-ops tool, not a college demo.

---

## 30. "BUILD THIS" — specification for a coding agent

> Paste everything below into Claude Code / Cursor as the implementation brief.

**Build a monolithic Python 3.11 FastAPI application called `recourse` with a React (Vite + TS) dashboard, implementing an AI chargeback-defense agent. No agent frameworks; use an explicit state machine.**

**Repo layout:** `backend/app/{orchestrator.py, ai/, policy/, tools/, store/, audit/, evals/}`, `frontend/`, `data/generate.py`, `tests/`.

**1. Storage (SQLite + SQLAlchemy):** implement the tables in §12 verbatim. `audit_log` is append-only; each row stores `sha256(prev_row_hash + payload)`.

**2. Synthetic data:** `data/generate.py --seed 42` creates 800 orders, shipments, refunds, email threads, and 120 disputes across 6 reason codes with imperfection injection at the rates in §13, plus hidden ground-truth labels (`gt_correct_action ∈ {FIGHT, ACCEPT, ESCALATE}`, `gt_evidence_present: list`, `gt_outcome_if_fought ∈ {won, lost}`). Write `data/split.json` fixing 80 dev / 40 held-out dispute IDs.

**3. Policy package (`policy/`, zero LLM imports, 100% pytest coverage):**
- `playbooks.yaml`: for reason codes `goods_not_received`, `not_as_described`, `duplicate`: required evidence items, each with machine-checkable predicates, plus `p_win_prior` per completeness band.
- `gate.py`: `admit(evidence, docs, order, shipments, refunds) -> Verdict(PASS|FAIL, reason)`. Checks: `quoted_span in doc.raw_text` (exact substring), timestamp coherence, AWB cross-match, `order.amount − sum(refunds) == dispute.amount ± ₹1`.
- `decide.py`: completeness = passed/required; `ev_fight = p_win×amount − fee(500)`; `ev_accept = −amount`; rules: FIGHT if `ev_fight > ev_accept` and completeness ≥ 0.75 and amount ≤ 10000 and hours_left ≥ 24; ACCEPT if completeness ≤ 0.25 and amount ≤ 2000; else ESCALATE. Return the full math for the audit log.
- `citations.py`: parse draft; every sentence containing a number, date, name, or factual assertion must contain `[E<n>]` where n is an admitted evidence id; return violations.

**4. AI package (`ai/`), each function = one LLM call, JSON-schema-validated (pydantic), one repair retry then raise `LowConfidence`:**
- `link_order(dispute, candidate_orders) -> {order_id, confidence, reasoning}`
- `extract_evidence(case_docs, checklist) -> [ {claim, source_doc_id, quoted_span, fields} ]` — quoted_span must be verbatim.
- `draft_representment(admitted_evidence, dispute, order) -> markdown draft citing [E#]`.
Model: `claude-sonnet-4-6` via env-configured API key. Log every call (prompt hash, latency, tokens) to `audit_log`.

**5. Tools (`tools/`):** `PaymentsAdapter` interface with two implementations — `RazorpayTestAdapter` (real test-mode HTTP for payments/refunds fetch; contest/accept endpoints only if available, else raise `NotSupported`) and `SimulatorAdapter` (full dispute lifecycle: contest → resolves per ground truth after a tick; accept → closed). Config flag selects; every response records which adapter served it. `shipping_mock.py` and `mail.py` read from the generated dataset. Contest submission requires `idempotency_key = dispute_id`; a repeat call returns the original response.

**6. Orchestrator:** state machine INTAKE→LINK→GATHER→EXTRACT→GATE→DECIDE→DRAFT→ACT→CLOSED/ESCALATED with the rules of §8. Deterministic exact-match linking before calling `link_order`; link confidence < 0.85 → ESCALATE. `LowConfidence` anywhere → ESCALATE with a human-readable summary + generated merchant email (missing items + hours left). Deadline daemon: escalate any open case at T−24h; hard-block ACT after `respond_by`. API-failure path: 3 retries exp backoff, then escalate at T−12h.

**7. API + dashboard:** REST endpoints for webhook intake (`POST /webhooks/dispute`), case list/detail, approve-escalation (`POST /cases/{id}/approve` executes the prepared action as actor=human), metrics. Dashboard pages: Case Queue (deadline clocks), Case File (docs, evidence with green PASS / red FAIL + reasons, decision-math panel, draft with clickable [E#] → source quote), Audit Timeline, Metrics.

**8. Evals (`evals/run_eval.py`):** run the pipeline over the 40 held-out disputes with a simulated clock; output `metrics.json` and `report.md` containing: decision accuracy, extraction precision/recall, automation & escalation rates, escalation precision, deadline compliance, simulated ₹ recovered vs `never_contest` and `contest_all` baselines, false-fight fee cost, and an `--ablate-gate` mode that skips `gate.py` and counts inadmissible citations that would have shipped. Include the "not confidently handled" table (case id, reason).

**9. Tests:** pytest for all policy functions (happy + adversarial: fabricated quote, mismatched AWB, partial-refund arithmetic, expired deadline, duplicate webhook), idempotency, hash-chain integrity, and one end-to-end happy path + one escalation path using the simulator.

**10. Demo seed:** `scripts/demo_seed.py` loads three curated cases: (a) clean winnable goods_not_received with a Hinglish admission email, (b) pincode-mismatch escalation, (c) low-value hopeless case → auto-accept under cap. `README` documents run steps: `generate → seed → uvicorn → npm dev → run_eval`.

**Non-negotiable invariants:** AI functions never perform I/O to the payments adapter; only the orchestrator acts, and only after `decide.py`; no submission after deadline; no duplicate submissions; every state transition audited; drafts with citation violations never reach ACT.
