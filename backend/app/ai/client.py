"""LLM client abstraction.

Providers:
- AnthropicClient  — real API via HTTPS; requires ANTHROPIC_API_KEY.
- StubAIClient     — deterministic, offline, FAITHFUL: it parses the prompt's
                     input JSON and answers the way a competent model would,
                     using pure regex/logic. Lets the entire pipeline run and
                     be tested with zero network and zero credentials.
- ScriptedAIClient — returns pre-scripted responses; used by tests to inject
                     malformed/adversarial outputs into the validation layer.

Selection is explicit via config. Choosing "anthropic" without a key raises
AIConfigError — the system never silently downgrades a real-provider request
to the stub.

Structural constraint (test-enforced): this package never imports app.tools,
app.store.repo, or sqlite3 — the AI cannot read the database or execute
actions; it only transforms the inputs it is handed.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

from .. import config
from .errors import AIConfigError, AIProviderError

_JSON_BLOCK_RE = re.compile(r"```json\s*(.*?)```", re.S)
_TASK_RE = re.compile(r"^# Task: (\w+)", re.M)


@dataclass(frozen=True)
class AIResponse:
    text: str
    model: str
    provider: str
    latency_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None


class AnthropicClient:
    """Thin HTTPS client for the Anthropic Messages API."""

    provider = "anthropic"

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key if api_key is not None else config.ANTHROPIC_API_KEY
        self.model = model or config.LLM_MODEL
        if not self.api_key:
            raise AIConfigError(
                "AI provider 'anthropic' selected but ANTHROPIC_API_KEY is not "
                "set. Export the key, or set RECOURSE_AI_PROVIDER=stub for "
                "offline development.")

    def complete_vision(self, prompt: str, image_b64: str,
                        media_type: str) -> str:
        """One vision call (R5): image + instruction -> plain text. The key
        stays server-side; the caller receives only the transcription."""
        import requests
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": self.api_key,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": self.model, "max_tokens": 1024,
                  "messages": [{"role": "user", "content": [
                      {"type": "image", "source": {
                          "type": "base64", "media_type": media_type,
                          "data": image_b64}},
                      {"type": "text", "text": prompt}]}]},
            timeout=120)
        resp.raise_for_status()
        return "".join(b.get("text", "")
                       for b in resp.json().get("content", []))

    def complete(self, prompt: str) -> AIResponse:
        import requests  # local import: only the real provider needs HTTP
        started = time.monotonic()
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": self.api_key,          # never logged anywhere
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={"model": self.model,
                  "max_tokens": config.AI_MAX_TOKENS,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=60)
        latency = int((time.monotonic() - started) * 1000)
        if resp.status_code != 200:
            raise AIProviderError(
                f"Anthropic API returned {resp.status_code}: {resp.text[:200]}")
        body = resp.json()
        text = "".join(b.get("text", "") for b in body.get("content", []))
        usage = body.get("usage", {})
        return AIResponse(text=text, model=self.model, provider=self.provider,
                          latency_ms=latency,
                          input_tokens=usage.get("input_tokens"),
                          output_tokens=usage.get("output_tokens"))


class StubAIClient:
    """Deterministic offline model. Faithful by design: it extracts only what
    is really in the documents and links only among given candidates."""

    provider = "stub"
    model = "stub-deterministic-v1"

    ADMISSION_MARKERS = ("mil gaya", "receive ho gaya", "delivery ho gayi",
                         "aa gaya")

    def complete(self, prompt: str) -> AIResponse:
        task_m = _TASK_RE.search(prompt)
        blocks = _JSON_BLOCK_RE.findall(prompt)
        if not task_m or not blocks:
            text = '{"error": "stub could not find task or input"}'
        else:
            payload = json.loads(blocks[-1])
            task = task_m.group(1)
            handler = getattr(self, f"_{task}", None)
            text = handler(payload) if handler else '{"error": "unknown task"}'
        return AIResponse(text=text, model=self.model, provider=self.provider,
                          latency_ms=0)

    # -- task behaviors ---------------------------------------------------------

    def _link_order(self, p: dict) -> str:
        dispute, candidates = p["dispute"], p["candidates"]
        same_amount = [c for c in candidates if c["amount"] == dispute["amount"]]
        if len(same_amount) == 1:
            pick, conf = same_amount[0], 0.92
            why = (f"only candidate whose amount \u20b9{pick['amount']} equals "
                   f"the disputed amount")
        elif len(same_amount) > 1:
            pick = sorted(same_amount, key=lambda c: c["created_at"])[0]
            conf = 0.55
            why = (f"{len(same_amount)} candidates share amount "
                   f"\u20b9{dispute['amount']} for the same customer; picking "
                   f"the earliest but the tie cannot be resolved from the data")
        else:
            pick, conf = candidates[0], 0.30
            why = "no candidate matches the disputed amount; weak best guess"
        return json.dumps({"order_id": pick["id"], "confidence": conf,
                           "reasoning": why})

    def _extract_evidence(self, p: dict) -> str:
        out = []
        wanted = {c["key"]: c for c in p["checklist"]}
        for doc in p["documents"]:
            text, did = doc["raw_text"], doc["id"]
            if doc["type"] == "pod":
                awb = re.search(r"^AWB: (.+)$", text, re.M)
                if awb and "awb" in wanted:
                    out.append({"key": "awb", "claim": "a shipment exists",
                                "source_doc_id": did, "quoted_span": awb.group(0),
                                "fields": {"awb": awb.group(1).strip()}})
                delivered = re.search(r"^Delivered: (.+)$", text, re.M)
                if delivered and awb and "pod" in wanted:
                    out.append({"key": "pod", "claim": "the shipment was delivered",
                                "source_doc_id": did,
                                "quoted_span": delivered.group(0),
                                "fields": {"awb": awb.group(1).strip(),
                                           "delivered_at": delivered.group(1).strip()}})
                addr = re.search(r"^Address: (.+)$", text, re.M)
                if addr and "address_match" in wanted:
                    pins = re.findall(r"\b\d{6}\b", addr.group(1))
                    if pins:
                        out.append({"key": "address_match",
                                    "claim": "delivered to the ordered address",
                                    "source_doc_id": did,
                                    "quoted_span": addr.group(0),
                                    "fields": {"pincode": pins[-1]}})
                if "Delivery OTP verified: YES" in text and "otp_verified" in wanted:
                    out.append({"key": "otp_verified",
                                "claim": "delivery OTP was verified",
                                "source_doc_id": did,
                                "quoted_span": "Delivery OTP verified: YES",
                                "fields": {}})
            elif doc["type"] == "email" and "admission_email" in wanted:
                found = self._admission(text)
                if found:
                    sent_at, line = found
                    out.append({"key": "admission_email",
                                "claim": "customer acknowledged receiving the parcel",
                                "source_doc_id": did, "quoted_span": line,
                                "fields": {"sent_at": sent_at}})
        return json.dumps({"evidence": out})

    def _admission(self, thread: str):
        for block in thread.split("\n---\n"):
            lines = block.strip().splitlines()
            if len(lines) < 3 or "support@" in lines[0]:
                continue
            body = "\n".join(lines[3:]).strip() or lines[-1].strip()
            if any(m in body for m in self.ADMISSION_MARKERS):
                return lines[1].removeprefix("Date: ").strip(), body
        return None

    def _draft_representment(self, p: dict) -> str:
        d, ev = p["dispute"], p["admitted_evidence"]
        lines = [f"RE: Dispute {d['id']} \u2014 merchant representment",
                 "We respectfully contest this dispute on the evidence below."]
        for e in ev:
            k = e["key"]
            f = e.get("fields", {})
            if k == "awb":
                lines.append(f"A shipment exists for this payment under airway "
                             f"bill {f.get('awb')} [{e['display_id']}].")
            elif k == "pod":
                lines.append(f"Courier records confirm delivery on "
                             f"{f.get('delivered_at')} [{e['display_id']}].")
            elif k == "address_match":
                lines.append(f"The proof of delivery shows pincode "
                             f"{f.get('pincode')}, matching the order address "
                             f"[{e['display_id']}].")
            elif k == "otp_verified":
                lines.append(f"The courier verified a delivery OTP at handover "
                             f"[{e['display_id']}].")
            elif k == "admission_email":
                lines.append(f"In their own message of {f.get('sent_at')} the "
                             f"customer acknowledged receipt: "
                             f"\"{e['quoted_span']}\" [{e['display_id']}].")
        lines.append("We request that the dispute be resolved in the "
                     "merchant's favour on this record.")
        return "\n".join(lines)


class ScriptedAIClient:
    """Returns queued responses verbatim — for adversarial validation tests."""

    provider = "scripted"
    model = "scripted"

    def __init__(self, responses: list[str]):
        self._queue = list(responses)
        self.calls: list[str] = []          # prompts received, for assertions

    def complete(self, prompt: str) -> AIResponse:
        self.calls.append(prompt)
        if not self._queue:
            raise AIProviderError("scripted client ran out of responses")
        return AIResponse(text=self._queue.pop(0), model=self.model,
                          provider=self.provider, latency_ms=0)


def get_client(provider: str | None = None):
    provider = provider or config.AI_PROVIDER
    if provider == "stub":
        return StubAIClient()
    if provider == "anthropic":
        return AnthropicClient()           # raises AIConfigError without a key
    raise AIConfigError(f"unknown AI provider '{provider}' "
                        f"(expected 'stub' or 'anthropic')")
