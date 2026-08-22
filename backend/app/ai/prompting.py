"""Prompt loading, rendering, and the untrusted-output call loop.

Every AI call follows one shape:
    render prompt -> call provider -> strict validation
      -> on SchemaError: ONE repair attempt (original prompt + the exact
         validator error) -> strict validation again
      -> still invalid: raise LowConfidence carrying all call records.

Observability: each provider call yields an AICallRecord (provider, model,
prompt name/version/sha256, latency, tokens, validation result, attempt
number). No prompts-with-secrets exist; API keys never enter records. The
orchestrator/audit stage will persist these records — the interface is kept
minimal on purpose (ADR-008).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .errors import LowConfidence, SchemaError

PROMPT_DIR = Path(__file__).parent / "prompts"


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    sha256: str
    template: str

    def render(self, payload: dict) -> str:
        return self.template.replace(
            "<<INPUT_JSON>>", json.dumps(payload, indent=1, ensure_ascii=False))


@dataclass(frozen=True)
class AICallRecord:
    task: str
    provider: str
    model: str
    prompt_version: str
    prompt_sha256: str
    attempt: int                # 1 = first call, 2 = repair
    latency_ms: int
    input_tokens: int | None
    output_tokens: int | None
    valid: bool
    validation_error: str | None

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def load_prompt(name: str) -> Prompt:
    text = (PROMPT_DIR / f"{name}.md").read_text()
    header, _, body = text.partition("\n---\n")
    version = header.replace("version:", "").strip()
    if not version:
        raise ValueError(f"prompt '{name}' missing version header")
    return Prompt(name=name, version=version,
                  sha256=hashlib.sha256(text.encode()).hexdigest(),
                  template=body)


def strip_json(text: str) -> str:
    """Tolerate a fenced code block around the JSON — nothing more."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.endswith("```"):
            t = t[: -3]
    return t.strip()


def call_with_repair(client, prompt: Prompt, payload: dict,
                     validator: Callable[[str], object], task: str
                     ) -> tuple[object, tuple[AICallRecord, ...]]:
    """One call, one repair, then LowConfidence. Never silent coercion."""
    records: list[AICallRecord] = []
    rendered = prompt.render(payload)
    last_error = ""
    for attempt in (1, 2):
        message = rendered if attempt == 1 else (
            rendered
            + "\n\n## Correction required\nYour previous response was invalid: "
            + last_error
            + "\nReturn ONLY the corrected output, following the schema exactly.")
        resp = client.complete(message)
        try:
            value = validator(resp.text)
            records.append(_record(task, client, prompt, attempt, resp, None))
            return value, tuple(records)
        except SchemaError as e:
            last_error = str(e)
            records.append(_record(task, client, prompt, attempt, resp, last_error))
    raise LowConfidence(task=task,
                        reason=f"invalid after repair retry: {last_error}",
                        records=tuple(records))


def _record(task, client, prompt, attempt, resp, error) -> AICallRecord:
    return AICallRecord(
        task=task, provider=resp.provider, model=resp.model,
        prompt_version=prompt.version, prompt_sha256=prompt.sha256,
        attempt=attempt, latency_ms=resp.latency_ms,
        input_tokens=resp.input_tokens, output_tokens=resp.output_tokens,
        valid=error is None, validation_error=error)
