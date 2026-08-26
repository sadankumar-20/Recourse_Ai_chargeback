"""Deterministic citation validator (spec §30.3 citations.py).

The final authority over representment drafts. Zero LLM imports — covered by
the existing policy-purity AST test.

Rules:
- Every [E<n>] citation must name an admitted evidence display id.
- Every FACTUAL sentence must carry at least one citation. "Factual" is
  detected deterministically: digits, currency, AWB tokens, or month names.
  (Name detection would need fuzzy matching, which this layer must not do —
  the prompt additionally forbids uncited names, and hallucinated *checkable*
  facts are what the money depends on.)
- Exemption: the "RE:" header line identifies the dispute and is not an
  evidentiary claim.
"""

from __future__ import annotations

import re

CITE_RE = re.compile(r"\[(E\d+)\]")
_FACTUAL_RE = re.compile(
    r"\d|\u20b9|\bAWB\b|"
    r"\b(January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\b", re.I)
def _split_sentences(text: str) -> list[str]:
    """Sentence boundaries, QUOTE-AWARE: a period inside "double quotes" is
    part of the quoted evidence, not a boundary of the drafter's sentence.
    Found the hard way — Hinglish admissions like "...size chhota hai. refund
    kar do please" were splitting drafts mid-quote and producing false
    uncited-fact violations."""
    sentences: list[str] = []
    buf: list[str] = []
    in_quote = False
    for ch in text:
        buf.append(ch)
        if ch == '"':
            in_quote = not in_quote
        elif not in_quote and ch in ".!?\n":
            sentences.append("".join(buf))
            buf = []
    if buf:
        sentences.append("".join(buf))
    return sentences


KB_CITE_RE = re.compile(r"\[(KB\d+)\]")


def validate_citations(draft: str, admitted_display_ids: set[str],
                       kb_display_ids: set[str] | None = None) -> list[str]:
    """Return a list of violations; empty list means the draft is valid.

    R3 (additive): [KB#] labels are valid only when present in
    kb_display_ids — the deterministically VERIFIED knowledge citations. A
    factual sentence satisfies the citation rule with an admitted [E#] or a
    verified [KB#]. With no KB provided, behavior is exactly pre-R3: any
    [KB#] is an unknown citation."""
    kb_ids = kb_display_ids or set()
    violations: list[str] = []
    for m in CITE_RE.finditer(draft):
        if m.group(1) not in admitted_display_ids:
            violations.append(
                f"unknown evidence id [{m.group(1)}] — not in the admitted set "
                f"{sorted(admitted_display_ids)}")
    for m in KB_CITE_RE.finditer(draft):
        if m.group(1) not in kb_ids:
            violations.append(
                f"unknown knowledge citation [{m.group(1)}] — not among the "
                f"verified KB citations {sorted(kb_ids)}")
    for sentence in _split_sentences(draft):
        s = sentence.strip()
        if not s or s.upper().startswith("RE:"):
            continue
        if (_FACTUAL_RE.search(s) and not CITE_RE.search(s)
                and not KB_CITE_RE.search(s)):
            violations.append(f"uncited factual sentence: {s[:80]!r}")
    return violations
