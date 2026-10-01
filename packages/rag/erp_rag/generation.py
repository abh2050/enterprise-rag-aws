"""Deterministic post-generation checks: citation integrity and output policy.

These checks are authoritative. The judge can make an outcome stricter but can never override them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from erp_rag.schemas import AnswerClaim, Citation, EvidencePassage, GeneratorOutput
from erp_rag.stores.dynamo import PermissionStore
from erp_rag.text import looks_like_injection

_MAX_CLAIM_CHARS = 2000
_SECRETISH_RE = re.compile(r"(?i)(BEGIN [A-Z ]*PRIVATE KEY|aws_secret_access_key|eyJ[a-zA-Z0-9_-]{10,}\.)")


@dataclass
class ValidationReport:
    citation_violations: list[str] = field(default_factory=list)
    policy_violations: list[str] = field(default_factory=list)
    dropped_claims: int = 0

    @property
    def ok(self) -> bool:
        return not self.citation_violations and not self.policy_violations


@dataclass
class ValidatedAnswer:
    status: str
    claims: list[tuple[str, list[str]]]  # (text, evidence_ids)
    conflicts: list[str]
    abstain_reason: str | None
    report: ValidationReport


def validate_output(output: GeneratorOutput, evidence: list[EvidencePassage]) -> ValidatedAnswer:
    """Keep only claims whose citations all exist in the packed evidence and that pass output policy."""
    known = {p.evidence_id for p in evidence}
    report = ValidationReport()
    kept: list[tuple[str, list[str]]] = []
    for i, claim in enumerate(output.claims):
        unknown = [e for e in claim.evidence_ids if e not in known]
        if unknown:
            report.citation_violations.append(f"claim {i}: unknown evidence ids {unknown}")
            report.dropped_claims += 1
            continue
        text = claim.text.strip()
        if len(text) > _MAX_CLAIM_CHARS or looks_like_injection(text) or _SECRETISH_RE.search(text):
            report.policy_violations.append(f"claim {i}: output policy")
            report.dropped_claims += 1
            continue
        kept.append((text, list(dict.fromkeys(claim.evidence_ids))))

    status = output.status
    if status != "abstain" and not kept:
        status = "abstain"
    if status == "answered" and report.dropped_claims:
        status = "limited"
    return ValidatedAnswer(
        status=status,
        claims=kept,
        conflicts=[c[:500] for c in output.conflicts],
        abstain_reason=output.abstain_reason if status == "abstain" else None,
        report=report,
    )


async def resolve_citations(
    tenant_id: str,
    claims: list[tuple[str, list[str]]],
    evidence: list[EvidencePassage],
    store: PermissionStore,
    base_path: str = "/api/citations",
) -> tuple[list[AnswerClaim], list[Citation]]:
    """Resolve citation metadata server-side from authoritative records (never from model text)."""
    by_eid = {p.evidence_id: p for p in evidence}
    used = [by_eid[e] for _, eids in claims for e in eids]
    records = await store.get_many(tenant_id, [p.document_id for p in used])
    citations: dict[str, Citation] = {}
    for p in used:
        if p.chunk_id in citations:
            continue
        rec = records.get(p.document_id)
        citations[p.chunk_id] = Citation(
            citation_id=p.chunk_id,
            evidence_id=p.evidence_id,
            document_id=p.document_id,
            document_version=p.document_version,
            title=rec.title if rec else p.title,
            location=p.location,
            section=p.section,
            source_uri=rec.source_uri if rec else "",
            open_url=f"{base_path}/{p.chunk_id}",
            source_modified_at=rec.source_modified_at if rec else p.source_modified_at,
            acl_synced_at=rec.acl_synced_at if rec else None,
            is_latest_revision=p.is_latest_revision,
        )
    answer_claims = [
        AnswerClaim(text=t, citation_ids=[by_eid[e].chunk_id for e in eids]) for t, eids in claims
    ]
    return answer_claims, list(citations.values())
