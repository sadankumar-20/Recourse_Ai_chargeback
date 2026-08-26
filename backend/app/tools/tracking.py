"""Courier tracking providers (R5).

Two implementations behind one record shape, chosen by
RECOURSE_TRACKING = simulator (default) | aftership:

- SimulatorTracking: the R2 behavior — reconstructs the courier's delivery
  record read-only from world state (provenance 'simulator', honestly
  labeled).
- AfterShipTracking: real HTTP against the AfterShip v4 API, env-keyed
  (AFTERSHIP_API_KEY), loud failure without a key, never a silent fallback.
  Results carry provenance 'tracking_api'. The HTTP transport is injectable
  so the adapter's URL construction, headers, and response parsing are fully
  tested offline; the live path just supplies requests.get.

Real-provider honesty: AfterShip may not return a destination address, so a
materialized confirmation may lack the address line — the gate's
address_match evidence simply won't be extractable and completeness math
handles it. We do not invent addresses.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .. import config
from ..store.models import Provenance


class TrackingError(RuntimeError):
    """Configuration or provider failure — always loud."""


@dataclass(frozen=True)
class TrackingRecord:
    awb: str
    courier: str
    status: str                      # delivered | in_transit | ... | unknown
    provenance: str
    delivered_at: str | None = None
    receiver: str | None = None
    address: str | None = None

    def to_dict(self) -> dict:
        return {"awb": self.awb, "courier": self.courier,
                "status": self.status, "provenance": self.provenance,
                "delivered_at": self.delivered_at, "receiver": self.receiver,
                "address": self.address}


def simulator_track(ro, awb: str) -> TrackingRecord | None:
    """Read-only reconstruction from the synthetic world (R2 semantics)."""
    rows = ro.select(
        "SELECT s.awb, s.courier, s.ship_date, s.status, o.address, "
        "o.customer_email FROM shipments s JOIN orders o ON o.id = s.order_id "
        "WHERE s.awb = ?", (awb,))
    if not rows:
        return None
    s = rows[0]
    if s["status"] != "delivered":
        return TrackingRecord(awb=s["awb"], courier=s["courier"],
                              status=s["status"],
                              provenance=Provenance.SIMULATOR.value)
    delivered = (datetime.fromisoformat(s["ship_date"])
                 + timedelta(hours=72)).isoformat(timespec="seconds")
    receiver = "".join(c for c in s["customer_email"].split("@")[0]
                       if not c.isdigit()).replace(".", " ").title()
    return TrackingRecord(awb=s["awb"], courier=s["courier"],
                          status="delivered",
                          provenance=Provenance.SIMULATOR.value,
                          delivered_at=delivered, receiver=receiver,
                          address=s["address"])


class AfterShipTracking:
    BASE = "https://api.aftership.com/v4"

    def __init__(self, api_key: str | None = None, http_get=None):
        self.api_key = (api_key if api_key is not None
                        else config.AFTERSHIP_API_KEY)
        if not self.api_key:
            raise TrackingError(
                "tracking provider 'aftership' selected but "
                "AFTERSHIP_API_KEY is not set. Export the key, or use "
                "RECOURSE_TRACKING=simulator.")
        self._http_get = http_get or self._real_get

    def _real_get(self, url: str, headers: dict) -> tuple[int, dict]:
        import requests
        resp = requests.get(url, headers=headers, timeout=30)
        return resp.status_code, (resp.json() if resp.content else {})

    def track(self, awb: str, courier: str) -> TrackingRecord | None:
        slug = courier.strip().lower().replace(" ", "-")
        url = f"{self.BASE}/trackings/{slug}/{awb}"
        status_code, body = self._http_get(
            url, {"aftership-api-key": self.api_key,
                  "content-type": "application/json"})
        if status_code == 404:
            return None
        if status_code != 200:
            raise TrackingError(f"AfterShip returned {status_code} for {awb}")
        t = (body.get("data") or {}).get("tracking") or {}
        tag = (t.get("tag") or "unknown").lower()
        status = "delivered" if tag == "delivered" else tag.replace(" ", "_")
        dest = t.get("destination_address") or None
        return TrackingRecord(
            awb=awb, courier=courier, status=status,
            provenance=Provenance.TRACKING_API.value,
            delivered_at=t.get("shipment_delivery_date"),
            receiver=t.get("signed_by"), address=dest)


class TrackingMoreTracking:
    """TrackingMore v4 (free-tier friendly): plain Tracking-Api-Key header,
    no signature schemes, no permission matrix. Same rules as AfterShip:
    env-keyed, loud failure without a key, provenance tracking_api, never a
    silent fallback, transport injectable so parsing is offline-tested."""
    BASE = "https://api.trackingmore.com/v4"

    def __init__(self, api_key: str | None = None, http_get=None):
        self.api_key = (api_key if api_key is not None
                        else config.TRACKINGMORE_API_KEY)
        if not self.api_key:
            raise TrackingError(
                "tracking provider 'trackingmore' selected but "
                "TRACKINGMORE_API_KEY is not set. Export the key, or use "
                "RECOURSE_TRACKING=simulator.")
        self._http_get = http_get or self._real_get

    def _real_get(self, url: str, headers: dict) -> tuple[int, dict]:
        import requests
        resp = requests.get(url, headers=headers, timeout=30)
        return resp.status_code, (resp.json() if resp.content else {})

    def track(self, awb: str, courier: str) -> TrackingRecord | None:
        url = (f"{self.BASE}/trackings/get?tracking_numbers={awb}"
               f"&courier_code={courier.strip().lower().replace(' ', '-')}")
        status_code, body = self._http_get(
            url, {"Tracking-Api-Key": self.api_key,
                  "Content-Type": "application/json"})
        if status_code != 200:
            raise TrackingError(
                f"TrackingMore returned {status_code} for {awb}")
        rows = (body.get("data") or [])
        if not rows:
            return None
        r = rows[0]
        raw = (r.get("delivery_status") or r.get("status")
               or "unknown").lower()
        status = "delivered" if raw == "delivered" else raw.replace(" ", "_")
        return TrackingRecord(
            awb=awb, courier=courier, status=status,
            provenance=Provenance.TRACKING_API.value,
            delivered_at=r.get("latest_checkpoint_time")
                         if status == "delivered" else None,
            receiver=r.get("signed_by") or None,
            address=r.get("destination_address") or None)


def track_via_configured_provider(ro, awb: str,
                                  courier: str) -> TrackingRecord | None:
    provider = config.TRACKING_PROVIDER
    if provider == "simulator":
        return simulator_track(ro, awb)
    if provider == "aftership":
        return AfterShipTracking().track(awb, courier)
    if provider == "trackingmore":
        return TrackingMoreTracking().track(awb, courier)
    raise TrackingError(f"unknown tracking provider '{provider}' (expected "
                        f"'simulator', 'aftership', or 'trackingmore')")
