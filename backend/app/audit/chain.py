"""Tamper-evident audit chain (spec §19).

Canonicalization (documented, deterministic):
    canonical_json(payload) = json.dumps(payload, sort_keys=True,
                                         separators=(",", ":"),
                                         ensure_ascii=False)
Hashing:
    entry_hash = SHA256( prev_hash + "|" + case_id + "|" + step + "|"
                         + canonical_payload_json + "|" + at )
Chain scope is PER CASE: prev_hash is the entry_hash of the same case's
previous audit entry; the first entry of a case uses GENESIS = 64 zeros.
The global `seq` column stays authoritative for ordering; deletion or
reordering of a case's entries breaks the prev-hash linkage and is detected.

Redaction happens BEFORE hashing, so the stored payload and the hashed
payload are the same bytes: keys matching secret patterns are replaced with
"[REDACTED]" recursively. Secrets never enter the audit trail.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

GENESIS = "0" * 64

_SECRET_KEY_MARKERS = ("api_key", "apikey", "authorization", "key_secret",
                       "secret", "token", "password", "x-api-key")


def canonical_json(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def redact(payload):
    """Recursively replace values of secret-looking keys."""
    if isinstance(payload, dict):
        return {k: ("[REDACTED]" if any(m in k.lower()
                                        for m in _SECRET_KEY_MARKERS)
                    else redact(v))
                for k, v in payload.items()}
    if isinstance(payload, list):
        return [redact(v) for v in payload]
    return payload


def compute_entry_hash(prev_hash: str, case_id: str, step: str,
                       payload_json: str, at: str) -> str:
    material = "|".join((prev_hash, case_id, step, payload_json, at))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ChainReport:
    case_id: str
    valid: bool
    entries: int
    broken_at_seq: int | None = None
    reason: str | None = None

    def to_text(self) -> str:
        if self.valid:
            return (f"Audit chain VALID\nCase: {self.case_id}\n"
                    f"Entries: {self.entries}")
        return (f"Audit chain INVALID\nCase: {self.case_id}\n"
                f"Broken at entry: {self.broken_at_seq}\n"
                f"Reason: {self.reason}")


def verify_audit_chain(repo, case_id: str) -> ChainReport:
    """Recompute the whole per-case chain from genesis. Detects modified
    payloads, tampered prev/entry hashes, deleted entries, and reordering —
    and reports exactly where the chain broke."""
    entries = repo.read_audit(case_id)
    expected_prev = GENESIS
    for e in entries:
        if e.prev_hash != expected_prev:
            return ChainReport(
                case_id=case_id, valid=False, entries=len(entries),
                broken_at_seq=e.seq,
                reason=(f"chain link broken: prev_hash {str(e.prev_hash)[:12]}… "
                        f"does not match previous entry_hash "
                        f"{expected_prev[:12]}… (entry deleted, reordered, "
                        f"or prev_hash tampered)"))
        recomputed = compute_entry_hash(e.prev_hash, e.case_id, e.step,
                                        e.payload_json, e.at)
        if recomputed != e.entry_hash:
            return ChainReport(
                case_id=case_id, valid=False, entries=len(entries),
                broken_at_seq=e.seq,
                reason=(f"entry_hash mismatch: stored "
                        f"{str(e.entry_hash)[:12]}…, recomputed "
                        f"{recomputed[:12]}… (payload or metadata modified)"))
        expected_prev = e.entry_hash
    return ChainReport(case_id=case_id, valid=True, entries=len(entries))
