"""Deterministic identifiers. Same inputs always produce the same IDs, which makes ingestion idempotent."""

from __future__ import annotations

import hashlib


def _h(*parts: str, length: int) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return digest[:length]


def document_id(tenant_id: str, source: str, source_key: str) -> str:
    return "doc_" + _h(tenant_id, source, source_key, length=32)


def document_version(source_revision: str, content_checksum: str) -> str:
    return "v_" + _h(source_revision, content_checksum, length=16)


def chunk_id(doc_id: str, version: str, extraction_version: str, ordinal: int, content_checksum: str) -> str:
    return "chk_" + _h(doc_id, version, extraction_version, str(ordinal), content_checksum, length=32)


def content_checksum(data: bytes | str) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


def event_id(*parts: str) -> str:
    return "evt_" + _h(*parts, length=32)
