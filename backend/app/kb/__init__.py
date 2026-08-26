"""Local knowledge base (R3): versioned documents, deterministic chunks,
transparent lexical retrieval.

Philosophy (the Admissibility Gate's, applied to knowledge): retrieval
provides CONTEXT, never truth and never decisions. Anything generated from
these chunks must cite source_id + chunk_id + an exact quote, and
policy/kb_citations.py verifies the quote verbatim against the chunk —
UNKNOWN_SOURCE / UNKNOWN_CHUNK / SOURCE_MISMATCH / QUOTE_MISMATCH are
structured failures, exactly like gate verdicts.

No vector database: the corpus is a handful of policy documents where a
plain BM25 over lowercase word tokens is transparent, deterministic
(stable tie-break by chunk_id), offline, and testable. Documents carry a
version header; the corpus exposes a SHA-256 checksum so tests can pin
reproducibility.

Document format (kb/documents/*.md):
    source_id: dispute_policy
    version: v1
    title: Dispute Handling Policy
    ---
    ## section_slug
    prose...
Each `## section` becomes one chunk with chunk_id "<section_slug>_<nn>".
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

KB_DIR = Path(__file__).resolve().parents[3] / "kb" / "documents"
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_K1, _B = 1.5, 0.75


class KBError(RuntimeError):
    pass


@dataclass(frozen=True)
class KBChunk:
    source_id: str
    document_version: str
    chunk_id: str
    title: str
    section: str
    text: str
    provenance: str = "kb_local"

    def to_dict(self) -> dict:
        return {"source_id": self.source_id,
                "document_version": self.document_version,
                "chunk_id": self.chunk_id, "title": self.title,
                "section": self.section, "text": self.text,
                "provenance": self.provenance}


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


class KnowledgeBase:
    def __init__(self, chunks: list[KBChunk]):
        if not chunks:
            raise KBError("knowledge base loaded zero chunks")
        self.chunks = sorted(chunks, key=lambda c: (c.source_id, c.chunk_id))
        self._by_key = {(c.source_id, c.chunk_id): c for c in self.chunks}
        self._by_chunk_id: dict[str, list[KBChunk]] = {}
        for c in self.chunks:
            self._by_chunk_id.setdefault(c.chunk_id, []).append(c)
        # BM25 statistics
        self._doc_tokens = [_tokens(c.text + " " + c.section) for c in self.chunks]
        self._doc_len = [len(t) for t in self._doc_tokens]
        self._avg_len = sum(self._doc_len) / len(self._doc_len)
        self._tf = [Counter(t) for t in self._doc_tokens]
        df: Counter = Counter()
        for t in self._doc_tokens:
            df.update(set(t))
        n = len(self.chunks)
        self._idf = {w: math.log(1 + (n - d + 0.5) / (d + 0.5))
                     for w, d in df.items()}
        self.checksum = hashlib.sha256(
            "\n".join(f"{c.source_id}|{c.document_version}|{c.chunk_id}|"
                      f"{c.text}" for c in self.chunks).encode()).hexdigest()

    @classmethod
    def load(cls, directory: str | Path = KB_DIR) -> "KnowledgeBase":
        directory = Path(directory)
        if not directory.exists():
            raise KBError(f"knowledge directory {directory} does not exist")
        chunks: list[KBChunk] = []
        for path in sorted(directory.glob("*.md")):
            header, _, body = path.read_text().partition("\n---\n")
            meta = dict(line.split(":", 1) for line in header.splitlines()
                        if ":" in line)
            meta = {k.strip(): v.strip() for k, v in meta.items()}
            for key in ("source_id", "version", "title"):
                if key not in meta:
                    raise KBError(f"{path.name} missing '{key}' header")
            counter = 0
            for section_block in re.split(r"^## ", body, flags=re.M)[1:]:
                counter += 1
                slug, _, text = section_block.partition("\n")
                text = " ".join(text.split())
                if not text:
                    raise KBError(f"{path.name} section '{slug}' is empty")
                chunks.append(KBChunk(
                    source_id=meta["source_id"],
                    document_version=meta["version"],
                    chunk_id=f"{slug.strip()}_{counter:02d}",
                    title=meta["title"], section=slug.strip(), text=text))
        return cls(chunks)

    def get(self, source_id: str, chunk_id: str) -> KBChunk | None:
        return self._by_key.get((source_id, chunk_id))

    def chunk_owners(self, chunk_id: str) -> list[KBChunk]:
        return self._by_chunk_id.get(chunk_id, [])

    def search(self, query: str, limit: int = 3) -> list[tuple[KBChunk, float]]:
        q = _tokens(query)
        if not q:
            return []
        scored = []
        for i, chunk in enumerate(self.chunks):
            score = 0.0
            for w in q:
                if w not in self._tf[i]:
                    continue
                tf = self._tf[i][w]
                score += self._idf.get(w, 0.0) * (tf * (_K1 + 1)) / (
                    tf + _K1 * (1 - _B + _B * self._doc_len[i] / self._avg_len))
            if score > 0:
                scored.append((chunk, round(score, 6)))
        scored.sort(key=lambda t: (-t[1], t[0].chunk_id))
        return scored[:max(1, min(limit, 5))]


_KB: KnowledgeBase | None = None


def get_kb(directory: str | Path | None = None) -> KnowledgeBase:
    """Process-wide lazy singleton; tests pass an explicit directory to get
    an isolated instance."""
    global _KB
    if directory is not None:
        return KnowledgeBase.load(directory)
    if _KB is None:
        _KB = KnowledgeBase.load()
    return _KB
