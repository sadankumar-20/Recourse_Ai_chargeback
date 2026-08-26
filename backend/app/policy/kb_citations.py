"""Deterministic verification of knowledge-base citations (R3).

RAG retrieves; it does not decide truth. Any statement attributed to the
knowledge base must carry {source_id, chunk_id, quote}, and this module —
pure policy code, no AI imports — verifies the quote EXISTS VERBATIM in the
referenced chunk. Paraphrases fail. "Close enough" fails. The vocabulary of
failure is structured, exactly like gate verdicts:

    VALID            quote found verbatim in the referenced chunk
    MALFORMED        citation missing source_id / chunk_id / quote
    UNKNOWN_SOURCE   no such source in the knowledge base
    UNKNOWN_CHUNK    source exists, chunk does not
    SOURCE_MISMATCH  chunk exists but belongs to a different source
    QUOTE_MISMATCH   source and chunk exist; the quote is not verbatim
"""

from __future__ import annotations

from dataclasses import dataclass

VALID = "VALID"
MALFORMED = "MALFORMED"
UNKNOWN_SOURCE = "UNKNOWN_SOURCE"
UNKNOWN_CHUNK = "UNKNOWN_CHUNK"
SOURCE_MISMATCH = "SOURCE_MISMATCH"
QUOTE_MISMATCH = "QUOTE_MISMATCH"

_MIN_QUOTE_CHARS = 12   # a citation must quote something substantive


@dataclass(frozen=True)
class KBCitationVerdict:
    status: str
    source_id: str | None
    chunk_id: str | None
    quote: str | None
    reason: str

    @property
    def valid(self) -> bool:
        return self.status == VALID

    def to_dict(self) -> dict:
        return {"status": self.status, "source_id": self.source_id,
                "chunk_id": self.chunk_id, "quote": self.quote,
                "reason": self.reason}


def verify_kb_citation(citation: dict, kb) -> KBCitationVerdict:
    source_id = citation.get("source_id")
    chunk_id = citation.get("chunk_id")
    quote = citation.get("quote")
    if (not isinstance(source_id, str) or not isinstance(chunk_id, str)
            or not isinstance(quote, str) or len(quote.strip())
            < _MIN_QUOTE_CHARS):
        return KBCitationVerdict(
            MALFORMED, source_id if isinstance(source_id, str) else None,
            chunk_id if isinstance(chunk_id, str) else None,
            quote if isinstance(quote, str) else None,
            f"citation requires string source_id, chunk_id, and a quote of "
            f"at least {_MIN_QUOTE_CHARS} characters")

    if not any(c.source_id == source_id for c in kb.chunks):
        return KBCitationVerdict(UNKNOWN_SOURCE, source_id, chunk_id, quote,
                                 f"no source '{source_id}' in the knowledge "
                                 f"base")
    chunk = kb.get(source_id, chunk_id)
    if chunk is None:
        owners = kb.chunk_owners(chunk_id)
        if owners:
            return KBCitationVerdict(
                SOURCE_MISMATCH, source_id, chunk_id, quote,
                f"chunk '{chunk_id}' belongs to "
                f"'{owners[0].source_id}', not '{source_id}'")
        return KBCitationVerdict(UNKNOWN_CHUNK, source_id, chunk_id, quote,
                                 f"source '{source_id}' has no chunk "
                                 f"'{chunk_id}'")
    if quote.strip() not in chunk.text:
        return KBCitationVerdict(
            QUOTE_MISMATCH, source_id, chunk_id, quote,
            f"quote is not verbatim in {source_id}:{chunk_id} — paraphrases "
            f"and edits are rejected; only exact text verifies")
    return KBCitationVerdict(VALID, source_id, chunk_id, quote,
                             "quote found verbatim in the referenced chunk")


def verify_kb_citations(citations: list[dict], kb) -> list[KBCitationVerdict]:
    """Order-preserving; duplicates each get their own verdict."""
    return [verify_kb_citation(c, kb) for c in citations]
