import threading
import time

from erp_observability.audit import AuditLog, MemorySink
from erp_observability.langsmith_exporter import LangSmithExporter
from erp_observability.redaction import REDACTED, redact
from erp_observability.tracing import InMemoryExporter, Tracer, current_trace_id, trace_context

JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlLXZhbHVl"


def test_redaction_removes_secrets_and_content() -> None:
    out = redact(
        {
            "Authorization": "Bearer abc",
            "api_key": "k",
            "client_secret": "s",
            "nested": {"password": "p"},
            "question": "What is Alice's salary?",
            "text": "secret document body",
            "claims": ["a", "b"],
            "document_id": "doc_1",
            # gitleaks:allow (fake test key)
            "note": f"token {JWT} and AKIAABCDEFGHIJKLMNOP and Bearer xyz.abc",  # gitleaks:allow
            "count": 3,
            "long": "x" * 1000,
        }
    )
    assert (
        out["Authorization"] == REDACTED
        and out["api_key"] == REDACTED
        and out["nested"]["password"] == REDACTED
    )
    assert "salary" not in str(out) and "document body" not in str(out)
    assert out["question"].startswith(REDACTED) and out["claims"] == f"{REDACTED} (items=2)"
    assert out["document_id"] == "doc_1" and out["count"] == 3
    assert JWT not in out["note"] and "AKIA" not in out["note"] and "xyz.abc" not in out["note"]
    assert len(out["long"]) < 300


def test_spans_share_trace_and_are_redacted() -> None:
    exp = InMemoryExporter()
    tracer = Tracer(exp)
    with (
        trace_context() as tid,
        tracer.span("outer", question="private question"),
        tracer.span("inner", document_id="doc_1"),
    ):
        assert current_trace_id() == tid
    tracer.flush()
    assert {s.trace_id for s in exp.spans} == {tid}
    inner = next(s for s in exp.spans if s.name == "inner")
    outer = next(s for s in exp.spans if s.name == "outer")
    assert inner.parent_span_id == outer.span_id
    assert "private" not in str(outer.attributes)


def test_telemetry_failures_never_block_or_raise() -> None:
    class Exploding:
        def export(self, span):  # type: ignore[no-untyped-def]
            raise RuntimeError("collector down")

    tracer = Tracer(Exploding())
    with tracer.span("work"):
        pass
    tracer.flush()
    assert tracer.export_failures == 1

    class Slow:
        def __init__(self) -> None:
            self.gate = threading.Event()

        def export(self, span):  # type: ignore[no-untyped-def]
            self.gate.wait(2)

    slow = Slow()
    bounded = Tracer(slow, max_queue=5)
    start = time.perf_counter()
    for _ in range(50):
        with bounded.span("burst"):
            pass
    assert time.perf_counter() - start < 0.5  # request path never waits on telemetry
    assert bounded.dropped >= 40
    slow.gate.set()


def test_langsmith_exporter_builds_run_tree_without_content() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.runs: list[dict] = []

        def batch_ingest_runs(self, create=None, update=None):  # type: ignore[no-untyped-def]
            self.runs.extend(create or [])

    client = FakeClient()
    tracer = Tracer(LangSmithExporter(client, project="p"))
    with (
        trace_context(),
        tracer.span("qa.ask", question="top secret question"),
        tracer.span("gateway.generate", model="m", cost_usd=0.01),
    ):
        pass
    tracer.flush()
    assert [r["name"] for r in client.runs] == ["qa.ask", "gateway.generate"]
    root, child = client.runs
    assert child["parent_run_id"] == root["id"] and child["trace_id"] == root["id"]
    assert child["dotted_order"].startswith(root["dotted_order"] + ".")
    assert child["run_type"] == "llm" and root["inputs"] == {} and root["outputs"] == {}
    assert "top secret" not in str(client.runs)


def test_audit_events_are_redacted() -> None:
    sink = MemorySink()
    AuditLog([sink]).record(
        "citation.open",
        actor="u",
        tenant_id="t",
        outcome="denied",
        resource="chk_1",
        text="sensitive passage",
        token="eyJx",
    )
    event = sink.events[0]
    assert event["details"]["token"] == REDACTED and "sensitive passage" not in str(event)
    assert event["trace_id"]
