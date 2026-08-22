"""Synthetic dataset generator for Recourse (spec §13).

Design
------
Scenario-driven, not corruption-after-the-fact: each dispute is generated from
one of 11 named scenarios with fixed quotas matching the imperfection rates in
spec §13. That makes every injected imperfection *guaranteed present*, makes
ground truth *derivable* from the scenario's facts, and makes the nine demo
cases reproducible from the seed alone.

Outputs (written to --out-dir, default ``data/``):
- ``dataset.db``        the application-facing world (orders, shipments,
                        refunds, documents, disputes) via the Stage-2 store
- ``events.jsonl``      the dispute-webhook feed the orchestrator will consume;
                        duplicate-delivery disputes appear here twice
- ``ground_truth.json`` hidden evaluation labels, deliberately OUTSIDE the app
                        database so no application API can ever expose them
- ``split.json``        frozen 80-dev / 40-held-out split (stratified per
                        scenario so both sets cover every failure mode)

Determinism: one ``random.Random(seed)`` instance threaded everywhere; the
simulated "now" is a constant. Same seed => byte-identical world.

Money is integer INR; ground-truth actions are computed with the SAME config
caps the policy engine will use, so later evaluation is principled.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from random import Random

from .config import (
    AUTO_ACCEPT_CAP_INR,
    DEADLINE_ESCALATE_HOURS,
    ESCALATION_AMOUNT_CAP_INR,
)
from .store.models import (
    Dispute,
    Document,
    DocumentType,
    Merchant,
    Order,
    ReasonCode,
    Refund,
    Shipment,
)
from .store.repo import Repository

# --- constants ----------------------------------------------------------------

SIM_NOW = datetime(2026, 8, 23, 12, 0, 0, tzinfo=timezone.utc)

# Scenario names (stable identifiers — used in ground truth and tests).
CLEAN = "clean_winnable"
HINGLISH = "hinglish_admission"
MISSING_POD = "missing_pod"
CONFLICT_PIN = "conflicting_pincode"      # POD delivered to a different pincode
CONFLICT_DENY = "conflicting_denial"      # courier says delivered, email denies
DUP_EVENT = "duplicate_event"             # webhook delivered twice
PARTIAL_REFUND = "partial_refund"
DELAYED = "delayed_deadline"
AMBIGUOUS = "ambiguous_match"             # unknown payment_id + twin orders
HOPELESS = "hopeless_low_value"
HIGH_VALUE = "high_value"
CANCEL_AFTER_SHIP = "cancelled_after_shipping"

# Quotas sum to 120 and mirror spec §13 rates over 120 disputes.
SCENARIO_QUOTAS: dict[str, int] = {
    CLEAN: 30,
    HINGLISH: 20,
    MISSING_POD: 18,        # ~15% missing POD
    CONFLICT_PIN: 5,        # together ~8% conflicting records
    CONFLICT_DENY: 5,
    DUP_EVENT: 6,           # 5% duplicate webhook delivery
    PARTIAL_REFUND: 12,     # 10% partial refunds
    DELAYED: 8,             # ~7% delayed webhooks (<36h left)
    AMBIGUOUS: 4,
    HOPELESS: 5,
    HIGH_VALUE: 4,
    CANCEL_AFTER_SHIP: 3,
}

FIRST = ["Asha", "Rohan", "Priya", "Kabir", "Meera", "Arjun", "Divya", "Sameer",
         "Nikhil", "Sneha", "Vikram", "Ananya", "Farhan", "Isha", "Rahul",
         "Pooja", "Aditya", "Lakshmi", "Manav", "Ritu"]
LAST = ["Rao", "Sharma", "Iyer", "Khan", "Patel", "Gupta", "Nair", "Das",
        "Reddy", "Mehta", "Chawla", "Bose", "Kulkarni", "Joshi", "Verma"]
CITIES = [
    ("Bengaluru", ["560001", "560038", "560095", "560102"]),
    ("Mumbai", ["400001", "400050", "400076"]),
    ("Delhi", ["110001", "110019", "110085"]),
    ("Chennai", ["600001", "600040", "600119"]),
    ("Pune", ["411001", "411045"]),
    ("Hyderabad", ["500001", "500081"]),
]
STREETS = ["MG Road", "Link Road", "Anna Salai", "FC Road", "Brigade Road",
           "Jubilee Hills Rd", "Carter Road", "Ring Road"]
COURIERS = ["Delhivery", "BlueDart", "Ekart", "EcomExpress", "XpressBees"]

# Hinglish admission lines: the customer concedes delivery happened. The gate
# will later need the exact substring, so these are the canonical spans.
# HINGLISH_MARKERS is the single source of truth: every admission template MUST
# contain exactly one marker, and validation asserts marker-doc count == quota.
HINGLISH_MARKERS = ["mil gaya", "receive ho gaya", "delivery ho gayi", "aa gaya"]
HINGLISH_ADMISSIONS = [
    "bhaiya parcel mil gaya tha {date} ko, but size chhota hai. refund kar do please",
    "package receive ho gaya {date} ko lekin quality bilkul acchi nahi hai, paise wapas chahiye",
    "haan delivery ho gayi thi {date} ko, par mujhe pasand nahi aaya. refund process karo warna complaint karunga",
    "parcel aa gaya {date} ko ghar pe, magar colour alag hai photo se. return karna hai",
]
DENIALS = [
    "I never received this package. Courier is lying, kuch bhi deliver nahi hua.",
    "koi parcel nahi aaya humare address pe. this charge is fraud, refund immediately.",
]
AMBIENT_LINES = [
    "hi, order ka status kya hai?",
    "please share tracking details",
    "thanks, will check with the watchman also",
    "any update? it's been a few days",
    "ok noted.",
]


class DatasetError(Exception):
    """Raised when generated (or tampered) data violates dataset invariants."""


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


# --- small builders -------------------------------------------------------------

@dataclass
class _World:
    """Accumulates generated entities plus generator-side bookkeeping."""
    repo: Repository
    rng: Random
    seq: dict[str, int]
    ground_truth: dict[str, dict]
    events: list[dict]
    scripted_order_ids: set[str]

    def next_id(self, prefix: str) -> str:
        self.seq[prefix] = self.seq.get(prefix, 0) + 1
        return f"{prefix}_{self.seq[prefix]:04d}"


def _customer(rng: Random) -> tuple[str, str]:
    name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
    email = f"{name.lower().replace(' ', '.')}{rng.randint(1, 99)}@example.com"
    return name, email


def _address(rng: Random) -> tuple[str, str]:
    city, pins = rng.choice(CITIES)
    pin = rng.choice(pins)
    addr = f"{rng.randint(1, 240)} {rng.choice(STREETS)}, {city} {pin}"
    return addr, pin


def _swap_two_digits(pin: str, rng: Random) -> str:
    i = rng.randint(0, len(pin) - 2)
    lst = list(pin)
    lst[i], lst[i + 1] = lst[i + 1], lst[i]
    out = "".join(lst)
    return out if out != pin else pin[::-1]


def _pod_text(awb: str, courier: str, delivered_at: datetime, receiver: str,
              address: str, otp_verified: bool) -> str:
    return (
        "PROOF OF DELIVERY\n"
        f"Courier: {courier}\n"
        f"AWB: {awb}\n"
        f"Delivered: {iso(delivered_at)}\n"
        f"Receiver: {receiver}\n"
        f"Delivery OTP verified: {'YES' if otp_verified else 'NO'}\n"
        f"Address: {address}\n"
    )


def _thread(rng: Random, customer_email: str, merchant_name: str,
            key_line: str | None, key_at: datetime) -> str:
    """An email thread with optional key line buried among ambient chatter."""
    n_before = rng.randint(1, 4)
    n_after = rng.randint(0, 2)
    lines = []
    t = key_at - timedelta(days=rng.randint(2, 6))
    for _ in range(n_before):
        lines.append((customer_email, t, rng.choice(AMBIENT_LINES)))
        t += timedelta(hours=rng.randint(4, 30))
        lines.append((f"support@{merchant_name.lower().replace(' ', '')}.in", t,
                      "Hi, thanks for reaching out — we're checking and will update you."))
        t += timedelta(hours=rng.randint(4, 30))
    if key_line:
        lines.append((customer_email, key_at, key_line))
        t = key_at
    for _ in range(n_after):
        t += timedelta(hours=rng.randint(4, 30))
        lines.append((f"support@{merchant_name.lower().replace(' ', '')}.in", t,
                      "We've noted this and escalated it to our team."))
    return "\n---\n".join(
        f"From: {frm}\nDate: {iso(when)}\n\n{body}" for frm, when, body in lines
    )


def _order_bundle(w: _World, merchant: Merchant, *, amount: int,
                  ship: bool = True, with_pod: bool = True,
                  pod_pin_typo: bool = False, otp: bool = True,
                  customer: tuple[str, str] | None = None,
                  created_at: datetime | None = None) -> dict:
    """Create order (+shipment +POD document) and return the pieces."""
    rng = w.rng
    name, email = customer or _customer(rng)
    address, pin = _address(rng)
    created = created_at or (SIM_NOW - timedelta(days=rng.randint(10, 70),
                                                 hours=rng.randint(0, 23)))
    order = Order(
        id=w.next_id("ord"), merchant_id=merchant.id,
        payment_id=f"pay_{w.next_id('p')[2:]}", amount=amount,
        customer_email=email, address=address,
        created_at=iso(created),
        promised_ship_by=iso(created + timedelta(days=3)),
    )
    w.repo.add_order(order)
    shipment = pod_doc = None
    delivered_at = None
    if ship:
        ship_date = created + timedelta(days=rng.randint(1, 3))
        delivered_at = ship_date + timedelta(days=rng.randint(1, 4),
                                             hours=rng.randint(1, 20))
        awb = f"{rng.choice(['DLV', 'BLD', 'EKT', 'ECX', 'XPB'])}{rng.randint(10**9, 10**10 - 1)}"
        courier = rng.choice(COURIERS)
        pod_doc_id = None
        if with_pod:
            pod_addr = address
            if pod_pin_typo:
                pod_addr = address.replace(pin, _swap_two_digits(pin, rng))
            doc = Document(
                id=w.next_id("doc"), case_id=None, type=DocumentType.POD,
                raw_text=_pod_text(awb, courier, delivered_at, name, pod_addr, otp),
                source=f"courier:{awb}", fetched_at=iso(delivered_at),
            )
            w.repo.add_document(doc)
            pod_doc = doc
            pod_doc_id = doc.id
        shipment = Shipment(
            id=w.next_id("shp"), order_id=order.id, awb=awb, courier=courier,
            ship_date=iso(ship_date), status="delivered" if with_pod or ship else "in_transit",
            pod_doc_id=pod_doc_id,
        )
        w.repo.add_shipment(shipment)
    return {"order": order, "name": name, "email": email, "pin": pin,
            "shipment": shipment, "pod_doc": pod_doc, "delivered_at": delivered_at,
            "created": created}


def _add_email(w: _World, email: str, merchant: Merchant, key_line: str | None,
               key_at: datetime) -> Document:
    doc = Document(
        id=w.next_id("doc"), case_id=None, type=DocumentType.EMAIL,
        raw_text=_thread(w.rng, email, merchant.name, key_line, key_at),
        source=f"mailbox:{email}", fetched_at=iso(SIM_NOW),
    )
    w.repo.add_document(doc)
    return doc


def _correct_action(amount: int, hours_left: float, evidence_complete: bool,
                    hopeless: bool) -> str:
    """Ground-truth action, computed with the SAME caps the policy engine uses."""
    if amount > ESCALATION_AMOUNT_CAP_INR:
        return "ESCALATE"
    if hours_left < DEADLINE_ESCALATE_HOURS:
        return "ESCALATE"
    if hopeless and amount <= AUTO_ACCEPT_CAP_INR:
        return "ACCEPT"
    if evidence_complete:
        return "FIGHT"
    return "ESCALATE"


def _outcome_if_fought(rng: Random, p_win: float) -> str:
    return "won" if rng.random() < p_win else "lost"


# --- main generation -------------------------------------------------------------

def generate(seed: int, out_dir: str | Path, n_orders: int = 800,
             n_disputes: int = 120) -> dict:
    """Generate the full synthetic world. Returns derived summary stats."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    db_path = out / "dataset.db"
    if db_path.exists():
        db_path.unlink()

    rng = Random(seed)
    repo = Repository(db_path)
    # Generation-only speed pragmas: thousands of tiny commits; synthetic data,
    # so durability during generation is irrelevant. The app never sets these.
    repo.conn.execute("PRAGMA synchronous = OFF")
    repo.conn.execute("PRAGMA journal_mode = MEMORY")

    w = _World(repo=repo, rng=rng, seq={}, ground_truth={}, events=[],
               scripted_order_ids=set())

    merchants = [
        Merchant("m_0001", "Kadai Crafts", AUTO_ACCEPT_CAP_INR, ESCALATION_AMOUNT_CAP_INR),
        Merchant("m_0002", "Vastra Studio", AUTO_ACCEPT_CAP_INR, ESCALATION_AMOUNT_CAP_INR),
        Merchant("m_0003", "Herbal Nest", AUTO_ACCEPT_CAP_INR, ESCALATION_AMOUNT_CAP_INR),
    ]
    for m in merchants:
        repo.add_merchant(m)

    # scenario roster: quotas -> shuffled list of scenario tags
    quotas = dict(SCENARIO_QUOTAS)
    quotas[CLEAN] = n_disputes - sum(v for k, v in quotas.items() if k != CLEAN)
    if quotas[CLEAN] < 0:
        raise ValueError("n_disputes smaller than fixed scenario quotas")
    roster: list[str] = [s for s, q in quotas.items() for _ in range(q)]
    rng.shuffle(roster)

    for scenario in roster:
        _make_dispute(w, rng.choice(merchants), scenario)

    # noise orders (never disputed) to reach n_orders
    n_noise = n_orders - w.seq.get("ord", 0)
    if n_noise < 0:
        raise ValueError("n_orders too small for scripted orders")
    for _ in range(n_noise):
        m = rng.choice(merchants)
        b = _order_bundle(w, m, amount=rng.randint(300, 9500),
                          ship=rng.random() < 0.95,
                          with_pod=rng.random() < 0.90)
        if rng.random() < 0.10:  # ambient support chatter, no dispute
            _add_email(w, b["email"], m, None, SIM_NOW - timedelta(days=rng.randint(1, 30)))

    # split: stratified per scenario so held-out covers every failure mode
    split = _stratified_split(w.ground_truth, rng, n_heldout=40)

    events = sorted(w.events, key=lambda e: e["arrival"])
    (out / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n")
    (out / "ground_truth.json").write_text(json.dumps({
        "_comment": "Hidden evaluation labels. Read ONLY by the eval harness. "
                    "Kept outside the app DB so no API can expose them.",
        "sim_now": iso(SIM_NOW), "seed": seed, "labels": w.ground_truth,
    }, indent=1))
    (out / "split.json").write_text(json.dumps({
        "_comment": "FROZEN split. The 40 held-out disputes are for final "
                    "evaluation only — never tune prompts or thresholds on them.",
        "seed": seed, "sim_now": iso(SIM_NOW),
        "dev": split["dev"], "held_out": split["held_out"],
    }, indent=1))

    stats = validate_dataset(out)
    repo.close()
    return stats


def _make_dispute(w: _World, merchant: Merchant, scenario: str) -> None:
    rng = w.rng
    amount = {
        HOPELESS: rng.randint(300, 1900),
        HIGH_VALUE: rng.randint(ESCALATION_AMOUNT_CAP_INR + 500, 25000),
    }.get(scenario, rng.randint(500, 9500))

    ship = scenario != HOPELESS            # hopeless: never shipped
    with_pod = scenario not in (MISSING_POD, HOPELESS)
    b = _order_bundle(w, merchant, amount=amount, ship=ship, with_pod=with_pod,
                      pod_pin_typo=(scenario == CONFLICT_PIN))
    order = b["order"]
    w.scripted_order_ids.add(order.id)
    delivered_at = b["delivered_at"] or (b["created"] + timedelta(days=4))

    evidence: list[str] = []
    if b["shipment"]:
        evidence += ["awb", "ship_on_time"]
    if b["pod_doc"]:
        evidence += ["pod", "otp_verified"]
        if scenario != CONFLICT_PIN:
            evidence.append("address_match")

    # scenario-specific emails / refunds / twins ------------------------------
    if scenario == HINGLISH:
        line = rng.choice(HINGLISH_ADMISSIONS).format(
            date=delivered_at.strftime("%d %B"))
        _add_email(w, b["email"], merchant, line, delivered_at + timedelta(days=1))
        evidence.append("admission_email")
    elif scenario == CONFLICT_DENY:
        _add_email(w, b["email"], merchant, rng.choice(DENIALS),
                   delivered_at + timedelta(days=2))
    elif scenario == CANCEL_AFTER_SHIP:
        ship_dt = datetime.fromisoformat(b["shipment"].ship_date)
        line = ("I want to cancel this order, mujhe ab nahi chahiye. "
                "cancel kar do please.")
        _add_email(w, b["email"], merchant, line, ship_dt + timedelta(hours=rng.randint(6, 40)))
        evidence.append("cancellation_after_ship_email")
    elif scenario == PARTIAL_REFUND:
        refund_amt = max(100, int(amount * rng.uniform(0.2, 0.5)))
        w.repo.add_refund(Refund(id=w.next_id("rf"), order_id=order.id,
                                 amount=refund_amt,
                                 created_at=iso(delivered_at + timedelta(days=2))))
        amount = amount - refund_amt      # disputed amount is the remainder
        evidence.append("refund_reconciled")

    twin_note = None
    if scenario == AMBIGUOUS:
        # a re-import twin: same customer & amount, minutes apart, own payment id
        _order_bundle(w, merchant, amount=order.amount,
                      customer=(b["name"], b["email"]),
                      created_at=b["created"] + timedelta(minutes=rng.randint(3, 40)))
        twin_note = "payment_id on dispute is unresolvable; two candidate orders"

    # dispute row --------------------------------------------------------------
    hours_left = (rng.uniform(6.0, 36.0) if scenario == DELAYED
                  else rng.uniform(48.0, 168.0))
    payment_id = (f"pay_unknown_{w.next_id('unk')[4:]}"
                  if scenario == AMBIGUOUS else order.payment_id)
    reason = {
        HINGLISH: rng.choice([ReasonCode.NOT_AS_DESCRIBED, ReasonCode.GOODS_NOT_RECEIVED]),
        PARTIAL_REFUND: ReasonCode.CREDIT_NOT_PROCESSED,
        HOPELESS: rng.choice([ReasonCode.FRAUD, ReasonCode.GOODS_NOT_RECEIVED]),
        HIGH_VALUE: ReasonCode.FRAUD,
        CANCEL_AFTER_SHIP: ReasonCode.CANCELLED_RECURRING,
        DUP_EVENT: ReasonCode.DUPLICATE,
        CLEAN: rng.choice([ReasonCode.GOODS_NOT_RECEIVED, ReasonCode.NOT_AS_DESCRIBED,
                           ReasonCode.DUPLICATE, ReasonCode.CANCELLED_RECURRING]),
    }.get(scenario, ReasonCode.GOODS_NOT_RECEIVED)

    dispute = Dispute(id=w.next_id("disp"), payment_id=payment_id, amount=amount,
                      reason_code=reason, respond_by=iso(SIM_NOW + timedelta(hours=hours_left)))
    w.repo.add_dispute(dispute)

    # webhook feed (duplicate scenario: delivered twice)
    arrival = iso(SIM_NOW - timedelta(hours=rng.uniform(0.5, 5.0)))
    w.events.append({"event": "dispute.created", "dispute_id": dispute.id,
                     "arrival": arrival})
    if scenario == DUP_EVENT:
        w.events.append({"event": "dispute.created", "dispute_id": dispute.id,
                         "arrival": iso(SIM_NOW - timedelta(minutes=rng.randint(2, 25)))})

    # ground truth ---------------------------------------------------------------
    complete = scenario in (CLEAN, HINGLISH, DUP_EVENT, PARTIAL_REFUND, DELAYED,
                            CONFLICT_DENY, HIGH_VALUE, CANCEL_AFTER_SHIP)
    hopeless = scenario == HOPELESS
    action = _correct_action(amount, hours_left, complete, hopeless)
    if scenario == AMBIGUOUS:
        action = "ESCALATE"               # linking itself is the blocker
    p_win = (0.90 if "admission_email" in evidence else
             0.60 if scenario == CONFLICT_DENY else
             0.85 if complete else 0.10)
    w.ground_truth[dispute.id] = {
        "scenario": scenario,
        "order_id": order.id,
        "gt_correct_action": action,
        "gt_evidence_present": sorted(evidence),
        "gt_outcome_if_fought": _outcome_if_fought(rng, p_win),
        "hours_left_at_sim_now": round(hours_left, 1),
        **({"note": twin_note} if twin_note else {}),
    }


def _stratified_split(gt: dict[str, dict], rng: Random, n_heldout: int) -> dict:
    by_scenario: dict[str, list[str]] = {}
    for did in sorted(gt):
        by_scenario.setdefault(gt[did]["scenario"], []).append(did)
    held: list[str] = []
    for scen in sorted(by_scenario):
        ids = by_scenario[scen][:]
        rng.shuffle(ids)
        take = max(1, len(ids) // 3)
        held.extend(ids[:take])
    # trim/pad deterministically to exactly n_heldout
    rng.shuffle(held)
    if len(held) > n_heldout:
        held = held[:n_heldout]
    else:
        remaining = [d for d in sorted(gt) if d not in set(held)]
        rng.shuffle(remaining)
        held.extend(remaining[: n_heldout - len(held)])
    held_set = set(held)
    dev = [d for d in sorted(gt) if d not in held_set]
    return {"dev": sorted(dev), "held_out": sorted(held)}


# --- validation ------------------------------------------------------------------

def validate_dataset(out_dir: str | Path) -> dict:
    """Check invariants over the generated artifacts; return derived stats.

    Raises DatasetError on any violation — also used by tests that tamper with
    the data on purpose.
    """
    out = Path(out_dir)
    repo = Repository(out / "dataset.db")
    try:
        gt = json.loads((out / "ground_truth.json").read_text())["labels"]
        split = json.loads((out / "split.json").read_text())
        events = [json.loads(l) for l in
                  (out / "events.jsonl").read_text().splitlines() if l.strip()]

        q = lambda sql, *a: repo.conn.execute(sql, a).fetchall()
        n_orders = q("SELECT COUNT(*) c FROM orders")[0]["c"]
        disputes = q("SELECT * FROM disputes")
        n_disputes = len(disputes)

        errors: list[str] = []

        # split integrity
        dev, held = set(split["dev"]), set(split["held_out"])
        if dev & held:
            errors.append(f"dev/held-out overlap: {sorted(dev & held)[:3]}")
        if dev | held != set(gt):
            errors.append("split union != ground-truth dispute ids")
        if len(held) != 40:
            errors.append(f"held-out size {len(held)} != 40")

        # per-dispute referential + monetary coherence
        ev_count = {}
        for e in events:
            ev_count[e["dispute_id"]] = ev_count.get(e["dispute_id"], 0) + 1
        for d in disputes:
            did = d["id"]
            if did not in gt:
                errors.append(f"{did}: no ground truth")
                continue
            g = gt[did]
            scen = g["scenario"]
            order = repo.get_order(g["order_id"])
            if order is None:
                errors.append(f"{did}: gt order missing")
                continue
            refunds = sum(r.amount for r in repo.list_refunds_for_order(order.id))
            if refunds > order.amount:
                errors.append(f"{did}: refunds {refunds} exceed order {order.amount}")
            if scen == AMBIGUOUS:
                if repo.get_order_by_payment(d["payment_id"]) is not None:
                    errors.append(f"{did}: ambiguous dispute payment_id resolves")
            else:
                if repo.get_order_by_payment(d["payment_id"]) is None:
                    errors.append(f"{did}: payment_id does not resolve")
                if order.amount - refunds != d["amount"]:
                    errors.append(f"{did}: amount {d['amount']} != order-refunds "
                                  f"{order.amount - refunds}")
            ships = repo.list_shipments_for_order(order.id)
            if scen == MISSING_POD and (not ships or ships[0].pod_doc_id is not None):
                errors.append(f"{did}: missing_pod scenario has a POD")
            if scen == HOPELESS and ships:
                errors.append(f"{did}: hopeless scenario has a shipment")
            if ships and order.created_at > ships[0].ship_date:
                errors.append(f"{did}: shipment predates order")
            expected_events = 2 if scen == DUP_EVENT else 1
            if ev_count.get(did, 0) != expected_events:
                errors.append(f"{did}: {ev_count.get(did, 0)} events, "
                              f"expected {expected_events}")
            if scen == DELAYED and g["hours_left_at_sim_now"] > 36:
                errors.append(f"{did}: delayed but {g['hours_left_at_sim_now']}h left")
            if g["gt_correct_action"] not in ("FIGHT", "ACCEPT", "ESCALATE"):
                errors.append(f"{did}: bad gt action")
            if g["gt_outcome_if_fought"] not in ("won", "lost"):
                errors.append(f"{did}: bad gt outcome")

        # structural presence of text imperfections. One OR-query over the
        # canonical marker list: a doc matching two markers can't double-count,
        # and the count must equal the Hinglish quota exactly (drift guard).
        where = " OR ".join("raw_text LIKE ?" for _ in HINGLISH_MARKERS)
        hinglish_markers = q(
            f"SELECT COUNT(*) c FROM documents WHERE type='email' AND ({where})",
            *[f"%{m}%" for m in HINGLISH_MARKERS])[0]["c"]
        if hinglish_markers != SCENARIO_QUOTAS[HINGLISH]:
            errors.append(f"hinglish marker docs {hinglish_markers} != "
                          f"quota {SCENARIO_QUOTAS[HINGLISH]}")
        pin_mismatch = 0
        for did, g in gt.items():
            if g["scenario"] != CONFLICT_PIN:
                continue
            order = repo.get_order(g["order_id"])
            ship = repo.list_shipments_for_order(order.id)[0]
            pod = repo.get_document(ship.pod_doc_id)
            pin = order.address.rsplit(" ", 1)[-1]
            if pin not in pod.raw_text:
                pin_mismatch += 1
            else:
                errors.append(f"{did}: conflicting_pincode POD matches order pincode")

        scen_counts: dict[str, int] = {}
        for g in gt.values():
            scen_counts[g["scenario"]] = scen_counts.get(g["scenario"], 0) + 1
        for scen, quota in SCENARIO_QUOTAS.items():
            if scen != CLEAN and scen_counts.get(scen, 0) != quota:
                errors.append(f"scenario {scen}: {scen_counts.get(scen, 0)} != quota {quota}")

        if errors:
            raise DatasetError(f"{len(errors)} violation(s):\n  " + "\n  ".join(errors[:12]))

        return {
            "orders": n_orders,
            "disputes": n_disputes,
            "dev": len(dev), "held_out": len(held),
            "documents": q("SELECT COUNT(*) c FROM documents")[0]["c"],
            "shipments": q("SELECT COUNT(*) c FROM shipments")[0]["c"],
            "refunds": q("SELECT COUNT(*) c FROM refunds")[0]["c"],
            "webhook_events": len(events),
            "scenario_counts": dict(sorted(scen_counts.items())),
            "missing_pod": scen_counts.get(MISSING_POD, 0),
            "conflicting_records": scen_counts.get(CONFLICT_PIN, 0)
                                   + scen_counts.get(CONFLICT_DENY, 0),
            "duplicate_webhooks": scen_counts.get(DUP_EVENT, 0),
            "partial_refunds": scen_counts.get(PARTIAL_REFUND, 0),
            "hinglish_marker_docs": hinglish_markers,
            "delayed_disputes": scen_counts.get(DELAYED, 0),
            "pincode_mismatch_pods": pin_mismatch,
        }
    finally:
        repo.close()


def format_summary(stats: dict) -> str:
    lines = [
        f"Orders: {stats['orders']}",
        f"Disputes: {stats['disputes']}",
        f"Development: {stats['dev']}",
        f"Held-out: {stats['held_out']}",
        "",
        f"Missing POD: {stats['missing_pod']}",
        f"Conflicting records: {stats['conflicting_records']} "
        f"(pincode-mismatch PODs verified: {stats['pincode_mismatch_pods']})",
        f"Duplicate webhook deliveries: {stats['duplicate_webhooks']}",
        f"Partial refunds: {stats['partial_refunds']}",
        f"Hinglish communications (marker docs): {stats['hinglish_marker_docs']}",
        f"Delayed disputes (<36h left): {stats['delayed_disputes']}",
        "",
        f"Documents: {stats['documents']}  Shipments: {stats['shipments']}  "
        f"Refunds: {stats['refunds']}  Webhook events: {stats['webhook_events']}",
        "",
        "Scenario mix: " + ", ".join(f"{k}={v}" for k, v in stats["scenario_counts"].items()),
    ]
    return "\n".join(lines)
