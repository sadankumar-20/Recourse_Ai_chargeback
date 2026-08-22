"""Payments execution adapters (spec §11, §30.5).

The adapter EXECUTES already-approved actions. It never decides FIGHT /
ACCEPT / ESCALATE — that is the Stage-5 decision engine's job — and the AI
package structurally cannot import this module (AST-enforced in tests).

Two implementations:
- SimulatorAdapter    — deterministic local dispute lifecycle over the
                        Stage-2 store. Every response is labeled
                        simulated=True. Supports controlled 503 injection
                        for future retry orchestration.
- RazorpayTestAdapter — real Razorpay test-mode HTTP for payment/refund
                        lookups. Contest/accept raise NotSupported honestly:
                        Razorpay test mode provides no way to create
                        synthetic disputes to contest, and we do not fake
                        API responses (README documents this).

Idempotency persistence and audit writing live in tools/executor.py — one
writer for money actions. The simulator additionally refuses to action a
dispute that is not open (defense in depth if the executor is bypassed).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Protocol

from .. import config
from ..store.models import DisputeStatus
from ..store.repo import Repository


class NotSupported(RuntimeError):
    """The selected provider genuinely cannot perform this operation."""


class PaymentsError(RuntimeError):
    """Non-transient execution failure (e.g. dispute already actioned)."""


class TransientPaymentsError(RuntimeError):
    """Retryable failure (e.g. HTTP 503). The future orchestrator retries."""

    def __init__(self, message: str, status: int = 503):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class AdapterResult:
    ok: bool
    data: dict
    adapter: str
    simulated: bool

    def to_dict(self) -> dict:
        return {"ok": self.ok, "data": self.data, "adapter": self.adapter,
                "simulated": self.simulated}


class PaymentsAdapter(Protocol):
    """Small on purpose: lookups + the two irreversible actions + status."""

    name: str
    simulated: bool

    def fetch_payment(self, payment_id: str) -> AdapterResult: ...
    def fetch_refunds(self, payment_id: str) -> AdapterResult: ...
    def contest_dispute(self, dispute_id: str, evidence_bundle: dict) -> AdapterResult: ...
    def accept_dispute(self, dispute_id: str) -> AdapterResult: ...
    def dispute_status(self, dispute_id: str) -> AdapterResult: ...


# --- simulator ---------------------------------------------------------------------

class SimulatorAdapter:
    """Deterministic dispute lifecycle:
        open -> contest -> under_review -> tick() -> won|lost
        open -> accept  -> accepted
    Outcomes come from an injected map (the eval layer loads ground truth);
    unknown disputes resolve by a documented deterministic fallback
    (sha256(dispute_id) parity) so the same id always resolves the same way.
    """

    name = "simulator"
    simulated = True

    def __init__(self, repo: Repository, outcomes: dict[str, str] | None = None,
                 failures: dict[str, int | str] | None = None):
        self.repo = repo
        self.outcomes = dict(outcomes or {})
        # failures: dispute_id -> N (first N calls raise 503) or "always"
        self._failures: dict[str, int | str] = dict(failures or {})

    # -- lookups ------------------------------------------------------------------

    def fetch_payment(self, payment_id: str) -> AdapterResult:
        order = self.repo.get_order_by_payment(payment_id)
        if order is None:
            raise PaymentsError(f"no payment {payment_id!r} in the simulated world")
        return AdapterResult(ok=True, adapter=self.name, simulated=True,
                             data={"payment_id": payment_id,
                                   "amount": order.amount,
                                   "email": order.customer_email,
                                   "captured": True})

    def fetch_refunds(self, payment_id: str) -> AdapterResult:
        order = self.repo.get_order_by_payment(payment_id)
        refunds = (self.repo.list_refunds_for_order(order.id) if order else [])
        return AdapterResult(ok=True, adapter=self.name, simulated=True,
                             data={"payment_id": payment_id,
                                   "refunds": [{"id": r.id, "amount": r.amount,
                                                "created_at": r.created_at}
                                               for r in refunds]})

    # -- actions -------------------------------------------------------------------

    def _maybe_fail(self, dispute_id: str) -> None:
        plan = self._failures.get(dispute_id)
        if plan == "always":
            raise TransientPaymentsError(
                f"simulated 503 for {dispute_id} (always-fail plan)")
        if isinstance(plan, int) and plan > 0:
            self._failures[dispute_id] = plan - 1
            raise TransientPaymentsError(
                f"simulated 503 for {dispute_id} ({plan} failure(s) remaining)")

    def _require_open(self, dispute_id: str):
        dispute = self.repo.get_dispute(dispute_id)
        if dispute is None:
            raise PaymentsError(f"unknown dispute {dispute_id!r}")
        if dispute.status is not DisputeStatus.OPEN:
            raise PaymentsError(
                f"dispute {dispute_id} is '{dispute.status.value}', not open — "
                f"a dispute receives at most one money action")
        return dispute

    def contest_dispute(self, dispute_id: str, evidence_bundle: dict) -> AdapterResult:
        self._maybe_fail(dispute_id)
        self._require_open(dispute_id)
        self.repo.update_dispute_status(dispute_id, DisputeStatus.UNDER_REVIEW)
        return AdapterResult(ok=True, adapter=self.name, simulated=True,
                             data={"dispute_id": dispute_id,
                                   "status": "under_review",
                                   "evidence_items": len(
                                       evidence_bundle.get("evidence", [])),
                                   "note": "SIMULATED submission"})

    def accept_dispute(self, dispute_id: str) -> AdapterResult:
        self._maybe_fail(dispute_id)
        self._require_open(dispute_id)
        self.repo.update_dispute_status(dispute_id, DisputeStatus.ACCEPTED)
        return AdapterResult(ok=True, adapter=self.name, simulated=True,
                             data={"dispute_id": dispute_id,
                                   "status": "accepted",
                                   "note": "SIMULATED acceptance"})

    def dispute_status(self, dispute_id: str) -> AdapterResult:
        dispute = self.repo.get_dispute(dispute_id)
        if dispute is None:
            raise PaymentsError(f"unknown dispute {dispute_id!r}")
        return AdapterResult(ok=True, adapter=self.name, simulated=True,
                             data={"dispute_id": dispute_id,
                                   "status": dispute.status.value})

    def tick(self, dispute_id: str) -> AdapterResult:
        """Advance an under_review dispute to its resolution — the simulated
        passage of network review time."""
        dispute = self.repo.get_dispute(dispute_id)
        if dispute is None or dispute.status is not DisputeStatus.UNDER_REVIEW:
            raise PaymentsError(
                f"tick requires an under_review dispute, got "
                f"{dispute.status.value if dispute else 'missing'}")
        result = self.outcomes.get(dispute_id) or self._fallback_outcome(dispute_id)
        status = DisputeStatus.WON if result == "won" else DisputeStatus.LOST
        self.repo.update_dispute_status(dispute_id, status)
        return AdapterResult(ok=True, adapter=self.name, simulated=True,
                             data={"dispute_id": dispute_id,
                                   "status": status.value,
                                   "note": "SIMULATED resolution"})

    @staticmethod
    def _fallback_outcome(dispute_id: str) -> str:
        digest = hashlib.sha256(dispute_id.encode()).digest()
        return "won" if digest[0] % 2 == 0 else "lost"


# --- Razorpay test mode -------------------------------------------------------------

class RazorpayTestAdapter:
    """Real test-mode HTTP for lookups; honest NotSupported for the dispute
    lifecycle. We do not fabricate Razorpay responses."""

    name = "razorpay_test"
    simulated = False
    BASE = "https://api.razorpay.com/v1"

    def __init__(self, key_id: str | None = None, key_secret: str | None = None):
        self.key_id = key_id if key_id is not None else config.RAZORPAY_KEY_ID
        self.key_secret = (key_secret if key_secret is not None
                           else config.RAZORPAY_KEY_SECRET)
        if not self.key_id or not self.key_secret:
            raise PaymentsError(
                "payments provider 'razorpay_test' selected but "
                "RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not set. Export "
                "test-mode credentials, or use RECOURSE_PAYMENTS_ADAPTER="
                "simulator.")

    def _get(self, path: str) -> dict:
        import requests
        resp = requests.get(f"{self.BASE}{path}",
                            auth=(self.key_id, self.key_secret), timeout=30)
        if resp.status_code == 503:
            raise TransientPaymentsError(f"Razorpay 503 on {path}")
        if resp.status_code != 200:
            raise PaymentsError(
                f"Razorpay returned {resp.status_code} on {path}: "
                f"{resp.text[:200]}")
        return resp.json()

    def fetch_payment(self, payment_id: str) -> AdapterResult:
        return AdapterResult(ok=True, adapter=self.name, simulated=False,
                             data=self._get(f"/payments/{payment_id}"))

    def fetch_refunds(self, payment_id: str) -> AdapterResult:
        return AdapterResult(ok=True, adapter=self.name, simulated=False,
                             data=self._get(f"/refunds?payment_id={payment_id}"))

    def contest_dispute(self, dispute_id: str, evidence_bundle: dict):
        raise NotSupported(
            "Razorpay test mode cannot create synthetic disputes, so there is "
            "nothing real to contest against; faking the response would "
            "misrepresent the integration. Use the simulator for the dispute "
            "lifecycle (documented in README).")

    def accept_dispute(self, dispute_id: str):
        raise NotSupported(
            "Razorpay test mode dispute acceptance is not exercisable without "
            "a real dispute; use the simulator (documented in README).")

    def dispute_status(self, dispute_id: str) -> AdapterResult:
        return AdapterResult(ok=True, adapter=self.name, simulated=False,
                             data=self._get(f"/disputes/{dispute_id}"))


def get_payments_adapter(repo: Repository, provider: str | None = None,
                         **kwargs) -> PaymentsAdapter:
    provider = provider or config.PAYMENTS_ADAPTER
    if provider == "simulator":
        return SimulatorAdapter(repo, **kwargs)
    if provider == "razorpay_test":
        return RazorpayTestAdapter()      # fails loudly without credentials
    raise PaymentsError(f"unknown payments provider '{provider}' "
                        f"(expected 'simulator' or 'razorpay_test')")
