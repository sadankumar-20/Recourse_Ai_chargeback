# Panel story

**1. Problem** — SAY: merchants lose disputes to fragmented evidence and
deadlines, not to missing words. SHOW: a dispute's raw pieces (email,
order, shipment, missing POD). WHY: grounds the project in money, not demos.

**2. Why existing automation is insufficient** — SAY: fixed pipelines stop
at the first gap; templates can't fetch a courier record. SHOW: the fixed
run escalating disp_0019 "missing required pod". WHY: motivates agency.

**3. Why agentic AI** — SAY: an investigator that notices what's missing
and chooses the next read-only step. SHOW: the ledger's PLAN→TOOL→OBSERVE
loop. WHY: agency where the mess is, and only there.

**4. Architecture** — SAY: AI investigates, policy decides, execution acts,
audit proves. SHOW: docs/ARCHITECTURE.md lanes. WHY: the separation IS the
product.

**5. Safety model** — SAY: read-only budget-metered tools; a gate that
verifies verbatim against systems of record; one idempotent money writer;
server-authoritative deadlines; hash-chained audit. SHOW: the lying-vision
test and the 4/4 injection result. WHY: money.

**6. Live workflow** — run docs/DEMO_SCRIPT.md.

**7. Evaluation** — SAY: frozen held-out 40, real orchestrator, run twice
byte-identically: 7 recoveries, 0 regressions, +21
exhibits, 0 unsafe — and a −₹3,500 label-priced net. SHOW:
evals/v2_report.md. WHY: we measured instead of marketing; the negative
number is the credibility.

**8. Limitations** — synthetic world, v1 labels, credentialed real modes
unexercised in dev, polling not streaming. SHOW: README limitations. WHY:
engineering maturity.

**9. Next** — v2 outcome labels priced by evidence-at-decision-time;
Razorpay dispute-API integration behind the same executor; SSE ledger;
retrieval at corpus scale; human-review queue with SLAs.
