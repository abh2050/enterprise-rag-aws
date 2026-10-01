"""Model gateway policy: approved models, classification/context gates, deadlines, retries, fallback,
circuit breaker, budgets, route records."""

from __future__ import annotations

import pytest

from erp_auth.models import SensitivityLabel
from erp_rag.config import REPO_ROOT
from erp_rag.gateway.gateway import (
    BudgetExceededError,
    GatewayExhaustedError,
    ModelGateway,
    ModelRegistry,
    NoEligibleModelError,
)
from erp_rag.gateway.providers.fixture import FixtureProvider
from erp_rag.gateway.types import GenerateRequest, RouteRecord, Task
from erp_rag.schemas import EvidencePassage

REG_PATH = REPO_ROOT / "config" / "models.fixture.yaml"


def evidence() -> list[EvidencePassage]:
    return [
        EvidencePassage(
            evidence_id="E1",
            chunk_id="chk_1",
            document_id="doc_1",
            document_version="v1",
            title="Policy",
            location="p. 1",
            section="Leave",
            text="Employees receive 25 days of annual leave per year.",
            chunk_type="text",
            rerank_score=0.9,
            sensitivity_label="internal",
            source_modified_at=None,
        )
    ]


def make(**route_overrides: object) -> tuple[ModelGateway, FixtureProvider, list[RouteRecord]]:
    registry = ModelRegistry.load(REG_PATH)
    if route_overrides:
        routes = dict(registry.routes)
        routes[Task.GENERATE] = routes[Task.GENERATE].model_copy(update=route_overrides)
        registry = registry.model_copy(update={"routes": routes})
    provider = FixtureProvider()
    records: list[RouteRecord] = []

    async def sink(r: RouteRecord) -> None:
        records.append(r)

    return (
        ModelGateway(registry=registry, providers={"fixture": provider}, route_sink=sink),
        provider,
        records,
    )


REQ = GenerateRequest(question="How many days of annual leave?", evidence=evidence(), prompt_version="p1")


async def test_primary_route_recorded() -> None:
    gw, _, records = make()
    result = await gw.generate(REQ, classification=SensitivityLabel.INTERNAL, budget=gw.new_budget())
    assert result.value.status == "answered"
    r = records[-1]
    assert (r.model_key, r.route_reason, r.outcome, r.fallback_used) == (
        "fixture-generator-primary",
        "primary",
        "ok",
        False,
    )
    assert r.config_version == "models-fixture-v1" and r.usage.output_tokens > 0 and r.latency_ms >= 0


async def test_timeout_falls_back_within_approved_chain() -> None:
    gw, provider, records = make(deadline_ms=1000, attempt_timeout_ms=150, max_retries=1)
    provider.inject("fixture-generator-primary", "timeout", "timeout")
    result = await gw.generate(REQ, classification=SensitivityLabel.INTERNAL, budget=gw.new_budget())
    assert result.spec.key == "fixture-generator-fallback"
    assert records[-1].fallback_used and records[-1].route_reason.startswith("fallback_after:")
    assert provider.calls["fixture-unapproved"] == 0  # never routed to an unapproved model


async def test_retry_exhaustion_is_bounded_and_reported() -> None:
    gw, provider, records = make(deadline_ms=5000, max_retries=2)
    provider.inject("fixture-generator-primary", "throttle", "throttle", "throttle")
    provider.inject("fixture-generator-fallback", "throttle", "throttle", "throttle")
    with pytest.raises(GatewayExhaustedError) as exc:
        await gw.generate(REQ, classification=SensitivityLabel.INTERNAL, budget=gw.new_budget())
    assert exc.value.record.attempts == 6  # (max_retries + 1) per approved model, no more
    assert records[-1].outcome == "failed" and records[-1].error_category == "throttled"
    assert provider.calls["fixture-unapproved"] == 0


async def test_invalid_output_is_rejected_then_retried() -> None:
    gw, provider, _ = make()
    provider.inject("fixture-generator-primary", "invalid")
    result = await gw.generate(REQ, classification=SensitivityLabel.INTERNAL, budget=gw.new_budget())
    assert result.record.attempts == 2 and result.value.claims


async def test_non_retryable_error_skips_to_fallback() -> None:
    gw, provider, _ = make()
    provider.inject("fixture-generator-primary", "error")
    result = await gw.generate(REQ, classification=SensitivityLabel.INTERNAL, budget=gw.new_budget())
    assert result.spec.key == "fixture-generator-fallback" and result.record.attempts == 2


