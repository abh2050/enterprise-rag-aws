"""Security audit trail, separate from operational logs.

Events record *who* did *what* to *which* resource and the authorization decision. They never contain
document text, tokens or answers. Locally they go to ``var/audit/audit-YYYYMMDD.jsonl``; on AWS the
``erp.audit`` logger is shipped to a dedicated CloudWatch log group with its own KMS key and IAM policy.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from erp_observability.redaction import redact
from erp_observability.tracing import current_trace_id

_audit_logger = logging.getLogger("erp.audit")


class AuditSink(Protocol):
    def write(self, event: dict[str, Any]) -> None: ...


class LoggerSink:
    """Writes JSON lines to the ``erp.audit`` logger (CloudWatch in AWS)."""

    def write(self, event: dict[str, Any]) -> None:
        _audit_logger.info(json.dumps(event, sort_keys=True))


class FileSink:
    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self._lock = threading.Lock()

    def write(self, event: dict[str, Any]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"audit-{datetime.now(UTC):%Y%m%d}.jsonl"
        line = json.dumps(event, sort_keys=True)
        with self._lock, path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


class CloudWatchLogsSink:
    """Writes audit events to a dedicated CloudWatch Logs group (separate KMS key + IAM in AWS).

    Events are queued and shipped by a background thread so audit I/O never blocks requests. The queue is
    bounded; overflow is counted and logged as an operational error (alarm on it).
    """

    def __init__(self, log_group: str, *, region: str, client: Any = None, max_queue: int = 10000) -> None:
        import queue
        import socket

        import boto3

        self._client = client or boto3.client("logs", region_name=region)
        self._group = log_group
        self._stream = f"{socket.gethostname()}-{datetime.now(UTC):%Y%m%dT%H%M%S}"
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=max_queue)
        self.dropped = 0
        self._client.create_log_stream(logGroupName=self._group, logStreamName=self._stream)
        threading.Thread(target=self._run, name="erp-audit-ship", daemon=True).start()

    def write(self, event: dict[str, Any]) -> None:
        import queue

        try:
            self._queue.put_nowait(event)
        except queue.Full:
            self.dropped += 1
            logging.getLogger("erp.audit.errors").error("audit queue full; event dropped")

    def _run(self) -> None:
        import queue
        import time

        while True:
            batch = [self._queue.get()]
            deadline = time.monotonic() + 1.0
            while len(batch) < 500 and time.monotonic() < deadline:
                try:
                    batch.append(self._queue.get(timeout=max(0.0, deadline - time.monotonic())))
                except queue.Empty:
                    break
            events = [
                {"timestamp": int(time.time() * 1000), "message": json.dumps(e, sort_keys=True)}
                for e in batch
            ]
            try:
                self._client.put_log_events(
                    logGroupName=self._group, logStreamName=self._stream, logEvents=events
                )
            except Exception:
                logging.getLogger("erp.audit.errors").exception(
                    "audit shipping failed (%d events)", len(events)
                )


class MemorySink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def write(self, event: dict[str, Any]) -> None:
        self.events.append(event)


class AuditLog:
    def __init__(self, sinks: list[AuditSink]) -> None:
        self._sinks = sinks

    def record(
        self,
        action: str,
        *,
        actor: str | None,
        tenant_id: str | None,
        outcome: str,
        resource: str | None = None,
        reason: str | None = None,
        **details: Any,
    ) -> None:
        event = {
            "ts": datetime.now(UTC).isoformat(),
            "trace_id": current_trace_id(),
            "action": action,
            "actor": actor,
            "tenant_id": tenant_id,
            "resource": resource,
            "outcome": outcome,
            "reason": reason,
            "details": redact(details),
        }
        for sink in self._sinks:
            try:
                sink.write(event)
            except Exception:  # audit sink failures are surfaced via the operational log
                logging.getLogger("erp.audit.errors").exception("audit sink failed")
