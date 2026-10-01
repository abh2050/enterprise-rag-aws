"""Tracing adapter with bounded, non-blocking export.

* One trace id per request/ingestion run, propagated through ``contextvars``.
* Exporters (noop, in-memory for tests, LangSmith) receive *redacted* span records only.
* A full queue drops spans and increments a counter; exporter exceptions are swallowed and counted.
  Telemetry can therefore never block or alter the response workflow.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import queue
import secrets
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from erp_observability.redaction import redact

log = logging.getLogger("erp.tracing")

_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("erp_trace_id", default=None)
_span_stack: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar("erp_span_stack", default=())


def new_trace_id() -> str:
    return secrets.token_hex(16)


def current_trace_id() -> str:
    tid = _trace_id.get()
    if tid is None:
        tid = new_trace_id()
        _trace_id.set(tid)
    return tid


@contextlib.contextmanager
def trace_context(trace_id: str | None = None) -> Iterator[str]:
    """Bind a trace id (incoming or new) for the duration of a request/run."""
    tid = trace_id if trace_id and _valid_trace_id(trace_id) else new_trace_id()
    token = _trace_id.set(tid)
    stack_token = _span_stack.set(())
    try:
        yield tid
    finally:
        _trace_id.reset(token)
        _span_stack.reset(stack_token)


def _valid_trace_id(value: str) -> bool:
    return len(value) == 32 and all(c in "0123456789abcdef" for c in value)


@dataclass
class SpanRecord:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    start_ns: int
    end_ns: int = 0
    start_wall_ns: int = 0  # wall clock for exporters; durations use the monotonic clock
    attributes: dict[str, Any] = field(default_factory=dict)
    status: str = "ok"
    error_category: str | None = None

    @property
    def duration_ms(self) -> float:
        return (self.end_ns - self.start_ns) / 1e6


class SpanExporter(Protocol):
    def export(self, span: SpanRecord) -> None: ...


class NoopExporter:
    def export(self, span: SpanRecord) -> None:
        return None


class InMemoryExporter:
    """Test exporter that keeps redacted spans."""

    def __init__(self) -> None:
        self.spans: list[SpanRecord] = []

    def export(self, span: SpanRecord) -> None:
        self.spans.append(span)


class Span:
    def __init__(self, record: SpanRecord) -> None:
        self.record = record

    def set(self, **attributes: Any) -> None:
        self.record.attributes.update(attributes)

    def fail(self, category: str) -> None:
        self.record.status = "error"
        self.record.error_category = category


class Tracer:
    """Bounded tracer. ``export`` runs on a daemon thread, never on the request path."""

    def __init__(self, exporter: SpanExporter | None = None, max_queue: int = 2048) -> None:
        self._exporter: SpanExporter = exporter or NoopExporter()
        self._queue: queue.Queue[SpanRecord | None] = queue.Queue(maxsize=max_queue)
        self.dropped = 0
        self.export_failures = 0
        self._thread = threading.Thread(target=self._run, name="erp-span-export", daemon=True)
        self._thread.start()

    @contextlib.contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[Span]:
        stack = _span_stack.get()
        record = SpanRecord(
            trace_id=current_trace_id(),
            span_id=secrets.token_hex(8),
            parent_span_id=stack[-1] if stack else None,
            name=name,
            start_ns=time.monotonic_ns(),
            start_wall_ns=time.time_ns(),
            attributes=dict(attributes),
        )
        token = _span_stack.set((*stack, record.span_id))
        span = Span(record)
        try:
            yield span
        except BaseException as exc:
            if record.status == "ok":
                span.fail(type(exc).__name__)
            raise
        finally:
            _span_stack.reset(token)
            record.end_ns = time.monotonic_ns()
            self._enqueue(record)

    def _enqueue(self, record: SpanRecord) -> None:
        try:
            record.attributes = redact(record.attributes)
            self._queue.put_nowait(record)
        except queue.Full:
            self.dropped += 1
        except Exception:  # redaction must never break the caller
            self.dropped += 1

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            try:
                self._exporter.export(item)
            except Exception:
                self.export_failures += 1
                log.debug("span export failed", exc_info=False)
            finally:
                self._queue.task_done()

    def flush(self, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.005)


_default_tracer: Tracer | None = None


def get_tracer() -> Tracer:
    global _default_tracer
    if _default_tracer is None:
        _default_tracer = Tracer()
    return _default_tracer


def set_tracer(tracer: Tracer) -> None:
    global _default_tracer
    _default_tracer = tracer
