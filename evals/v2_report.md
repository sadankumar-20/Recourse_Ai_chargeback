# Eval v2 — the final agentic evaluation (R7)

Frozen held-out 40, replayed through the REAL orchestrator with deterministic providers; run twice, byte-identical (True).

## Fixed vs agentic
| metric | fixed | agentic |
|---|---|---|
| automation | 42% | 60% |
| escalations | 23 | 16 |
| net (v1 labels) | Rs.58,489 | Rs.54,989 |
| pending (escalated) | Rs.153,559 | Rs.109,152 |
| deadline violations | 0 | 0 |
| invalid tool calls | 0 | 0 |
| budget violations | 0 | 0 |
| chains invalid | 0 | 0 |

**Headline**: the agent resolved 7 cases the fixed pipeline escalated, admitting 21 additional exhibits, at 3.27 avg tool calls (max 6, budget 12). v1 gt_outcome labels price missing_pod fights at 10% win — authored under the fixed pipeline's capability assumption (ADR-014). Capability and label-priced money are both reported; neither is hidden.

## Recoverable gaps (courier blinded, merchant answers the ask)
7/7 resolved after upload — gate-admitted evidence only (rate 1.0).

## Ablations
- Tracking: missing-POD recoveries 7 -> 7 when the tool exists.
- RAG: 25 queries; decision changes caused: 0 (must be and is zero).
- Vision: lying transcription rejected = True; fabrications accepted = 0.

## Prompt injection
4 vectors (customer email, POD-style document, poisoned knowledge, intake narrative) — blocked: 4, unsafe actions: 0.

## Where the agent fails (escalated, honestly priced)
| case | scenario | amount | why |
|---|---|---|---|
| disp_0002 | partial_refund | Rs.4,532 | Dispute #disp_0002
Amount: ₹4,532
Hours remaining: 113

Recommended action: HUMAN REVIEW

 |
| disp_0004 | high_value | Rs.18,979 | Dispute #disp_0004
Amount: ₹18,979
Hours remaining: 77

Recommended action: HUMAN REVIEW

 |
| disp_0008 | partial_refund | Rs.2,457 | Dispute #disp_0008
Amount: ₹2,457
Hours remaining: 109

Recommended action: HUMAN REVIEW

 |
| disp_0009 | delayed_deadline | Rs.9,400 | Dispute #disp_0009
Amount: ₹9,400
Hours remaining: 11

Recommended action: HUMAN REVIEW

R |
| disp_0013 | partial_refund | Rs.5,753 | Dispute #disp_0013
Amount: ₹5,753
Hours remaining: 128

Recommended action: HUMAN REVIEW

 |
| disp_0028 | conflicting_pincode | Rs.3,064 | Dispute #disp_0028
Amount: ₹3,064
Hours remaining: 127

Recommended action: HUMAN REVIEW

 |
| disp_0047 | clean_winnable | Rs.4,024 | Dispute #disp_0047
Amount: ₹4,024
Hours remaining: 81

Recommended action: HUMAN REVIEW

R |
| disp_0049 | delayed_deadline | Rs.5,629 | Dispute #disp_0049
Amount: ₹5,629
Hours remaining: 9

Recommended action: HUMAN REVIEW

Re |
| disp_0052 | clean_winnable | Rs.1,432 | Dispute #disp_0052
Amount: ₹1,432
Hours remaining: 121

Recommended action: HUMAN REVIEW

 |
| disp_0059 | clean_winnable | Rs.6,897 | Dispute #disp_0059
Amount: ₹6,897
Hours remaining: 90

Recommended action: HUMAN REVIEW

R |
| disp_0074 | clean_winnable | Rs.8,293 | Dispute #disp_0074
Amount: ₹8,293
Hours remaining: 160

Recommended action: HUMAN REVIEW

 |
| disp_0084 | clean_winnable | Rs.8,192 | Dispute #disp_0084
Amount: ₹8,192
Hours remaining: 66

Recommended action: HUMAN REVIEW

R |

## Where the fixed pipeline wins
Clean cases with complete merchant records: identical decisions at zero tool calls — the conveyor belt is cheaper when nothing is missing, which is exactly why the flag defaults to fixed for batch replay and agentic for interactive intake.

## Where agentic AI wins
Every fixed-escalation the agent resolved followed the same shape: notice the gap -> query the courier's own record -> materialize it as a document -> UNCHANGED gate admits -> deterministic decision. See recovered_case_details in v2_metrics.json.

## Conclusion
Recourse is not an LLM that decides whether to fight a chargeback. It is a bounded AI investigator; every exhibit passes a deterministic gate, every decision a deterministic engine, every money action is idempotent and hash-chain audited. Eval v2 measures — honestly, twice, byte-identically — what that architecture recovers.
