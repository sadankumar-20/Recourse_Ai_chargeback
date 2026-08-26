# Five-minute demo script (verified against the live app)

Prep: `python3 data/generate.py --seed 42 && python3 scripts/demo_seed.py && python3 scripts/serve.py` → localhost:8000. All-simulator mode; /health shows it honestly.

**0:00–0:30 — Problem.** "Chargeback teams don't lose money because they
can't generate text. They lose it because evidence is fragmented and
deadlines are hard. Recourse is an AI that investigates — and a
deterministic system that decides."

**0:30–1:00 — Intake.** New investigation → type: *"The customer says they
never received order #0019, but our courier says it was delivered."* →
Start. Point at the verbatim-source note: the AI's reading is labeled an
untrusted interpretation.

**1:00–2:00 — The Investigation Ledger.** The cockpit opens on the seeded
live case. Walk the stream: PLAN → TOOL get_shipments → OBSERVATION (POD
missing) → TOOL fetch_tracking → TOOL search_knowledge. "Every row is a
hash-chained audit event — the UI cannot invent work."

**2:00–2:45 — NEEDS INPUT.** The ask names the exact AWB
(e.g. ECX5646751885). Drop `pod.txt` on the zone → Resume. "The agent
paused, asked, received, resumed — same case, same audit history."

**2:45–3:30 — Verification.** Exhibits: provenance badge USER UPLOAD,
per-check ticks, and (in the wrong-pincode variant) the explicit rejection.
Click [KB1]: the verbatim-verified policy quote. "The model can suggest
evidence. The gate decides whether it counts."

**3:30–4:00 — Decision.** Decision-math panel: p(win), completeness,
EV(fight) vs EV(accept), rule `fight_ev_positive`, versions. "AI
investigated. Policy decided."

**4:00–4:30 — Execution + audit.** ACTION SUBMITTED [SIMULATED], the
idempotency key (= dispute id: one money action ever), CHAIN VERIFIED.

**4:30–5:00 — Evaluation.** Evaluation tab, then the numbers: 7
fixed escalations resolved, 0 unsafe actions, 4/4
injections blocked, 100% deadline compliance, 80/80 chains valid — and the
label-priced net of −₹3,500, explained as a dataset-label
limitation we found instead of tuning away. Close: "That's the difference
between an AI demo and an AI system."
