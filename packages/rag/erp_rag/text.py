"""Deterministic text helpers shared by chunking, planning, fixtures and validation."""

from __future__ import annotations

import re

STOPWORDS = frozenset(
    (
        "a an and are as at be by can could did do does for from had has have how i if in into is it its "
        "may me my of on or our should so than that the their them then there these they this to was we "
        "were what when where which who whom why will with would you your about after all also any been "
        "before being between both each more most other over same some such only own very just not no nor"
    ).split()
)

_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-_.]*[A-Za-z0-9]|[A-Za-z0-9]")
_SENT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")
# Exact identifiers: ticket/invoice/policy numbers such as INV-2024-0042, POL-7, SOP-12.3, RFC 9110.
IDENTIFIER_RE = re.compile(r"\b[A-Z]{2,6}-\d[\w.\-]*\b|\b[A-Z]{2,6}\s\d{2,}\b")


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in _WORD_RE.findall(text)]


def content_terms(text: str) -> list[str]:
    return [t for t in tokenize(text) if t not in STOPWORDS and len(t) > 1]


def sentences(text: str) -> list[str]:
    parts = [s.strip() for s in _SENT_RE.split(text.replace("\n", " ")) if s.strip()]
    return parts


def extract_identifiers(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for match in IDENTIFIER_RE.findall(text):
        seen.setdefault(match.strip(), None)
    return list(seen)


def estimate_tokens(text: str) -> int:
    """Budgeting estimate: ~4 characters per token (documented assumption)."""
    return max(1, (len(text) + 3) // 4)


def overlap(a: list[str], b: list[str]) -> float:
    """Fraction of distinct terms of ``a`` that appear in ``b``."""
    sa, sb = set(a), set(b)
    if not sa:
        return 0.0
    return len(sa & sb) / len(sa)


# Patterns typical of instructions embedded in documents (prompt injection). Used to *flag* passages so
# the generator prompt and the output-policy check can treat them as hostile data. Detection is a
# defence-in-depth heuristic, not a guarantee.
INJECTION_RE = re.compile(
    r"(?i)(ignore (all |any )?(previous|prior|above) (instructions|rules)|disregard (the )?(system|previous)"
    r"|you are now|system prompt|reveal (the )?(system|hidden)|call the tool"
    r"|execute (the )?(following|command)"
    r"|override (your|the) (policy|instructions)|do not cite|act as (an? )?(admin|administrator))"
)


def looks_like_injection(text: str) -> bool:
    return INJECTION_RE.search(text) is not None
