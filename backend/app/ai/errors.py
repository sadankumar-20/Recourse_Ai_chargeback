"""AI-layer error types. Failures here are loud and typed — never silent
defaults. The future orchestrator converts LowConfidence into safe escalation."""

from __future__ import annotations


class AIConfigError(RuntimeError):
    """Provider misconfiguration (e.g. real provider selected without a key)."""


class AIProviderError(RuntimeError):
    """The provider call itself failed (HTTP error, transport failure)."""


class SchemaError(ValueError):
    """LLM output failed strict validation. Triggers exactly one repair retry."""


class LowConfidence(Exception):
    """The AI layer cannot safely produce a valid structured result.

    Raised after the single repair retry also fails, or when inputs make a
    safe answer impossible (e.g. empty candidate list). Carries the call
    records so the audit trail can show exactly what was attempted."""

    def __init__(self, task: str, reason: str, records: tuple = ()):
        super().__init__(f"{task}: {reason}")
        self.task = task
        self.reason = reason
        self.records = records
