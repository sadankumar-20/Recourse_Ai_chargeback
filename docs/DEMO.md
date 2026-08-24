# Recourse — 5-minute demo script

Every moment below is the real pipeline. Nothing is mocked-as-real: simulated
calls are labeled `[SIMULATED]` on screen, and `/health` states the pinned
demo clock.

## Setup (before the demo, ~30s)

```bash
python3 data/generate.py --seed 42
python3 scripts/demo_seed.py
python3 scripts/serve.py            # http://127.0.0.1:8000
```

## 0:00 — The problem (talk over the Overview screen)

> "A customer files a chargeback. The merchant has 4–7 days to assemble
> courier proof, order records, and email evidence — or they lose by
> default. Most small Indian merchants lose by default."

Point at the stat strip: disputes in play, ₹ pending human action, and the
amber **under-24h** counter.

## 0:45 — A case runs live

Terminal, split screen:

```bash
curl -s -X POST http://127.0.0.1:8000/webhooks/dispute \
  -H 'Content-Type: application/json' \
  -d '{"event":"dispute.created","dispute_id":"disp_0004"}' | python3 -m json.tool
```

Refresh the queue: the new case appears already resolved — the agent linked
the order, gathered documents, extracted, gated, decided, drafted, and
submitted in under a second.

## 1:15 — The Hinglish case (open the closed `hinglish_admission` docket)

- Scroll the **evidence exhibits**: the customer's own message —
  *"bhaiya parcel mil gaya tha…"* — extracted verbatim, untranslated, with a
  green PASS stamp and its check ledger (source integrity ✓, quote verbatim
  ✓, sent-after-ship ✓).
- > "The model reads messy Hinglish; the deterministic gate verifies every
  > character of the quote against the mailbox before it counts."

## 2:00 — The Admissibility Gate earning its name (open `conflicting_pincode`)

- The red exhibit: **pincode mismatch: POD shows delivery to 5600XX, order
  address is 5600YY** — the AI extracted *faithfully*, the gate rejected
  *precisely*, and the other exhibits (AWB, POD) still passed.
- > "We don't blanket-fail cases. We fail claims, with reasons."
- Scroll to **HUMAN REVIEW REQUIRED**: the merchant summary names the
  missing item and the hours left. *No payment action was executed.*

## 2:45 — Decision math (back on the Hinglish docket, right column)

Point through the panel: potential recovery ₹, p_win from the versioned
playbook band, completeness, **EV(fight) vs EV(accept)**, the exact rule
that fired, playbook + thresholds versions.
> "The model never made this call. This arithmetic did — and it's stamped
> with the policy version that produced it."

## 3:15 — The citation-locked draft

Click **[E3]** in the representment → its exhibit flashes.
> "Every factual sentence must cite an admitted exhibit. A deterministic
> validator rejects anything else before submission — the model cannot lie
> in a filing, because the clerk checks every citation."

Then the **audit timeline** below: sixteen steps, and the badge —
**✓ CHAIN VERIFIED**. Mention: modifying, deleting, or reordering any entry
breaks the SHA-256 chain, and tests prove all five tamper modes are caught.

## 3:45 — A human takes over (still on the pincode case)

Click **Approve fight**, type your name.
- The timeline gains `HUMAN_APPROVED` → `ACTION_SUBMITTED (actor: human)
  [SIMULATED]`, and the case closes.
- Click approve again → *"Already executed — the original action was
  returned (idempotent)."*
> "Humans go through the same executor, the same idempotency key, the same
> audit chain as the agent. One money action per dispute. Ever."

## 4:20 — The numbers (Evaluation tab)

- 40 frozen held-out disputes, never tuned on. **Zero wrong fights, zero
  wrong accepts, zero deadline violations, 40/40 chains valid.**
- The strategy table — and the honest part: contest-everything nets more on
  this synthetic set. Point at **"Where Recourse currently stops"**:
  > "Every rupee of that gap is a reason code we refuse to fight without a
  > deterministic playbook — ₹45,969 of it winnable. That's not a weakness
  > slide; that's the v2 roadmap with a price tag on it."
- Gate ablation line: with the gate off, the system would have contested on
  a POD delivered to the wrong pincode.

## 4:50 — Close

> "Small merchants lose disputes by default. Recourse fights the ones worth
> fighting, concedes the hopeless ones for less than the fee, hands humans
> exactly what's missing when it stops — and can prove, line by
> cryptographic line, why it did each one."

## Failure-drill appendix (if asked "what if the API is down?")

```bash
cd backend && python3 -m unittest tests.test_orchestrator.TestFailurePaths -v
```
Live: 3 retries with exponential backoff under one idempotency key, zero
duplicate submissions, escalation carrying the prepared bundle — plus the
expired-deadline, duplicate-webhook, and low-confidence drills.
