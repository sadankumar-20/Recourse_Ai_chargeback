#!/usr/bin/env python3
"""Real-provider smoke test. Runs ONLY when explicitly requested with
credentials: RECOURSE_AI_PROVIDER=anthropic ANTHROPIC_API_KEY=... python3 scripts/ai_smoke.py
Sends one tiny link_order call against the live API and prints the call
record (never the key). Not part of the test suite — the suite is offline."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app import config  # noqa: E402
from app.ai.client import get_client  # noqa: E402
from app.ai.link_order import link_order  # noqa: E402
from app.store.models import Dispute, DisputeStatus, Order, ReasonCode  # noqa: E402

if config.AI_PROVIDER != "anthropic":
    sys.exit("Set RECOURSE_AI_PROVIDER=anthropic (and ANTHROPIC_API_KEY) to run "
             "the live smoke test. The default test suite never needs this.")

d = Dispute(id="smoke_1", payment_id="pay_x", amount=3499,
            reason_code=ReasonCode.GOODS_NOT_RECEIVED,
            respond_by="2026-08-27T12:00:00+00:00", status=DisputeStatus.OPEN)
orders = [Order("ord_a", "m", "p1", 3499, "a@b.c", "12 MG Road 560038",
                "2026-08-01T00:00:00+00:00", "2026-08-04T00:00:00+00:00"),
          Order("ord_b", "m", "p2", 999, "a@b.c", "12 MG Road 560038",
                "2026-08-02T00:00:00+00:00", "2026-08-05T00:00:00+00:00")]
res = link_order(d, orders, get_client())
print("proposal:", res.proposal)
for r in res.records:
    print("record:", r.to_dict())
