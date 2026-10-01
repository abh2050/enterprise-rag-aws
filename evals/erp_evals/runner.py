"""Evaluation runner.

Runs the synthetic dataset end to end against local OpenSearch + DynamoDB in an ISOLATED namespace, then
reports retrieval (Recall@k, nDCG@k), citation validity, authorization violations, abstention, latency
percentiles, cost per answer, ingestion freshness, revocation delay and scripted scenario outcomes.

Every report states the dataset id + hash, sample size, configuration versions and whether inference was
SIMULATED (fixture models) or LIVE. Fixture results say nothing about real model quality.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import secrets
import shutil
import statistics
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from erp_auth.context import ContextBuilder, EntitlementMap
from erp_auth.devidp import DevIdentityProvider, load_dev_directory
from erp_auth.directory import SyntheticDirectory
from erp_auth.models import AuthzContext
from erp_auth.policy import decide
from erp_evals import fixtures
from erp_ingestion.pipeline import IngestionPipeline
from erp_ingestion.wiring import build_pipeline
from erp_observability.tracing import trace_context
from erp_rag.config import REPO_ROOT, Settings
from erp_rag.planner import build_plan
from erp_rag.retrieval import RetrievalStats
from erp_rag.runtime import Core, build_core
from erp_rag.schemas import AnswerResponse, AnswerStatus, ResponseMode

DISCLAIMER_SIMULATED = (
    "INFERENCE SIMULATED with deterministic FIXTURE models. These numbers validate the pipeline "
    "(authorization, retrieval plumbing, citation integrity, workflow) and are NOT evidence of model quality."
)


@dataclass
class ItemResult:
    id: str
    category: str
    user: str
    status: str
    expected_titles: list[str]
    cited_titles: list[str]
    retrieved_titles: list[str]
    recall_at_5: float | None
    recall_at_10: float | None
    ndcg_at_10: float | None
    citation_valid: bool
    authorization_violation: bool
    abstain_correct: bool
    expected_cited: bool | None
    forbidden_string_hit: bool
    latency_ms: float
    cost_usd: float
    notes: list[str] = field(default_factory=list)


def ndcg(ranked: list[str], relevant: set[str], k: int) -> float:
    dcg = sum(1 / math.log2(i + 2) for i, t in enumerate(ranked[:k]) if t in relevant)
    ideal = sum(1 / math.log2(i + 2) for i in range(min(k, len(relevant))))
    return dcg / ideal if ideal else 0.0


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, math.ceil(p / 100 * len(s)) - 1))
    return s[idx]


class Evaluator:
    def __init__(self, core: Core, pipeline: IngestionPipeline, contexts: dict[str, AuthzContext]) -> None:
        self.core, self.pipeline, self.contexts = core, pipeline, contexts

    async def ranked_titles(self, ctx: AuthzContext, question: str) -> list[str]:
        """Authorized, reranked candidate documents (dedup by title) — the retrieval leg being measured."""
        qa = self.core.qa
        budget = qa.gateway.new_budget()
        plan, _ = await build_plan(
            qa.gateway,
            question=question,
            history_questions=[],
            cfg=qa.cfg,
            acronyms=qa.acronyms,
            budget=budget,
        )
        stats = RetrievalStats()
        cands = await qa.retriever.search(ctx, plan, qa.cfg, budget, stats)
        cands = await qa.retriever.recheck(ctx, cands, stats)
        cands = qa.retriever.model_admissible(cands, stats)
        ranked = (
            await qa.retriever.rerank(plan.standalone_question, cands, qa.cfg, budget, stats) if cands else []
        )
        titles: list[str] = []
        for c in ranked:
            if c.chunk.title not in titles:
                titles.append(c.chunk.title)
        return titles

    async def cost(self, trace_id: str) -> float:
        rows = await self.core.tables["usage"].query(trace_id, limit=200)
        return float(sum(r.get("est_cost_usd", 0.0) for r in rows))

    async def run_item(self, item: dict[str, Any]) -> ItemResult:
        if item.get("when_ocr") and self.core.settings.textract_enabled:
            item = {**item, **item["when_ocr"]}  # expectation depends on whether scanned pages are OCR'd
        ctx = self.contexts[item["user"]]
        expected = set(item["expected_titles"])
        t0 = time.perf_counter()
        with trace_context():
            resp: AnswerResponse = await self.core.qa.ask(
                ctx, item["question"], mode=ResponseMode(item.get("mode", "standard"))
            )
        latency = (time.perf_counter() - t0) * 1000
        ranked = await self.ranked_titles(ctx, item["question"])
        cited = sorted({c.title for c in resp.citations})
        # Citation validity: every citation is currently authorized for this user and every claim cites one.
        records = await self.core.store.get_many(ctx.tenant_id, [c.document_id for c in resp.citations])
        valid = all(
            decide(
                ctx, records.get(c.document_id), settings=self.core.policy, version=c.document_version
            ).allowed
            for c in resp.citations
        ) and all(claim.citation_ids for claim in resp.claims)
        forbidden = set(item.get("forbidden_titles", []))
        violation = bool(forbidden & set(cited)) or bool(forbidden & set(ranked)) or not valid
        abstained = resp.status == AnswerStatus.ABSTAINED
        text = " ".join(c.text for c in resp.claims).lower()
        notes = []
        if item.get("expect_outdated_flag") and not resp.outdated_sources:
            notes.append("expected outdated-revision flag missing")
        return ItemResult(
            id=item["id"],
            category=item["category"],
            user=item["user"],
            status=str(resp.status),
            expected_titles=sorted(expected),
            cited_titles=cited,
            retrieved_titles=ranked[:10],
            recall_at_5=(len(expected & set(ranked[:5])) / len(expected)) if expected else None,
            recall_at_10=(len(expected & set(ranked[:10])) / len(expected)) if expected else None,
            ndcg_at_10=ndcg(ranked, expected, 10) if expected else None,
            citation_valid=valid,
            authorization_violation=violation,
            abstain_correct=abstained == bool(item["expect_abstain"]),
            expected_cited=(expected <= set(cited)) if expected else None,
            forbidden_string_hit=any(s.lower() in text for s in item.get("forbidden_strings", [])),
            latency_ms=latency,
            cost_usd=await self.cost(resp.trace_id),
            notes=notes,
        )


async def run_scenarios(ev: Evaluator, workdir: Path) -> tuple[list[dict[str, Any]], dict[str, float]]:
    from erp_connectors.localfs import LocalFileConnector, ManualGovernanceAdapter
    from erp_rag.gateway.providers.fixture import FixtureProvider

    core, pipe = ev.core, ev.pipeline
    alice, bob = ev.contexts["alice@acme.example"], ev.contexts["bob@acme.example"]
    tenant = alice.tenant_id
    results: list[dict[str, Any]] = []
    measures: dict[str, float] = {}
    root = workdir / "scenarios"
    name = "localfs-eval-scenarios"
    term = "zq" + secrets.token_hex(3)
    folder = fixtures.write_collection(
        root,
        "c",
        {"note.md": f"# Note\n\nThe {term} allowance is 300 USD per month.\n"},
        f"tenant_id: {tenant}\nallowed_groups: [acme-finance]\nsensitivity_label: internal\n",
    )
    conn = LocalFileConnector(root, name=name)
    pipe.connectors[name], pipe.governance[name] = conn, ManualGovernanceAdapter(conn)
    t0 = time.perf_counter()
    published = (await pipe.sync(name))[0]
    measures["ingestion_freshness_ms_single_doc"] = (time.perf_counter() - t0) * 1000
    doc = published.document_id or ""
    q = f"What is the {term} allowance?"

    # s03 duplicate events
    events, _ = await conn.list_changes({})
    dup = await pipe.handle(events[0])
    results.append(
        {"id": "s03", "category": "duplicate_ingestion_events", "passed": dup.outcome == "duplicate"}
    )

    # s06 history access change (before revocation scenario mutates the doc)
    conv = "eval-" + secrets.token_hex(4)
    first = await core.qa.ask(alice, q, conversation_id=conv)
    await core.store.update_fields(
        tenant, doc, {"allowed_principals": ["group:acme-engineering"]}, bump_acl=True
    )
    turns = await core.qa.load_history(alice, conv)
    follow = await core.qa.ask(alice, "and how often is it paid?", conversation_id=conv)
    results.append(
        {
            "id": "s06",
            "category": "conversation_history_access_change",
            "passed": bool(first.citations)
            and turns[0].withheld
            and all(c.document_id != doc for c in follow.citations),
        }
    )
    await core.store.update_fields(tenant, doc, {"allowed_principals": ["group:acme-finance"]}, bump_acl=True)

    # s01 revocation delay
    before = await core.qa.ask(alice, q + " (rev)")
    t0 = time.perf_counter()
    await core.store.update_fields(tenant, doc, {"revoked": True}, bump_acl=True)
    after = await core.qa.ask(alice, q + " (rev)")
    measures["permission_revocation_delay_ms"] = (time.perf_counter() - t0) * 1000
    results.append(
        {
            "id": "s01",
            "category": "permission_revocation",
            "passed": any(c.document_id == doc for c in before.citations)
            and all(c.document_id != doc for c in after.citations),
        }
    )

    # s02 deletion
    (folder / "note.md").unlink()
    deleted = await pipe.sync(name)
    rec = await core.store.get(tenant, doc)
    results.append(
        {
            "id": "s02",
            "category": "deletion",
            "passed": [r.outcome for r in deleted] == ["deleted"]
            and rec is not None
            and rec.status == "deleted",
        }
    )

    provider = core.providers.get("fixture")
    if isinstance(provider, FixtureProvider):
        provider.faults.clear()
        provider.inject("fixture-generator-primary", "timeout", "timeout")
        with trace_context():
            r = await core.qa.ask(bob, "What is the RPO for tier-1 services? (fallback)")
        rows = await core.tables["usage"].query(r.trace_id, limit=50)
        results.append(
            {
                "id": "s04",
                "category": "model_timeout_fallback",
                "passed": r.status == AnswerStatus.ANSWERED and any(x.get("fallback_used") for x in rows),
            }
        )
        provider.faults.clear()
        provider.inject("fixture-judge", "invalid", "invalid")
        with trace_context():
            r = await core.qa.ask(
                bob, "What is the RPO for tier-1 services? (judge)", mode=ResponseMode.HIGH_ASSURANCE
            )
        results.append(
            {
                "id": "s05",
                "category": "judge_failure",
                "passed": r.status == AnswerStatus.ABSTAINED and r.abstain_reason == "judge_unavailable",
            }
        )
        provider.faults.clear()
    else:
        results += [
            {"id": s, "category": c, "passed": None, "note": "fault injection only available with fixtures"}
            for s, c in (("s04", "model_timeout_fallback"), ("s05", "judge_failure"))
        ]
    return results, measures


async def build_contexts(settings: Settings) -> dict[str, AuthzContext]:
    directory = load_dev_directory(settings.dev_directory_path)
    idp = DevIdentityProvider(
        issuer=settings.dev_idp_issuer, audience=settings.api_audience, directory=directory
    )
    builder = ContextBuilder(SyntheticDirectory(directory), EntitlementMap.load(settings.entitlements_path))
    verifier = idp.verifier()
    out: dict[str, AuthzContext] = {}
    for user in directory.users:  # full token path: issue → verify → build context
        out[user.username] = await builder.build(await verifier.verify(idp.issue_token(user.username)))
    return out


async def run(dataset: Path, out_dir: Path, provider: str) -> dict[str, Any]:
    workdir = Path(tempfile.mkdtemp(prefix="erp-eval-"))
    ns = "erpeval-" + secrets.token_hex(3)
    settings = Settings(
        environment="test",
        model_provider=provider,
        table_prefix=f"{ns}-",
        index_namespace=ns,
        artifact_root=workdir / "artifacts",
        audit_dir=workdir / "audit",
        judge_sample_rate_standard=0.0,
    )
    core = await build_core(settings)
    try:
        extra = workdir / "extra"
        access = (
            "tenant_id: 11111111-1111-4111-8111-111111111111\n"
            "allowed_groups: [acme-all-staff]\nsensitivity_label: internal\n"
        )
        fixtures.write_collection(
            extra,
            "generated",
            {
                "logistics-handbook.pdf": fixtures.pdf_with_table(),
                "warranty-scan.pdf": fixtures.scanned_pdf(),
                "security-awareness.docx": fixtures.docx_with_table(),
            },
            access + 'files:\n  logistics-handbook.pdf: {title: "Logistics Handbook"}\n'
            '  security-awareness.docx: {title: "Security Awareness Policy"}\n'
            '  warranty-scan.pdf: {title: "Warranty Terms (scanned)"}\n',
        )
        pipeline = build_pipeline(
            core,
            local_roots={"localfs-synthetic": REPO_ROOT / "data" / "synthetic", "localfs-eval-extra": extra},
        )
        t0 = time.perf_counter()
        ingest = [
            r for name in ("localfs-synthetic", "localfs-eval-extra") for r in await pipeline.sync(name)
        ]
        ingest_ms = (time.perf_counter() - t0) * 1000
        contexts = await build_contexts(settings)
        ev = Evaluator(core, pipeline, contexts)
        items = [
            json.loads(line)
            for line in (dataset / "questions.jsonl").read_text().splitlines()
            if line.strip()
        ]
        results = [await ev.run_item(item) for item in items]
        scenarios, measures = await run_scenarios(ev, workdir)
        report = summarize(dataset, items, results, scenarios, measures, core, ingest, ingest_ms)
    finally:
        await core.index.drop()
        core.db.drop_tables()
        await core.close()
        shutil.rmtree(workdir, ignore_errors=True)
    await asyncio.to_thread(out_dir.mkdir, parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base = out_dir / f"{stamp}-{dataset.name}-{report['header']['inference']}"
    base.with_suffix(".json").write_text(json.dumps(report, indent=2, default=str))
    base.with_suffix(".md").write_text(to_markdown(report))
    return report


def summarize(
    dataset: Path,
    items: list[dict[str, Any]],
    results: list[ItemResult],
    scenarios: list[dict[str, Any]],
    measures: dict[str, float],
    core: Core,
    ingest: list[Any],
    ingest_ms: float,
) -> dict[str, Any]:
    digest = hashlib.sha256(
        (dataset / "questions.jsonl").read_bytes() + (dataset / "scenarios.jsonl").read_bytes()
    )
    answerable = [r for r in results if r.expected_titles]
    abstain_expected = [r for r, i in zip(results, items, strict=True) if i["expect_abstain"]]
    abstained = [r for r in results if r.status == "abstained"]
    tp = sum(1 for r in abstained if r in abstain_expected)
    latencies = [r.latency_ms for r in results]
    mode = core.gateway.registry.inference_mode
    calibration_dir = REPO_ROOT / "evals" / "calibration" / "labels"
    has_labels = calibration_dir.exists() and any(calibration_dir.glob("*.csv"))
    return {
        "header": {
            "dataset": dataset.name,
            "dataset_sha256": digest.hexdigest(),
            "n_questions": len(results),
            "n_scenarios": len(scenarios),
            "inference": "simulated" if mode == "fixture" else "live",
            "disclaimer": DISCLAIMER_SIMULATED
            if mode == "fixture"
            else "LIVE inference; see calibration status.",
            "config_versions": {
                "retrieval": core.cfg.config_version,
                "models": core.gateway.registry.config_version,
                "rubric": core.qa.rubric.rubric_version,
                "authz_policy": core.policy.policy_version,
            },
            "generated_at": datetime.now(UTC).isoformat(),
        },
        "metrics": {
            "recall_at_5": statistics.fmean(r.recall_at_5 for r in answerable if r.recall_at_5 is not None)
            if answerable
            else None,
            "recall_at_10": statistics.fmean(r.recall_at_10 for r in answerable if r.recall_at_10 is not None)
            if answerable
            else None,
            "ndcg_at_10": statistics.fmean(r.ndcg_at_10 for r in answerable if r.ndcg_at_10 is not None)
            if answerable
            else None,
            "expected_document_cited_rate": statistics.fmean(
                1.0 if r.expected_cited else 0.0 for r in answerable
            )
            if answerable
            else None,
            "citation_validity_rate": statistics.fmean(1.0 if r.citation_valid else 0.0 for r in results),
            "authorization_violations": sum(1 for r in results if r.authorization_violation),
            "prompt_injection_string_hits": sum(1 for r in results if r.forbidden_string_hit),
            "abstention_precision": tp / len(abstained) if abstained else None,
            "abstention_recall": tp / len(abstain_expected) if abstain_expected else None,
            "abstention_accuracy": statistics.fmean(1.0 if r.abstain_correct else 0.0 for r in results),
            "latency_ms_p50": percentile(latencies, 50),
            "latency_ms_p95": percentile(latencies, 95),
            "latency_ms_p99": percentile(latencies, 99),
            "cost_usd_per_answer_mean": statistics.fmean(r.cost_usd for r in results),
            "ingestion_documents": len(ingest),
            "ingestion_outcomes": {
                o: sum(1 for r in ingest if r.outcome == o) for o in sorted({r.outcome for r in ingest})
            },
            "ingestion_total_ms": ingest_ms,
            **measures,
            "human_reviewed_groundedness": "not measured — no human-reviewed labels"
            if not has_labels
            else "see `python -m erp_evals.calibration`",
            "judge_calibration": "UNCALIBRATED",
        },
        "by_category": {
            cat: {
                "n": len(rs),
                "abstain_correct": sum(r.abstain_correct for r in rs),
                "violations": sum(r.authorization_violation for r in rs),
            }
            for cat in sorted({r.category for r in results})
            for rs in [[r for r in results if r.category == cat]]
        },
        "scenarios": scenarios,
        "items": [r.__dict__ for r in results],
    }


def to_markdown(report: dict[str, Any]) -> str:
    h, m = report["header"], report["metrics"]
    lines = [
        f"# Evaluation report — {h['dataset']} ({h['inference'].upper()})",
        "",
        f"> {h['disclaimer']}",
        "",
        f"* Dataset: `{h['dataset']}` sha256 `{h['dataset_sha256'][:16]}…`, n={h['n_questions']} questions + "
        f"{h['n_scenarios']} scenarios",
        f"* Config: `{json.dumps(h['config_versions'])}`",
        f"* Generated: {h['generated_at']}",
        "",
        "| Metric | Value |",
        "|---|---|",
    ]
    for k, v in m.items():
        lines.append(f"| {k} | {round(v, 4) if isinstance(v, float) else v} |")
    lines += ["", "## Scenarios", "", "| id | category | passed |", "|---|---|---|"]
    lines += [f"| {s['id']} | {s['category']} | {s['passed']} |" for s in report["scenarios"]]
    lines += [
        "",
        "## Items",
        "",
        "| id | category | status | expected | cited | R@10 | violation |",
        "|---|---|---|---|---|---|---|",
    ]
    for it in report["items"]:
        lines.append(
            f"| {it['id']} | {it['category']} | {it['status']} | {', '.join(it['expected_titles']) or '—'} | "
            f"{', '.join(it['cited_titles']) or '—'} | "
            f"{it['recall_at_10'] if it['recall_at_10'] is not None else '—'} | "
            f"{it['authorization_violation']} |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(prog="erp-eval")
    parser.add_argument("--dataset", type=Path, default=REPO_ROOT / "evals" / "datasets" / "synthetic_v1")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "evals" / "reports")
    parser.add_argument("--provider", choices=["fixture", "bedrock"], default="fixture")
    args = parser.parse_args()
    report = asyncio.run(run(args.dataset, args.out, args.provider))
    print(
        json.dumps(
            {"header": report["header"], "metrics": report["metrics"], "scenarios": report["scenarios"]},
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
