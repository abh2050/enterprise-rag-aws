"""OPT-IN live Bedrock smoke tests (cost money). Run: RUN_LIVE_BEDROCK=1 make live-bedrock.

They call real us-east-2 Bedrock with SYNTHETIC text only. When not enabled they are reported as SKIPPED —
never as passed. Results must be recorded in docs/progress.md with date and outcome.
"""

from __future__ import annotations

import os

import pytest

from erp_auth.models import SensitivityLabel
from erp_rag.config import REPO_ROOT
from erp_rag.gateway.gateway import ModelGateway, ModelRegistry
from erp_rag.gateway.providers.bedrock import BedrockProvider
from erp_rag.gateway.types import GenerateRequest
from erp_rag.schemas import EvidencePassage

pytestmark = [
    pytest.mark.live_bedrock,
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE_BEDROCK") != "1", reason="live Bedrock tests are opt-in (RUN_LIVE_BEDROCK=1)"
    ),
]


@pytest.fixture(scope="module")
def gateway() -> ModelGateway:
    reg = ModelRegistry.load(REPO_ROOT / "config" / "models.bedrock.yaml")
    return ModelGateway(registry=reg, providers={"bedrock": BedrockProvider(region="us-east-2")})


async def test_live_embed(gateway: ModelGateway) -> None:
    r = await gateway.embed(
        ["synthetic smoke test"], purpose="query", classification=SensitivityLabel.INTERNAL
    )
    assert len(r.value[0]) == 1024


async def test_live_rerank(gateway: ModelGateway) -> None:
    r = await gateway.rerank(
        "annual leave days",
        ["Employees get 25 days of leave.", "The cafeteria opens at 7."],
        classification=SensitivityLabel.INTERNAL,
        budget=gateway.new_budget(),
    )
    assert r.value[0] > r.value[1]


async def test_live_generate_structured(gateway: ModelGateway) -> None:
    ev = EvidencePassage(
        evidence_id="E1",
        chunk_id="c",
        document_id="d",
        document_version="v",
        title="Leave",
        location="p. 1",
        section="Leave",
        text="Employees receive 25 days of annual leave.",
        chunk_type="text",
        rerank_score=1.0,
        sensitivity_label="internal",
        source_modified_at=None,
    )
    r = await gateway.generate(
        GenerateRequest(
            question="How many days of annual leave?", evidence=[ev], prompt_version="generator-prompt-v1"
        ),
        classification=SensitivityLabel.INTERNAL,
        budget=gateway.new_budget(),
    )
    assert r.value.claims and all(set(c.evidence_ids) <= {"E1"} for c in r.value.claims)
