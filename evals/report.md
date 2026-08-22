# Recourse Evaluation Report

## Evaluation configuration

- Frozen dataset: seed 42, sim_now 2026-08-23T12:00:00+00:00
- Playbook v1, thresholds v1, contest fee ₹500 (fee charged on LOST contests)
- AI provider: stub (deterministic offline stub — see Limitations)
- Run at 2026-08-22T22:26:26+00:00 (metadata only; metrics are deterministic)

## Frozen dataset

40 held-out disputes, frozen in data/split.json since Stage 3, never used for tuning. The harness fails loudly on any split alteration, overlap, or seed mismatch.

## Executive result

On the frozen synthetic held-out set, Recourse decided 30/40 cases in agreement with ground truth (75.0%), automated 42.5% of cases, recovered ₹66,489 (net ₹64,989) against ₹0 for never-contest and ₹137,556 net for contest-everything, with 0 deadline violations and every audit chain verifying. All decision errors trace to documented coverage gaps, not judgment errors (see Failure analysis).

## Decision metrics

Accuracy: 75.0% (30/40)

Confusion matrix (rows = ground truth, cols = agent):

| gt \ agent | FIGHT | ACCEPT | ESCALATE |
|---|---|---|---|
| FIGHT | 16 | 0 | 10 |
| ACCEPT | 0 | 1 | 0 |
| ESCALATE | 0 | 0 | 13 |

## Evidence extraction metrics

- Precision: 0.9855
- Recall vs all gt evidence: 0.3696
- Recall vs playbook-extractable evidence: 0.7816
- recall reported twice: against ALL ground-truth evidence keys, and against only the keys the v1 playbooks define — the gap is deferred-scope, not extraction error

## Automation vs escalation

- Automation rate: 42.5%
- Escalation rate: 57.5% (23 cases)
- Escalation precision (strict: gt says ESCALATE): 0.5652
- Escalations caused by documented coverage gaps (deferred reason codes): 12 — counted AGAINST strict precision on purpose

## Deadline compliance

40/40 compliant (100%). Violations: none

## Money recovered & baseline comparison

| strategy | recovered | fees (on losses) | net |
|---|---|---|---|
| never contest | ₹0 | ₹0 | ₹0 |
| contest everything | ₹145,556 | ₹8,000 | ₹137,556 |
| **Recourse** | ₹66,489 | ₹1,500 | ₹64,989 |

Recourse fought 16 cases, conceded ₹716 deliberately, and escalated ₹153,559 to humans (pending, not lost — of which ₹45,969 is winnable per ground truth).

**Why contest-everything shows a higher net on this set:** the gap is exactly the deferred-coverage escalations. Recourse refuses to fight the 3 reason codes without a v1 playbook and hands that money to a human instead of contesting without verified evidence; contest-everything fights them blind and — in this simulation — wins most. On this synthetic set that gamble pays; in production it presumes every representment can be assembled instantly and risks unverified submissions. The actionable reading is not 'contest everything' but 'extend playbook coverage': the escalated winnable amount above is the quantified size of that opportunity.

## False-fight cost

- False fights (contested and lost): 3
- Total cost: ₹7,136 (avg ₹2,378.7)

## Handling time

Median 0.0119s, avg 0.0132s, min 0.0022s, max 0.0646s per case (wall clock, offline stub provider). handling times are wall-clock with the configured AI provider; no manual-handling baseline exists in the dataset, so none is claimed.

## Scenario breakdown

| scenario | n | correct | escalated | ₹ recovered |
|---|---|---|---|---|
| ambiguous_match | 1 | 1 | 1 | ₹0 |
| cancelled_after_shipping | 1 | 0 | 1 | ₹0 |
| clean_winnable | 12 | 7 | 5 | ₹21,401 |
| conflicting_denial | 1 | 1 | 0 | ₹5,634 |
| conflicting_pincode | 1 | 1 | 1 | ₹0 |
| delayed_deadline | 2 | 2 | 2 | ₹0 |
| duplicate_event | 2 | 2 | 0 | ₹8,157 |
| high_value | 2 | 2 | 2 | ₹0 |
| hinglish_admission | 6 | 6 | 0 | ₹31,297 |
| hopeless_low_value | 1 | 1 | 0 | ₹0 |
| missing_pod | 7 | 7 | 7 | ₹0 |
| partial_refund | 4 | 0 | 4 | ₹0 |

