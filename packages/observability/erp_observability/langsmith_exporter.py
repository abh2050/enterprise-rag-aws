"""LangSmith span exporter (optional; local development runs without it).

* Receives spans that were ALREADY redacted by the tracer (IDs, versions, counts, timings — no tokens,
  no document text, no questions/answers). The LangSmith client is additionally configured with
  ``hide_inputs``/``hide_outputs`` as defence in depth.
* Spans are buffered per trace until the root span completes, then emitted as one run tree with
  correct ``dotted_order``. Buffers are bounded; overflow drops whole traces and counts them.
* Runs on the tracer's background thread; failures never reach the request path.

Self-hosted LangSmith: point ``LANGSMITH_ENDPOINT`` / ``ERP_LANGSMITH_ENDPOINT`` at the deployment.
Live status: implemented-not-live-tested.
"""

from __future__ import annotations

import uuid
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any, Protocol

from erp_observability.tracing import SpanRecord

_NS = uuid.UUID("6f1c9d1e-4e6b-4f7a-9d55-2b0e3f6c1a77")


class RunSink(Protocol):
    def batch_ingest_runs(self, create: list[dict[str, Any]] | None = None, update: Any = None) -> None: ...


def _run_id(trace_id: str, span_id: str) -> str:
    return str(uuid.uuid5(_NS, f"{trace_id}:{span_id}"))


def _dotted_ts(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, tz=UTC).strftime("%Y%m%dT%H%M%S%fZ")


class LangSmithExporter:
    def __init__(
        self, client: RunSink, *, project: str, max_traces: int = 1000, max_spans_per_trace: int = 500
    ) -> None:
        self._client = client
        self._project = project
        self._max_traces = max_traces
        self._max_spans = max_spans_per_trace
        self._pending: OrderedDict[str, list[SpanRecord]] = OrderedDict()
        self.dropped_traces = 0

    def export(self, span: SpanRecord) -> None:
        spans = self._pending.setdefault(span.trace_id, [])
        if len(spans) < self._max_spans:
            spans.append(span)
        if len(self._pending) > self._max_traces:
            self._pending.popitem(last=False)
            self.dropped_traces += 1
        if span.parent_span_id is None:
            self._flush(self._pending.pop(span.trace_id, []))

    def _flush(self, spans: list[SpanRecord]) -> None:
        if not spans:
            return
        by_id = {s.span_id: s for s in spans}
        root = next(s for s in spans if s.parent_span_id is None)
        root_run = _run_id(root.trace_id, root.span_id)
        dotted: dict[str, str] = {}

        def order(s: SpanRecord) -> str:
            if s.span_id in dotted:
                return dotted[s.span_id]
            own = f"{_dotted_ts(s.start_wall_ns)}{_run_id(s.trace_id, s.span_id)}"
            parent = by_id.get(s.parent_span_id or "")
            dotted[s.span_id] = own if parent is None else f"{order(parent)}.{own}"
            return dotted[s.span_id]

        runs: list[dict[str, Any]] = []
        for s in sorted(spans, key=lambda x: (order(x).count("."), x.start_wall_ns)):
            parent = by_id.get(s.parent_span_id or "")
            end_ns = s.start_wall_ns + (s.end_ns - s.start_ns)
            runs.append(
                {
                    "id": _run_id(s.trace_id, s.span_id),
                    "trace_id": root_run,
                    "dotted_order": order(s),
                    "parent_run_id": _run_id(s.trace_id, parent.span_id) if parent else None,
                    "name": s.name,
                    "run_type": "llm" if s.name.startswith("gateway.") else "chain",
                    "inputs": {},
                    "outputs": {},
                    "start_time": datetime.fromtimestamp(s.start_wall_ns / 1e9, tz=UTC),
                    "end_time": datetime.fromtimestamp(end_ns / 1e9, tz=UTC),
                    "error": s.error_category if s.status == "error" else None,
                    "extra": {
                        "metadata": {**s.attributes, "erp_trace_id": s.trace_id, "duration_ms": s.duration_ms}
                    },
                    "session_name": self._project,
                }
            )
        self._client.batch_ingest_runs(create=runs)


def build_langsmith_exporter(endpoint: str | None, project: str) -> LangSmithExporter:
    from langsmith import Client

    client = Client(api_url=endpoint, hide_inputs=True, hide_outputs=True, auto_batch_tracing=False)
    return LangSmithExporter(client, project=project)
