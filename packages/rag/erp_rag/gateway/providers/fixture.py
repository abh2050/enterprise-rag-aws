"""FIXTURE model provider — deterministic stand-ins for planner, embeddings, reranker, generator, judge.

!!! FIXTURE — NOT EVIDENCE OF MODEL QUALITY !!!
These implementations exist so the full pipeline (authorization, retrieval, citation validation, judge
workflow, fallbacks) can be tested locally without paid APIs. They are lexical heuristics. Any metric
computed with them is labeled ``inference: simulated`` and says nothing about real LLM behaviour,
including robustness to prompt injection.

Supports fault injection (timeouts, errors, invalid output) per model key for gateway tests.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import math
import re
from collections import defaultdict
from typing import Any, Literal

from erp_rag.gateway.types import (
    GenerateRequest,
    JudgeRequest,
    ModelSpec,
    PlanRequest,
    ProviderError,
    ThrottledError,
    Usage,
)
from erp_rag.text import content_terms, extract_identifiers, looks_like_injection, overlap, sentences

FIXTURE_LABEL = "FIXTURE — not evidence of model quality"
FIXTURE_EMBEDDING_DIM = 384
FIXTURE_EMBEDDING_VERSION = "fixture-hash-384-v1"

Fault = Literal["timeout", "error", "throttle", "invalid", "hang", "fabricate", "bad_citation"]

_PRONOUNS = {"it", "that", "this", "they", "those", "them", "its", "their", "he", "she"}
_COMPARE_RE = re.compile(r"(?i)\b(compare|comparison|versus|vs\.?|difference|differ|both)\b")
_SPLIT_RE = re.compile(r"(?i)\s+(?:and|vs\.?|versus)\s+|;\s*")


def _stem(term: str) -> str:
    for suffix in ("ing", "ies", "ed", "es", "s"):
        if len(term) > len(suffix) + 3 and term.endswith(suffix):
            return term[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return term


def fixture_embedding(text: str, dim: int = FIXTURE_EMBEDDING_DIM) -> list[float]:
    terms = [_stem(t) for t in content_terms(text)]
    vec = [0.0] * dim
    features: list[tuple[str, float]] = [(t, 1.0) for t in terms]
    features += [(f"{a}_{b}", 0.5) for a, b in itertools.pairwise(terms)]
    for feature, weight in features:
        digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
        idx = int.from_bytes(digest[:4], "big") % (dim - 1) + 1
        sign = 1.0 if digest[4] & 1 else -1.0
        vec[idx] += sign * weight
    vec[0] = 0.05  # bias term: cosine similarity is undefined for zero vectors
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec]


def _cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


class FixtureProvider:
    label = FIXTURE_LABEL

    def __init__(self, *, latency_s: float = 0.0) -> None:
        self.latency_s = latency_s
        self.faults: dict[str, list[Fault]] = defaultdict(list)
        self.calls: dict[str, int] = defaultdict(int)

    def inject(self, model_key: str, *faults: Fault) -> None:
        self.faults[model_key].extend(faults)

    async def _maybe_fault(self, spec: ModelSpec) -> Fault | None:
        self.calls[spec.key] += 1
        if self.latency_s:
            await asyncio.sleep(self.latency_s)
        queue = self.faults.get(spec.key)
        if not queue:
            return None
        fault = queue.pop(0)
        if fault in ("timeout", "hang"):
            await asyncio.sleep(3600)
        if fault == "error":
            raise ProviderError("injected fixture error")
        if fault == "throttle":
            raise ThrottledError("injected throttling")
        return fault

    # ---------------------------------------------------------------- tasks

    async def plan(self, spec: ModelSpec, req: PlanRequest) -> tuple[str, Usage]:
        if await self._maybe_fault(spec) == "invalid":
            return '{"standalone_question": ""}', Usage(input_tokens=10, output_tokens=5)
        question = req.question.strip()
        terms = set(content_terms(question))
        standalone = question
        if req.history_questions and (len(terms) <= 3 or set(question.lower().split()) & _PRONOUNS):
            standalone = f"{question} (regarding: {req.history_questions[-1]})"
        acronyms = {
            word: req.acronyms[word]
            for word in re.findall(r"\b[A-Z]{2,6}\b", question)
            if word in req.acronyms
        }
        identifiers = extract_identifiers(question)
        subqueries = [standalone]
        if _COMPARE_RE.search(question):
            parts = [
                p.strip(" ?.") for p in _SPLIT_RE.split(re.sub(_COMPARE_RE, "", question)) if p.strip(" ?.")
            ]
            subqueries += [p for p in parts if len(content_terms(p)) >= 1]
        subqueries = list(dict.fromkeys(subqueries))[: req.max_subqueries]
        draft = {
            "standalone_question": standalone,
            "acronym_expansions": acronyms,
            "exact_identifiers": identifiers,
            "subqueries": subqueries,
            "strategy": "lexical_first" if identifiers else "hybrid",
            "min_distinct_documents": 2 if _COMPARE_RE.search(question) else 1,
            "needs_table": bool(re.search(r"(?i)\b(table|rates)\b", question)),
        }
        return json.dumps(draft), Usage(input_tokens=len(question) // 4 + 50, output_tokens=60)

    async def embed(
        self, spec: ModelSpec, texts: list[str], purpose: Literal["query", "document"]
    ) -> tuple[list[list[float]], Usage]:
        if await self._maybe_fault(spec) == "invalid":
            return [[0.0]] * len(texts), Usage()
        dim = spec.embedding_dimension or FIXTURE_EMBEDDING_DIM
        return [fixture_embedding(t, dim) for t in texts], Usage(input_tokens=sum(len(t) for t in texts) // 4)

    async def rerank(self, spec: ModelSpec, query: str, documents: list[str]) -> tuple[list[float], Usage]:
        if await self._maybe_fault(spec) == "invalid":
            return [], Usage()
        q_terms = [_stem(t) for t in content_terms(query)]
        q_vec = fixture_embedding(query)
        scores = []
        for doc in documents:
            d_terms = [_stem(t) for t in content_terms(doc)]
            lexical = overlap(q_terms, d_terms)
            semantic = max(0.0, _cos(q_vec, fixture_embedding(doc)))
            scores.append(round(0.65 * lexical + 0.35 * semantic, 6))
        return scores, Usage(input_tokens=(len(query) + sum(len(d) for d in documents)) // 4)

    async def generate(self, spec: ModelSpec, req: GenerateRequest) -> tuple[str, Usage]:
        fault = await self._maybe_fault(spec)
        if fault == "invalid":
            return "I think the answer is probably yes.", Usage(input_tokens=100, output_tokens=10)
        q_terms = [_stem(t) for t in content_terms(req.question)]
        # Comparison questions are scored per part, simulating multi-document synthesis.
        parts = [q_terms]
        if _COMPARE_RE.search(req.question):
            split = [p for p in _SPLIT_RE.split(re.sub(_COMPARE_RE, "", req.question)) if p.strip(" ?.")]
            parts = [[_stem(t) for t in content_terms(p)] for p in split] or [q_terms]
        strict = req.repair_feedback is not None
        threshold = 0.5 if strict else 0.3
        min_shared = 1 if len(set(q_terms)) <= 2 else 2
        scored: list[tuple[float, int, str, str, str]] = []
        for order, ev in enumerate(req.evidence):
            units = ev.text.splitlines() if ev.chunk_type == "table" else sentences(ev.text)
            # Section headings give context to every sentence beneath them.
            context_terms = [_stem(t) for t in content_terms(ev.section.split(">")[-1])]
            for raw_unit in units:
                unit = raw_unit.strip()
                if len(unit) < 12 or looks_like_injection(unit):
                    continue  # never repeat or follow instruction-like text from documents
                u_terms = [_stem(t) for t in content_terms(unit)] + context_terms
                best = max(parts, key=lambda part: overlap(part, u_terms))
                shared = len(set(best) & set(u_terms))
                score = overlap(best, u_terms)
                if score >= threshold and shared >= min_shared:
                    # Prefer the latest revision when scores tie.
                    score += 0.01 if ev.is_latest_revision else 0.0
                    scored.append((score, -order, unit, ev.evidence_id, ev.document_id))
        scored.sort(reverse=True)
        claims: list[dict[str, object]] = []
        used_docs: dict[str, str] = {}
        seen_units: set[str] = set()
        for _score, _o, unit, eid, doc in scored:
            if unit in seen_units:
                continue
            seen_units.add(unit)
            claims.append({"text": unit, "evidence_ids": [eid]})
            used_docs.setdefault(doc, unit)
            if len(claims) >= 3:
                break
        if fault == "fabricate" and req.evidence:
            # Simulates a hallucinating model: an unsupported claim with a real citation id.
            claims.insert(
                0,
                {
                    "text": "Every employee is entitled to unlimited paid sabbaticals each year.",
                    "evidence_ids": [req.evidence[0].evidence_id],
                },
            )
        if fault == "bad_citation":
            claims.append(
                {"text": "A claim citing evidence that was never provided.", "evidence_ids": ["E999"]}
            )
        conflicts = _numeric_conflicts(claims)
        out: dict[str, Any]
        if not claims:
            out = {
                "status": "abstain",
                "claims": [],
                "conflicts": [],
                "abstain_reason": "insufficient_evidence",
            }
        else:
            top = scored[0][0]
            out = {
                "status": "answered" if top >= 0.5 else "limited",
                "claims": claims,
                "conflicts": conflicts,
            }
        usage = Usage(input_tokens=sum(len(e.text) for e in req.evidence) // 4 + 300, output_tokens=120)
        return json.dumps(out), usage

    async def judge(self, spec: ModelSpec, req: JudgeRequest) -> tuple[str, Usage]:
        fault = await self._maybe_fault(spec)
        if fault == "invalid":
            return '{"verdict": "looks good"}', Usage(input_tokens=100, output_tokens=5)
        by_id = {e.evidence_id: e for e in req.evidence}
        findings: list[dict[str, Any]] = []
        unsupported: list[int] = []
        mismatches: list[int] = []
        all_claim_terms: list[str] = []
        for i, claim in enumerate(req.claims):
            text = str(claim.get("text", ""))
            ids = [str(x) for x in claim.get("evidence_ids", [])]  # type: ignore[attr-defined]
            c_terms = [_stem(t) for t in content_terms(text)]
            all_claim_terms += c_terms
            cited_text = " ".join(by_id[x].text for x in ids if x in by_id)
            support = overlap(c_terms, [_stem(t) for t in content_terms(cited_text)])
            elsewhere = max(
                (
                    overlap(c_terms, [_stem(t) for t in content_terms(e.text)])
                    for e in req.evidence
                    if e.evidence_id not in ids
                ),
                default=0.0,
            )
            label = "supported" if support >= 0.9 else "partial" if support >= 0.6 else "unsupported"
            mismatch = label != "supported" and elsewhere >= 0.9
            if label == "unsupported":
                unsupported.append(i)
            if mismatch:
                mismatches.append(i)
            findings.append(
                {
                    "claim_index": i,
                    "supported": label,
                    "citation_mismatch": mismatch,
                    "note": f"term coverage {support:.2f}",
                }
            )
        q_terms = [_stem(t) for t in content_terms(req.question)]
        rel = overlap(q_terms, all_claim_terms)
        relevance = "relevant" if rel >= 0.5 else "partially_relevant" if rel >= 0.2 else "irrelevant"
        any_supported = any(f["supported"] == "supported" for f in findings)
        if not findings or not any_supported or relevance == "irrelevant":
            rec = "abstain"
        elif unsupported or mismatches or any(f["supported"] == "partial" for f in findings):
            rec = "revise"
        else:
            rec = "pass"
        verdict: dict[str, Any] = {
            "rubric_version": req.rubric_version,
            "claim_findings": findings,
            "unsupported_claims": unsupported,
            "citation_mismatches": mismatches,
            "relevance": relevance,
            "contradictions": [],
            "recommendation": rec,
            "calibrated": False,
        }
        return json.dumps(verdict), Usage(
            input_tokens=sum(len(e.text) for e in req.evidence) // 4 + 400, output_tokens=150
        )


def _numeric_conflicts(claims: list[dict[str, object]]) -> list[str]:
    """Flag claims from different evidence that share a subject but state different numbers."""
    conflicts: list[str] = []
    for i, a in enumerate(claims):
        for b in claims[i + 1 :]:
            if a["evidence_ids"] == b["evidence_ids"]:
                continue
            ta, tb = str(a["text"]), str(b["text"])
            na, nb = set(re.findall(r"\d+(?:\.\d+)?", ta)), set(re.findall(r"\d+(?:\.\d+)?", tb))
            shared = set(content_terms(re.sub(r"\d", "", ta))) & set(content_terms(re.sub(r"\d", "", tb)))
            if na and nb and na != nb and len(shared) >= 3:
                conflicts.append(f"Sources disagree: '{ta[:80]}' vs '{tb[:80]}'")
    return conflicts
