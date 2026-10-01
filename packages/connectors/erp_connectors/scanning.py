"""Malware scanning integrations. Anything other than a definitive "clean" quarantines the file.

* ``DevSignatureScanner`` — SIMULATED for local testing: detects only the EICAR test signature.
  Production startup refuses it.
* ``ClamdScanner`` — ClamAV daemon (sidecar container) over TCP using the INSTREAM protocol
  (``zINSTREAM\\0`` + 4-byte big-endian length-prefixed chunks + zero-length terminator). Used in AWS where
  GuardDuty Malware Protection is denied by organization policy. Signature-based only.
* ``GuardDutyS3TagScanner`` — reads the ``GuardDutyMalwareScanStatus`` object tag written by GuardDuty
  Malware Protection for S3 (values verified 2026-09-30: NO_THREATS_FOUND, THREATS_FOUND, UNSUPPORTED,
  ACCESS_DENIED, FAILED). Live status: implemented-not-live-tested.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


class ScanResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["clean", "infected", "unsupported", "error", "timeout"]
    engine: str
    detail: str | None = None

    @property
    def clean(self) -> bool:
        return self.status == "clean"


class MalwareScanner(Protocol):
    engine: str

    async def scan(self, data: bytes, *, artifact_key: str) -> ScanResult: ...


class DevSignatureScanner:
    engine = "dev-signature (SIMULATED)"

    async def scan(self, data: bytes, *, artifact_key: str) -> ScanResult:
        if EICAR in data:
            return ScanResult(status="infected", engine=self.engine, detail="EICAR test signature")
        return ScanResult(status="clean", engine=self.engine)


_GD_MAP: dict[str, Literal["clean", "infected", "unsupported", "error"]] = {
    "NO_THREATS_FOUND": "clean",
    "THREATS_FOUND": "infected",
    "UNSUPPORTED": "unsupported",
    "ACCESS_DENIED": "error",
    "FAILED": "error",
}


class GuardDutyS3TagScanner:
    engine = "guardduty-malware-protection-s3"

    def __init__(self, s3_client: Any, bucket: str, *, timeout_s: float = 300, poll_s: float = 5) -> None:
        self._s3 = s3_client
        self._bucket = bucket
        self._timeout = timeout_s
        self._poll = poll_s

    async def scan(self, data: bytes, *, artifact_key: str) -> ScanResult:
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            resp = await asyncio.to_thread(self._s3.get_object_tagging, Bucket=self._bucket, Key=artifact_key)
            tags = {t["Key"]: t["Value"] for t in resp.get("TagSet", [])}
            value = tags.get("GuardDutyMalwareScanStatus")
            if value is not None:
                return ScanResult(status=_GD_MAP.get(value, "error"), engine=self.engine, detail=value)
            await asyncio.sleep(self._poll)
        return ScanResult(status="timeout", engine=self.engine, detail="no scan result tag before timeout")


class ClamdScanner:
    engine = "clamav-clamd"
    CHUNK = 64 * 1024

    def __init__(self, host: str = "127.0.0.1", port: int = 3310, *, timeout_s: float = 120.0) -> None:
        self._host, self._port, self._timeout = host, port, timeout_s

    async def scan(self, data: bytes, *, artifact_key: str) -> ScanResult:
        try:
            async with asyncio.timeout(self._timeout):
                reader, writer = await asyncio.open_connection(self._host, self._port)
                try:
                    writer.write(b"zINSTREAM\0")
                    for i in range(0, len(data), self.CHUNK):
                        chunk = data[i : i + self.CHUNK]
                        writer.write(len(chunk).to_bytes(4, "big") + chunk)
                        await writer.drain()
                    writer.write((0).to_bytes(4, "big"))
                    await writer.drain()
                    reply = (await reader.read(4096)).rstrip(b"\0\n").decode("utf-8", errors="replace")
                finally:
                    writer.close()
                    with contextlib.suppress(Exception):
                        await writer.wait_closed()
        except TimeoutError:
            return ScanResult(status="timeout", engine=self.engine, detail="clamd did not answer in time")
        except OSError as exc:
            return ScanResult(
                status="error", engine=self.engine, detail=f"clamd unreachable: {type(exc).__name__}"
            )
        if reply.endswith(" OK"):
            return ScanResult(status="clean", engine=self.engine)
        if reply.endswith(" FOUND"):
            signature = reply.removeprefix("stream: ").removesuffix(" FOUND")
            return ScanResult(status="infected", engine=self.engine, detail=signature[:200])
        # e.g. "INSTREAM size limit exceeded. ERROR" → never treated as clean
        return ScanResult(status="error", engine=self.engine, detail=reply[:200] or "empty reply")
