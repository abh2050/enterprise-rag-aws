"""STAGE 5 GATE (part 1) — judge workflow and high-assurance release behaviour (FIXTURE models).

Fault injection simulates hallucinated claims, fabricated citations, judge failure and retry exhaustion.
"""

from __future__ import annotations

import pytest

from erp_rag.gateway.providers.fixture import FixtureProvider
from erp_rag.runtime import Core
from erp_rag.schemas import AnswerStatus, ResponseMode
from tests.integration.helpers import BOB

pytestmark = pytest.mark.integration
Q = "What are the RPO and RTO for tier-1 services?"


def fixture(core: Core) -> FixtureProvider:
    p = core.providers["fixture"]
    assert isinstance(p, FixtureProvider)
    p.faults.clear()
    return p


async def ask(core: Core, mode: ResponseMode, suffix: str):  # type: ignore[no-untyped-def]
    # Unique suffix → no answer-cache reuse between tests.
    return await core.qa.ask(BOB, f"{Q} ({suffix})", mode=mode)


async def test_high_assurance_pass(core: Core, ingested) -> None:  # type: ignore[no-untyped-def]
    fixture(core)
    resp = await ask(core, ResponseMode.HIGH_ASSURANCE, "pass")
    assert resp.status == AnswerStatus.ANSWERED and resp.judge_status == "passed"
    assert resp.judge and resp.judge.recommendation == "pass" and resp.judge.calibrated is False


async def test_unsupported_claim_triggers_single_repair(core: Core, ingested) -> None:  # type: ignore[no-untyped-def]
    p = fixture(core)
    p.inject("fixture-generator-primary", "fabricate")  # first draft hallucinates; repair is clean
    calls = p.calls["fixture-generator-primary"]
    resp = await ask(core, ResponseMode.HIGH_ASSURANCE, "repair")
    assert p.calls["fixture-generator-primary"] - calls == 2  # exactly one repair cycle
    assert resp.judge_status == "revised" and resp.status in (AnswerStatus.ANSWERED, AnswerStatus.LIMITED)
    assert all("sabbatical" not in c.text for c in resp.claims)


async def test_persistent_unsupported_claims_are_removed_after_repair(core: Core, ingested) -> None:  # type: ignore[no-untyped-def]
    p = fixture(core)
    p.inject("fixture-generator-primary", "fabricate", "fabricate")
    resp = await ask(core, ResponseMode.HIGH_ASSURANCE, "persistent")
    assert p.calls["fixture-generator-primary"] >= 2
    assert all("sabbatical" not in c.text for c in resp.claims)  # never released
    assert resp.status in (AnswerStatus.LIMITED, AnswerStatus.ABSTAINED)


async def test_judge_failure_withholds_answer_in_high_assurance(core: Core, ingested) -> None:  # type: ignore[no-untyped-def]
    p = fixture(core)
    p.inject("fixture-judge", "invalid", "invalid")  # schema-invalid verdicts until retries are exhausted
    resp = await ask(core, ResponseMode.HIGH_ASSURANCE, "judge-fail")
    assert resp.status == AnswerStatus.ABSTAINED and resp.abstain_reason == "judge_unavailable"
    assert resp.claims == [] and resp.citations == []  # nothing unjudged is released


async def test_standard_mode_does_not_block_on_judge(core: Core, ingested) -> None:  # type: ignore[no-untyped-def]
    p = fixture(core)
    p.inject("fixture-judge", "invalid", "invalid")
    resp = await ask(core, ResponseMode.STANDARD, "standard")
    assert resp.status == AnswerStatus.ANSWERED and resp.judge_status in ("skipped", "sampled_async")


async def test_fabricated_citation_is_dropped_deterministically(core: Core, ingested) -> None:  # type: ignore[no-untyped-def]
    p = fixture(core)
    p.inject("fixture-generator-primary", "bad_citation")
    resp = await ask(core, ResponseMode.STANDARD, "bad-cite")
    assert all("never provided" not in c.text for c in resp.claims)
    valid_ids = {c.citation_id for c in resp.citations}
    assert all(set(c.citation_ids) <= valid_ids for c in resp.claims)
    assert resp.status == AnswerStatus.LIMITED  # deterministic citation failure downgrades the answer


async def test_generation_retry_exhaustion_abstains(core: Core, ingested) -> None:  # type: ignore[no-untyped-def]
    p = fixture(core)
    p.inject("fixture-generator-primary", "throttle", "throttle")
    p.inject("fixture-generator-fallback", "throttle", "throttle")
    resp = await ask(core, ResponseMode.STANDARD, "exhausted")
    assert resp.status == AnswerStatus.ABSTAINED and resp.abstain_reason.startswith("model_unavailable")  # type: ignore[union-attr]


async def test_generator_timeout_falls_back(core: Core, ingested) -> None:  # type: ignore[no-untyped-def]
    p = fixture(core)
    p.inject("fixture-generator-primary", "timeout", "timeout")
    resp = await ask(core, ResponseMode.STANDARD, "timeout")
    assert resp.status == AnswerStatus.ANSWERED
    ledger = await core.tables["usage"].query(resp.trace_id, limit=50)
    gen = [r for r in ledger if r["task"] == "generate"]
    assert gen and gen[-1]["fallback_used"] and gen[-1]["model_key"] == "fixture-generator-fallback"
    assert {"route_reason", "config_version", "latency_ms", "usage", "est_cost_usd"} <= set(gen[-1])
