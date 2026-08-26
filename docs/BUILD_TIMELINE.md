# Build timeline

| Stage | Problem introduced | Capability | Safety property | Measured |
|---|---|---|---|---|
| 1 Foundation | repo discipline | skeleton, tests | lanes named | suite green |
| 2 Domain+SQLite | persistence | models, repo | schema versioning | migrations guarded |
| 3 Synthetic world | realistic mess | seed-42 world, 11 scenarios | frozen dev/held-out split | 800 orders/120 disputes |
| 4 Admissibility Gate | untrusted claims | verbatim + system-of-record checks | dual verification | ablation: gate removal caught |
| 5 Decision engine | money judgment | EV ladder, playbooks v1 | caps, versioned thresholds | rules test-pinned |
| 6 AI layer | interpretation | link/extract/draft via schema→repair | LowConfidence, lane AST bans | stub determinism |
| 7 Payments+audit | execution risk | executor, hash chain | idempotency=dispute; tamper detection | 5 tamper modes |
| 8 Orchestrator | coordination | end-to-end pipeline | deadline kill-switch | journeys green |
| 9 Evaluation | proof | frozen held-out harness | no tuning on held-out | 75% agreement, 0 wrong fights |
| 10 API+dashboard | operability | Flask + docket UI | server authority | HTTP suite |
| R1 Tools+provenance | agent hands | read-only registry, budgets | writes raise; TOOL_CALL audited | 225 tests |
| R2 Agentic loop | investigation | planner-as-data + runner | bounded, no-progress; gate unbent | 11 dev recoveries, eval byte-identical |
| R3 RAG | policy context | BM25 KB, verified citations | verbatim verification; poisoned-KB bit-identity | 116 verified citations |
| R4 Interactive | merchant loop | intake, uploads, NEEDS_INPUT, resume, deadlines | verbatim-first; 4th gate channel scopes only | 11/11 blinded resolved |
| R5 Tracking+vision | external reality | AfterShip adapter, vision transcription | loud failure; lying vision inadmissible | offline transport tests |
| R6 Cockpit | visibility | audit-chain ledger, live countdown | UI renders server truth only | 9 contract tests |
| R7 Eval v2 | final proof | A/B, ablations, injections, reproducibility | held-out never tuned | 7 recovered, 0 unsafe, −₹3,500 honest |
| R8 Story | communication | docs, demo, Q&A | no overclaiming | this file |