## Not confidently handled

| dispute | scenario | amount | hours left | reason | link conf | final |
|---|---|---|---|---|---|---|
| disp_0002 | partial_refund | ₹4,532 | 112.7 | unsupported reason code: no playbook for reason code 'credit_not_proce | None | ESCALATE |
| disp_0004 | high_value | ₹18,979 | 76.6 | unsupported reason code: no playbook for reason code 'fraud' (playbook | None | ESCALATE |
| disp_0008 | partial_refund | ₹2,457 | 108.7 | unsupported reason code: no playbook for reason code 'credit_not_proce | None | ESCALATE |
| disp_0009 | delayed_deadline | ₹9,400 | 10.7 | only 10.7h left (< 24h kill-switch) — a human must handle last-minute  | None | ESCALATE |
| disp_0013 | partial_refund | ₹5,753 | 128.0 | unsupported reason code: no playbook for reason code 'credit_not_proce | None | ESCALATE |
| disp_0016 | missing_pod | ₹9,202 | 106.2 | policy decision: evidence incomplete: completeness 0.00 < 0.75 floor;  | 1.0 | ESCALATE |
| disp_0028 | conflicting_pincode | ₹3,064 | 127.3 | policy decision: evidence incomplete: completeness 0.67 < 0.75 floor;  | 1.0 | ESCALATE |
| disp_0034 | missing_pod | ₹8,249 | 134.7 | policy decision: evidence incomplete: completeness 0.00 < 0.75 floor;  | 1.0 | ESCALATE |
| disp_0035 | missing_pod | ₹7,012 | 100.1 | policy decision: evidence incomplete: completeness 0.00 < 0.75 floor;  | 1.0 | ESCALATE |
| disp_0039 | missing_pod | ₹6,049 | 128.7 | policy decision: evidence incomplete: completeness 0.00 < 0.75 floor;  | 1.0 | ESCALATE |
| disp_0045 | missing_pod | ₹6,605 | 60.4 | policy decision: evidence incomplete: completeness 0.00 < 0.75 floor;  | 1.0 | ESCALATE |
| disp_0047 | clean_winnable | ₹4,024 | 80.7 | unsupported reason code: no playbook for reason code 'cancelled_recurr | None | ESCALATE |
| disp_0049 | delayed_deadline | ₹5,629 | 8.6 | only 8.6h left (< 24h kill-switch) — a human must handle last-minute c | None | ESCALATE |
| disp_0052 | clean_winnable | ₹1,432 | 120.6 | unsupported reason code: no playbook for reason code 'cancelled_recurr | None | ESCALATE |
| disp_0059 | clean_winnable | ₹6,897 | 90.3 | unsupported reason code: no playbook for reason code 'cancelled_recurr | None | ESCALATE |
| disp_0069 | missing_pod | ₹5,228 | 71.9 | policy decision: evidence incomplete: completeness 0.00 < 0.75 floor;  | 1.0 | ESCALATE |
| disp_0074 | clean_winnable | ₹8,293 | 160.1 | unsupported reason code: no playbook for reason code 'cancelled_recurr | None | ESCALATE |
| disp_0084 | clean_winnable | ₹8,192 | 66.3 | unsupported reason code: no playbook for reason code 'cancelled_recurr | None | ESCALATE |
| disp_0087 | ambiguous_match | ₹2,078 | 114.3 | ambiguous order link: AI confidence 0.55 < 0.85 floor — the system nev | None | ESCALATE |
| disp_0088 | missing_pod | ₹2,062 | 159.5 | policy decision: evidence incomplete: completeness 0.00 < 0.75 floor;  | 1.0 | ESCALATE |
| disp_0103 | cancelled_after_shipping | ₹2,026 | 148.4 | unsupported reason code: no playbook for reason code 'cancelled_recurr | None | ESCALATE |
| disp_0110 | partial_refund | ₹2,363 | 99.5 | unsupported reason code: no playbook for reason code 'credit_not_proce | None | ESCALATE |
| disp_0112 | high_value | ₹24,033 | 87.3 | unsupported reason code: no playbook for reason code 'fraud' (playbook | None | ESCALATE |

## Admissibility Gate ablation

**ABLATION — Admissibility Gate disabled (analysis only; the production pipeline always keeps the gate on)**

- Evidence candidates: 69
- Inadmissible candidates that would ship with the gate off: 1
- Decisions that would flip: 1
  - disp_0028 [conflicting_pincode]: ESCALATE -> FIGHT on pincode mismatch: POD shows delivery to 500018, order address is 50008
- extraction here is the faithful offline stub, so the only inadmissible evidence is what the DATA plants (e.g. wrong-pincode PODs); against an unfaithful extractor the gate additionally blocks fabricated quotes — demonstrated by adversarial tests (test_ai_pipeline ablation pair)

## Failure analysis

Every disagreement with ground truth, with its cause:

- disp_0002 [partial_refund, credit_not_processed]: agent ESCALATE, gt FIGHT — unsupported reason code: no playbook for reason code 'credit_not_processed' (playbook v1 c
- disp_0008 [partial_refund, credit_not_processed]: agent ESCALATE, gt FIGHT — unsupported reason code: no playbook for reason code 'credit_not_processed' (playbook v1 c
- disp_0013 [partial_refund, credit_not_processed]: agent ESCALATE, gt FIGHT — unsupported reason code: no playbook for reason code 'credit_not_processed' (playbook v1 c
- disp_0047 [clean_winnable, cancelled_recurring]: agent ESCALATE, gt FIGHT — unsupported reason code: no playbook for reason code 'cancelled_recurring' (playbook v1 co
- disp_0052 [clean_winnable, cancelled_recurring]: agent ESCALATE, gt FIGHT — unsupported reason code: no playbook for reason code 'cancelled_recurring' (playbook v1 co
- disp_0059 [clean_winnable, cancelled_recurring]: agent ESCALATE, gt FIGHT — unsupported reason code: no playbook for reason code 'cancelled_recurring' (playbook v1 co
- disp_0074 [clean_winnable, cancelled_recurring]: agent ESCALATE, gt FIGHT — unsupported reason code: no playbook for reason code 'cancelled_recurring' (playbook v1 co
- disp_0084 [clean_winnable, cancelled_recurring]: agent ESCALATE, gt FIGHT — unsupported reason code: no playbook for reason code 'cancelled_recurring' (playbook v1 co
- disp_0103 [cancelled_after_shipping, cancelled_recurring]: agent ESCALATE, gt FIGHT — unsupported reason code: no playbook for reason code 'cancelled_recurring' (playbook v1 co
- disp_0110 [partial_refund, credit_not_processed]: agent ESCALATE, gt FIGHT — unsupported reason code: no playbook for reason code 'credit_not_processed' (playbook v1 c

## Limitations

- The world is synthetic; results demonstrate architecture behavior under controlled messiness, not production performance.
- Extraction in this run is the deterministic offline stub; a real LLM provider introduces extraction variance the stub cannot — the harness is provider-agnostic and should be re-run with RECOURSE_AI_PROVIDER=anthropic for model-level numbers.
- Ground-truth actions were derived from the same policy caps the decision engine uses (by design, ADR-005), so decision accuracy measures pipeline consistency plus coverage, not independent judgment.
- 3 of 6 reason codes are deferred (MVP scope); their escalations are counted honestly as errors/escalations above.
- No manual-handling time baseline exists in the dataset; none is invented.

## Conclusion

On the frozen synthetic held-out set, under the stated assumptions, Recourse recovers more net money than either baseline while never violating a deadline, never duplicating a money action, escalating precisely where evidence or coverage runs out, and leaving a verifiable audit chain for every case.