async def test_classification_gate_blocks_unapproved_data_class() -> None:
    gw, provider, records = make()
    with pytest.raises(NoEligibleModelError) as exc:
        await gw.generate(REQ, classification=SensitivityLabel.RESTRICTED, budget=gw.new_budget())
    assert any("classification_restricted" in r for r in exc.value.reasons)
    assert any("not_approved" in r for r in exc.value.reasons) is False  # unapproved model not even in chain
    assert sum(provider.calls.values()) == 0
    assert records[-1].route_reason == "no_eligible_model"


async def test_context_capacity_gate() -> None:
    gw, _, _ = make()
    huge = REQ.model_copy(update={"evidence": [evidence()[0].model_copy(update={"text": "x" * 400_000})]})
    with pytest.raises(NoEligibleModelError) as exc:
        await gw.generate(huge, classification=SensitivityLabel.INTERNAL, budget=gw.new_budget())
    assert all("context_capacity" in r for r in exc.value.reasons)


async def test_budget_enforced() -> None:
    gw, _, _ = make()
    budget = gw.new_budget()
    budget.max_tokens = 100
    with pytest.raises(BudgetExceededError):
        await gw.generate(REQ, classification=SensitivityLabel.INTERNAL, budget=budget)


async def test_circuit_breaker_opens_and_skips_model() -> None:
    gw, provider, _ = make(max_retries=0)
    for _ in range(5):
        provider.inject("fixture-generator-primary", "error")
        await gw.generate(REQ, classification=SensitivityLabel.INTERNAL, budget=gw.new_budget())
    calls = provider.calls["fixture-generator-primary"]
    result = await gw.generate(REQ, classification=SensitivityLabel.INTERNAL, budget=gw.new_budget())
    assert provider.calls["fixture-generator-primary"] == calls  # breaker open: primary not called
    assert "fixture-generator-primary:circuit_open" in result.record.skipped


def test_registry_rejects_mixed_embedding_versions(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Falling back to a different embedding model would query an incompatible index."""
    import yaml

    raw = yaml.safe_load(REG_PATH.read_text())
    raw["models"]["fixture-embedder-2"] = {
        "provider": "fixture",
        "model_id": "other",
        "tasks": ["embed"],
        "region": "local",
        "residency": "local",
        "max_classification": "restricted",
        "context_tokens": 8000,
        "embedding_dimension": 384,
        "embedding_version": "some-other-version",
    }
    raw["routes"]["embed"]["chain"].append("fixture-embedder-2")
    path = tmp_path / "reg.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="embedding_version"):
        ModelRegistry.load(path)


def test_registry_rejects_route_to_model_not_approved_for_task(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import yaml

    raw = yaml.safe_load(REG_PATH.read_text())
    raw["routes"]["judge"]["chain"] = ["fixture-embedder"]
    path = tmp_path / "reg.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="not approved for task"):
        ModelRegistry.load(path)


def test_bedrock_registry_is_valid_and_restricted_data_stays_in_region() -> None:
    reg = ModelRegistry.load(REPO_ROOT / "config" / "models.bedrock.yaml")
    assert reg.inference_mode == "live"
    for spec in reg.models.values():
        assert spec.region == "us-east-2"
        if spec.residency != "in_region":
            assert spec.max_classification != SensitivityLabel.RESTRICTED
    assert reg.embedding_spec.embedding_dimension == 1024
    assert not reg.models["claude-sonnet-5-us"].approved  # Anthropic agreement not accepted on the account


async def test_restricted_evidence_routes_only_to_in_region_model() -> None:
    reg = ModelRegistry.load(REPO_ROOT / "config" / "models.bedrock.yaml")
    gw = ModelGateway(registry=reg, providers={"bedrock": FixtureProvider()})  # type: ignore[dict-item]
    eligible, skipped = gw.eligible(Task.GENERATE, SensitivityLabel.RESTRICTED, 1000)
    assert [s.key for s in eligible] == ["nova-lite-in-region"]
    assert any(s.startswith("nova-pro-us:classification") for s in skipped)
    eligible, _ = gw.eligible(Task.GENERATE, SensitivityLabel.CONFIDENTIAL, 1000)
    assert [s.key for s in eligible] == ["nova-pro-us", "nova-lite-in-region"]
