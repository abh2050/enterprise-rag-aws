"""LLM-as-a-judge: separate interface, versioned rubric, schema-validated verdicts.

The judge can only make outcomes stricter. It cannot override deterministic authorization, citation
integrity or output-policy failures (those are applied before the judge ever runs). Verdicts are
categorical and explicitly uncalibrated — never presented as probabilities of correctness.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict

from erp_auth.models import SensitivityLabel
from erp_rag.gateway.gateway import (
    BudgetExceededError,
    GatewayExhaustedError,
    ModelGateway,
    NoEligibleModelError,
    RequestBudget,
)
from erp_rag.gateway.types import JudgeRequest
from erp_rag.schemas import EvidencePassage, JudgeVerdict


class Rubric(BaseModel):
    model_config = ConfigDict(frozen=True)

    rubric_version: str
    instructions: str

    @classmethod
    def load(cls, path: Path) -> Rubric:
        return cls.model_validate(yaml.safe_load(path.read_text()))


@dataclass(frozen=True)
class JudgeOutcome:
    verdict: JudgeVerdict | None
    status: Literal["pass", "revise", "abstain", "failed"]
    failure: str | None = None


def _consistent(verdict: JudgeVerdict, n_claims: int, rubric_version: str) -> bool:
    if verdict.rubric_version != rubric_version:
        return False
    indices = sorted(f.claim_index for f in verdict.claim_findings)
    if indices != list(range(n_claims)):
        return False
    return all(0 <= i < n_claims for i in [*verdict.unsupported_claims, *verdict.citation_mismatches])


async def run_judge(
    gateway: ModelGateway,
    rubric: Rubric,
    *,
    question: str,
    claims: list[tuple[str, list[str]]],
    evidence: list[EvidencePassage],
    classification: SensitivityLabel,
    budget: RequestBudget,
) -> JudgeOutcome:
    req = JudgeRequest(
        question=question,
        claims=[{"index": i, "text": t, "evidence_ids": e} for i, (t, e) in enumerate(claims)],
        evidence=evidence,
        rubric_version=rubric.rubric_version,
        rubric_text=rubric.instructions,
    )
    try:
        result = await gateway.judge(req, classification=classification, budget=budget)
    except (GatewayExhaustedError, NoEligibleModelError, BudgetExceededError) as exc:
        return JudgeOutcome(verdict=None, status="failed", failure=type(exc).__name__)
    verdict = result.value
    if not _consistent(verdict, len(claims), rubric.rubric_version):
        return JudgeOutcome(verdict=None, status="failed", failure="inconsistent_verdict")
    if verdict.calibrated:
        # Calibration is a property of this deployment's evidence, not something a model can assert.
        verdict = verdict.model_copy(update={"calibrated": False})
    return JudgeOutcome(verdict=verdict, status=verdict.recommendation)


def supported_subset(
    claims: list[tuple[str, list[str]]], verdict: JudgeVerdict
) -> list[tuple[str, list[str]]]:
    """Claims the judge marked fully supported with matching citations."""
    ok = {
        f.claim_index
        for f in verdict.claim_findings
        if f.supported == "supported" and not f.citation_mismatch
    }
    return [c for i, c in enumerate(claims) if i in ok]


def repair_feedback(verdict: JudgeVerdict) -> str:
    lines = []
    for f in verdict.claim_findings:
        if f.supported != "supported" or f.citation_mismatch:
            lines.append(
                f"- claim {f.claim_index}: {f.supported}"
                + (" (citation mismatch)" if f.citation_mismatch else "")
            )
    for c in verdict.contradictions:
        lines.append(f"- contradiction: {c[:200]}")
    return "Remove or correct these claims; cite only directly supporting evidence:\n" + "\n".join(lines)
