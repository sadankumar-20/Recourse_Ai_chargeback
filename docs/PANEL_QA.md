# Panel Q&A (every answer from the actual implementation)

**Why an agent?** Fixed gathering stops at the first gap; the agent noticed
missing PODs and queried the courier's own record — 7 held-out
escalations resolved (0 regressions).

**Why not just an LLM / let it decide?** An LLM's output is plausible, not
verified. Money requires verification, so the LLM proposes and
deterministic layers dispose: gate → decision engine → executor. The AI
lane cannot even import the tools or repo (AST-enforced).

**Why is the gate deterministic?** So admissibility is reproducible and
arguable: verbatim quote in a linked source, AWB vs shipments, pincode vs
order, amounts vs records. Same bar for uploads, tracking, vision, KB.

**Hallucinated evidence?** It fails verbatim/system-of-record checks. The
lying-vision test: wrong pincode → linked, INADMISSIBLE, zero money.

**Prompt injection?** Treated as data. Four vectors (customer email,
POD-style doc, poisoned KB, intake narrative) leave state/action/money
bit-identical to clean baselines: 4/4 blocked, 0 unsafe.

**API down / tracking lies?** Loud structured errors, never silent
simulator fallback; tracking output is candidate evidence for the same
gate; agent escalates or asks rather than inventing.

**Malicious upload?** Content-hashed, provenance user_upload, untrusted
until gated; wrong-pincode upload test proves rejection.

**Missing evidence?** NEEDS_INPUT: a structured ask naming the exact AWB;
upload; resume the same case (7/7 blinded cases resolved).

**Deadlines / duplicates / audit?** Server-authoritative snapshots, expired
mutations 409; idempotency_key = dispute id (replay test: one action);
per-case SHA-256 hash chain with 5 tamper modes detected.

**If the system is wrong?** It escalates with the deterministic reason and
hours left; humans act through the same executor with the same audit.

**Why is the revenue result negative?** v1 outcome labels price missing-POD
fights at 10% win — authored for the fixed pipeline's capabilities. We
report capability and label-priced money side by side; fixing labels is a
dataset version, not tuning.

**Where does fixed win?** Clean, complete cases: identical decisions at
zero tool cost — which is why the default is fixed for batch, agentic for
interactive.

**Why should Razorpay care?** At payment scale the expensive problem is
safely automating messy, rule-bound, money-touching workflows. This is an
architecture for exactly that (a student demonstration, not a product).

**Next?** v2 labels, real dispute-API behind the same executor, SSE
ledger, corpus-scale retrieval, review queues.
