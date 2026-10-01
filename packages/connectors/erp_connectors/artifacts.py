"""Artifact storage for landing, quarantine and parsed outputs.

Local: filesystem under ``var/artifacts`` (no encryption/versioning/object lock — documented gap).
AWS: S3 with SSE-KMS, versioning and Object Lock legal holds (see infra/terraform/modules/storage).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Protocol


class ArtifactStore(Protocol):
    async def put(self, key: str, data: bytes, *, legal_hold: bool = False) -> None: ...
    async def get(self, key: str) -> bytes | None: ...
    async def delete(self, key: str) -> bool: ...
    async def exists(self, key: str) -> bool: ...
    async def delete_prefix(self, prefix: str) -> int: ...


def _safe_key(key: str) -> str:
    parts = [p for p in key.split("/") if p]
    if any(p in (".", "..") for p in parts):
        raise ValueError("invalid artifact key")
    return "/".join(parts)


class LocalArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._holds: set[str] = set()

    def _path(self, key: str) -> Path:
        return self.root / _safe_key(key)

    async def put(self, key: str, data: bytes, *, legal_hold: bool = False) -> None:
        path = self._path(key)

        def op() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)  # atomic within a filesystem
            if legal_hold:
                (path.parent / (path.name + ".legal-hold")).write_text("hold")

        await asyncio.to_thread(op)

    async def get(self, key: str) -> bytes | None:
        path = self._path(key)
        return await asyncio.to_thread(lambda: path.read_bytes() if path.exists() else None)

    async def exists(self, key: str) -> bool:
        return self._path(key).exists()

    async def delete(self, key: str) -> bool:
        path = self._path(key)
        if (path.parent / (path.name + ".legal-hold")).exists():
            return False  # physical retention preserved; access is blocked by the tombstone instead
        if path.exists():
            path.unlink()
            return True
        return False

    async def delete_prefix(self, prefix: str) -> int:
        base = self._path(prefix)
        if not base.exists():
            return 0
        deleted = 0
        for f in sorted(base.rglob("*")):
            if f.is_file() and not f.name.endswith(".legal-hold"):
                rel = str(f.relative_to(self.root))
                if await self.delete(rel):
                    deleted += 1
        return deleted


class S3ArtifactStore:
    """Live status: implemented-not-live-tested."""

    def __init__(self, s3_client: Any, bucket: str, kms_key_id: str | None = None) -> None:
        self._s3 = s3_client
        self._bucket = bucket
        self._kms = kms_key_id

    async def put(self, key: str, data: bytes, *, legal_hold: bool = False) -> None:
        # Always request SSE-KMS: the bucket policy denies uploads without it (bucket default key unless set).
        kwargs: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": _safe_key(key),
            "Body": data,
            "ServerSideEncryption": "aws:kms",
        }
        if self._kms:
            kwargs["SSEKMSKeyId"] = self._kms
        if legal_hold:
            kwargs["ObjectLockLegalHoldStatus"] = "ON"
        await asyncio.to_thread(self._s3.put_object, **kwargs)

    async def get(self, key: str) -> bytes | None:
        try:
            resp = await asyncio.to_thread(self._s3.get_object, Bucket=self._bucket, Key=_safe_key(key))
        except self._s3.exceptions.NoSuchKey:
            return None
        body: bytes = await asyncio.to_thread(resp["Body"].read)
        return body

    async def exists(self, key: str) -> bool:
        try:
            await asyncio.to_thread(self._s3.head_object, Bucket=self._bucket, Key=_safe_key(key))
        except Exception:
            return False
        return True

    async def delete(self, key: str) -> bool:
        try:
            hold = await asyncio.to_thread(
                self._s3.get_object_legal_hold, Bucket=self._bucket, Key=_safe_key(key)
            )
            if hold.get("LegalHold", {}).get("Status") == "ON":
                return False
        except Exception:  # noqa: S110 — no legal hold configured on this object
            pass
        await asyncio.to_thread(self._s3.delete_object, Bucket=self._bucket, Key=_safe_key(key))
        return True

    async def delete_prefix(self, prefix: str) -> int:
        paginator = self._s3.get_paginator("list_objects_v2")
        keys: list[str] = []
        for page in await asyncio.to_thread(
            lambda: list(paginator.paginate(Bucket=self._bucket, Prefix=prefix))
        ):
            keys.extend(o["Key"] for o in page.get("Contents", []))
        deleted = 0
        for k in keys:
            if await self.delete(k):
                deleted += 1
        return deleted
