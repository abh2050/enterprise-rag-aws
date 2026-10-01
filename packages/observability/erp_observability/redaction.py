"""Redaction applied to every telemetry/log payload before it leaves the process.

Policy: credentials and tokens are always removed; raw document or answer text is removed unless the
caller explicitly marks a field as safe. IDs, versions, counts and timings pass through.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

REDACTED = "[REDACTED]"

# Keys whose values are always dropped, matched case-insensitively as substrings.
_SECRET_KEY_PARTS = (
    "authorization",
    "token",
    "secret",
    "password",
    "api_key",
    "apikey",
    "credential",
    "cookie",
    "private_key",
    "client_assertion",
)
# Keys that carry user or document content; dropped by default.
_CONTENT_KEYS = frozenset(
    {
        "text",
        "content",
        "question",
        "query",
        "answer",
        "evidence",
        "prompt",
        "completion",
        "passage",
        "passages",
        "body",
        "messages",
        "claims",
        "rewritten_question",
        "subqueries",
        "standalone_question",
        "original_question",
    }
)
_BEARER_RE = re.compile(r"(?i)bearer\s+[a-z0-9\-._~+/]+=*")
_JWT_RE = re.compile(r"eyJ[a-zA-Z0-9_-]{5,}\.[a-zA-Z0-9_-]{5,}\.[a-zA-Z0-9_-]{5,}")
_AWS_KEY_RE = re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b")
_MAX_STR = 256


def _scrub_string(value: str) -> str:
    value = _BEARER_RE.sub("Bearer " + REDACTED, value)
    value = _JWT_RE.sub(REDACTED, value)
    value = _AWS_KEY_RE.sub(REDACTED, value)
    if len(value) > _MAX_STR:
        return value[:_MAX_STR] + "…[truncated]"
    return value


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SECRET_KEY_PARTS)


def redact(value: Any, *, allow_content_keys: frozenset[str] = frozenset(), _depth: int = 0) -> Any:
    """Return a redacted deep copy of ``value`` safe for export."""
    if _depth > 8:
        return REDACTED
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if _is_secret_key(key):
                out[key] = REDACTED
            elif key.lower() in _CONTENT_KEYS and key not in allow_content_keys:
                out[key] = _content_summary(item)
            else:
                out[key] = redact(item, allow_content_keys=allow_content_keys, _depth=_depth + 1)
        return out
    if isinstance(value, list | tuple | set | frozenset):
        return [redact(v, allow_content_keys=allow_content_keys, _depth=_depth + 1) for v in value][:200]
    if isinstance(value, str):
        return _scrub_string(value)
    if isinstance(value, int | float | bool) or value is None:
        return value
    return _scrub_string(repr(value))


def _content_summary(value: Any) -> str:
    """Describe content without revealing it."""
    if isinstance(value, str):
        return f"{REDACTED} (chars={len(value)})"
    if isinstance(value, list | tuple):
        return f"{REDACTED} (items={len(value)})"
    return REDACTED
