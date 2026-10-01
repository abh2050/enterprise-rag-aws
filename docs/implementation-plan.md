# Plan: enterprise-rag-platform (Stage 0 → Stage 6)

## Context
`/Users/abhishekshah/Desktop/enterprise_rag` is **empty and not a git repo** — greenfield. Goal: a working, tested, permission-aware enterprise RAG app that runs locally without cloud credentials, plus Terraform/CI for AWS (generated, **not deployed**). Entra ID, Purview, SharePoint and Bedrock adapters will be implemented against verified docs but are **not live-testable here** (no credentials) and will be reported as such.

Local toolchain found: Python 3.12 (`~/.local/bin/python3.12`), uv 0.6.14, Node 25 / npm, Docker 29 (8 GB), gh. **No terraform** → run `hashicorp/terraform:<pinned>` via Docker (no host install).

## AWS target (decided)
- **Account/region:** profile `awsnew`, **us-west-2**. Credentials currently absent → you run `aws login`/`aws sso login --profile awsnew` before Stage 7.
- **Footprint:** minimal dev, **core RAG stack only**. Self-hosted LangSmith Terraform is written + validated but **not applied** (license + EKS cost); tracing adapter stays noop/LangSmith-cloud-optional in dev.
- **Gate:** I run `terraform plan`, show you resources + cost estimate, and **apply only on your explicit yes**. Same for every later apply/destroy.

### Component → AWS service map
| Concern | AWS service (dev sizing) |
|---|---|
| Web UI | S3 (private, KMS) + CloudFront with OAC, TLS |
| Ingress | ALB (HTTPS, ACM cert) + AWS WAF managed rule groups + rate limit |
| API + ingestion workers | ECS Fargate (1 api task, 1 worker task), images in ECR (scan on push, immutable tags) |
| Document artifacts | S3 landing / quarantine / parsed buckets, SSE-KMS, versioning, Object Lock (legal hold), Block Public Access |
| Ingestion orchestration | S3 events → EventBridge → SQS (+DLQ) → Step Functions Standard → `sqs:sendMessage.waitForTaskToken` to Fargate workers; EventBridge Scheduler for reconciliation |
| Search | Amazon OpenSearch Service, VPC-only, 1 node single-AZ, FGAC + IAM SigV4, encryption at rest + node-to-node, pinned engine version |
| Authoritative state | DynamoDB (manifests, ACL versions, revocations, workflow checkpoints, answer cache w/ TTL, conversations, config, usage ledger) with PITR + KMS |
| Models | Amazon Bedrock in us-west-2 via VPC interface endpoint: Converse (plan/generate/judge), embeddings, Bedrock Rerank — exact model IDs confirmed against docs + `list-foundation-models` (read-only) before pinning in the registry |
| OCR | Amazon Textract (async) for scanned pages |
| Malware scan | GuardDuty Malware Protection for S3 on landing bucket (verify availability/cost); quarantine on non-clean/timeout |
| Secrets/keys | Secrets Manager (Entra client secret, Graph creds), customer-managed KMS keys per data class |
| Network | VPC, 2 AZ private subnets, 1 NAT (Microsoft egress via SG + documented Network Firewall upgrade path), gateway endpoints (S3, DynamoDB), interface endpoints (Bedrock, ECR, Logs, Secrets Manager, SQS, STS, Step Functions, Textract) |
| Observability/audit | CloudWatch Logs (separate KMS-restricted audit log group), ADOT/OTel sidecar → X-Ray, CloudTrail (+data events on doc bucket), alarms |
| CI/CD | GitHub Actions → GitHub OIDC provider → scoped deploy role (no long-lived keys) |
| State | S3 backend (versioned, KMS) + DynamoDB/S3 lockfile, separate per env |

Request path (AWS): CloudFront(SPA) → browser gets Entra token (MSAL) → WAF → ALB → API task → [Entra JWKS via NAT] → DynamoDB authz context/ACLs → OpenSearch BM25 + k-NN (filtered) → DynamoDB recheck → Bedrock rerank/generate/judge via VPC endpoint → validated answer.
Ingestion path (AWS): source change (S3 event / Graph delta poll via NAT) → EventBridge → SQS → Step Functions → workers (scan, Textract, parse, chunk, Bedrock embed) → OpenSearch staged → DynamoDB manifest flip → published.

## Ground rules applied throughout
- `git init` locally; **no commits/pushes** unless you ask. No Azure calls. AWS: read-only calls only (e.g. `sts get-caller-identity`, `bedrock list-foundation-models`) until the Stage 7 apply approval.
- Verify APIs (OpenSearch k-NN filtering, Bedrock Converse/embeddings/Rerank model+region availability, Entra v2 token claims & group overage, Graph delta/permissions/sensitivity labels, Purview Data Map APIs, LangSmith self-hosted AWS tooling, GuardDuty Malware Protection for S3) with WebFetch against official docs; log URL + access date (2026-09-30) in `docs/sources.md`.
- Integration status vocabulary used everywhere: *verified* / *implemented-not-live-tested* / *simulated-local* / *blocked*.
- Fixture models are labeled `FIXTURE — not evidence of model quality` in code, outputs, and eval reports.

## Repository layout (uv workspace + npm)
```
apps/api            FastAPI app (routes, deps, startup config checks)
apps/web            Vite + React + TS (MSAL for Entra, dev-login for local)
services/ingestion  worker: connector polling, pipeline steps, orchestrator
packages/auth       token validation (Entra + dev IdP), directory adapter, AuthzContext, policy engine
packages/rag        schemas, state machine, planner, retrieval, fusion, rerank, packing, generation, citations, judge, model gateway
packages/connectors local-file, S3, SharePoint(Graph), Purview governance adapter, parsers, chunker
packages/observability  OTel setup, tracing adapter (noop/LangSmith), redaction, audit log
infra/terraform     modules/* + envs/{dev,staging,prod}; infra/langsmith separate root
evals               datasets (synthetic, labeled), runner, metrics, calibration
tests               unit / integration (real OpenSearch + DynamoDB Local) / e2e / cloud (opt-in, marked)
scripts, docs, docker-compose.yml, .env.example, Makefile
```
Python 3.12, deps locked in `uv.lock`; `package-lock.json` for web. Lint/type: ruff, mypy --strict on packages; eslint + tsc + vitest.

## Key design decisions
**Local stack (docker compose):** OpenSearch (version pinned to one also offered by Amazon OpenSearch Service — confirmed during Stage 0), DynamoDB Local (real DynamoDB API → same boto3 adapter local & AWS), filesystem artifact store behind `ArtifactStore` (S3 adapter for cloud), api, worker, web, dev-IdP. Gaps (OpenSearch security plugin/IAM, S3 object lock, SQS/Step Functions semantics, KMS) documented in `docs/local-vs-aws.md` with marked cloud tests.

**Authn:** `TokenVerifier` protocol. `EntraVerifier`: PyJWT + JWKS cache with refresh-on-unknown-`kid` (rotation), RS256 only, `iss` = v2 issuer for allowed `tid`, `aud`, `exp/nbf/iat` with skew, required `scp`/`roles`, rejects ID tokens (`aud`≠API, missing `scp`/`roles`). Group overage (`_claim_names`/`hasgroups`) → `DirectoryAdapter` (Graph `transitiveMemberOf`; local synthetic directory). `DevIdP`: separate issuer, signs tokens for synthetic users/groups across 2 tenants; still goes through full validation — not a bypass. **Production startup fails** if dev IdP, fixture models, dev scanner, or disabled security flags are configured.

**Authz:** `AuthzContext` built only server-side (tenant, user + group principals, clearance, projects). Policy engine `decide(ctx, DocPermissionRecord) → Allow|Deny(reason)`; deny on: tenant mismatch, no principal intersection, explicit deny hit, project restriction, label above clearance/unknown label, ACL stale beyond `max_acl_age`, missing/ambiguous permissions, doc tombstoned/revoked, version not current. Authoritative records in DynamoDB (`doc_manifest`, `acl` with `acl_version`, `revocations`) — independent of OpenSearch.
Enforcement points: OpenSearch BM25 & k-NN both carry the same mandatory filter (tenant, allowed principals, must_not denied, permitted labels, `publication_status=published`); post-fusion **batch recheck against DynamoDB** before rerank/generate/judge; neighbor/parent chunk fetch; answer cache (keyed by principal-set hash, evidence rechecked on read); conversation history (prior turns store cited doc/version; turns whose evidence is no longer authorized are dropped/redacted before reuse); citation open/download endpoint.
**Fast revocation:** write revocation/ACL bump to DynamoDB → effective on next request via recheck; cache + history invalidated by version check; index cleanup (update_by_query/delete) runs async.

**RAG workflow:** explicit enum state machine with a transition table: `RECEIVED→PLANNED→RETRIEVED→AUTHORIZED→RERANKED→PACKED→SUFFICIENCY→GENERATED→CITATIONS_VALIDATED→JUDGED→(REPAIR ≤1)→RELEASED|ABSTAINED|FAILED`; typed Pydantic I/O per state; checkpoint per transition to DynamoDB; bounded retries from config. LLMs never choose transitions.
- Planner → `RetrievalPlan` (original, standalone rewrite, acronyms, exact identifiers, suggested constraints, ≤N subqueries, strategy, evidence requirements); suggested filters validated in code and can only **narrow**.
- Retrieval: BM25 top 50 + k-NN top 50 per subquery (OpenSearch efficient filtered k-NN — engine choice verified), app-side RRF (k=60), dedupe, recheck, rerank ≤40, pack ≤10 passages under token budget with doc-diversity, table headers kept, conflicting revisions flagged; sufficiency check; ≤1 retrieval retry. All defaults in versioned config, documented as unvalidated.
- Generation: evidence wrapped as untrusted data with delimiters; structured output `{claims:[{text, citation_ids}], answer_status}`; citation IDs validated against packed evidence; URL/title/page/revision resolved server-side. Modes: standard (validated answer, async judge sampling) and high-assurance (buffer until validation + sync judge pass). Prompt-injection tests.
- Judge: separate interface + versioned rubric; outputs claim support, unsupported claims, citation mismatches, relevance, contradictions, pass/revise/abstain; schema-validated; cannot override deterministic failures; uncalibrated scores never presented as probabilities.

**Model gateway:** single `ModelGateway` with typed tasks (plan, embed, rerank, generate, judge). Versioned YAML registry: approved model IDs, region, max data classification, context window, modalities, price. Enforces deadline (`asyncio.timeout`), token/cost budgets, per-model semaphore, retry w/ jittered backoff, circuit breaker, fallback only within approved chain. Persists route reason, model, config version, usage, latency, fallback outcome. Providers: `fixture` (deterministic hashing embeddings, extractive generator, lexical judge/reranker) and `bedrock` (Converse, InvokeModel embeddings, Bedrock Rerank — availability verified). Index name embeds embedding version; mismatch = refuse query, reindex required.

**Ingestion:** connector protocol `list_changes(cursor)`, `fetch(ref)`, `get_permissions(ref)`. Local-file connector (sidecar `.acl.json`/`.meta.json`), S3 connector, SharePoint (Graph drive delta + item permissions; sharing links/ambiguous grants → deny), Purview governance adapter (versioned mapping of verified label/classification metadata → policy fields; missing/stale → deny-by-policy). Pipeline steps (detect → capture identity/rev/checksum/owner/ACL → land → validate type/size → malware scan → parse/layout → normalize tables/pages → chunk → attach metadata → embed → publish → retire) as idempotent functions keyed by `doc_id#revision#step`, checkpointed in DynamoDB. Parsers: pypdf/pdfplumber, python-docx, HTML, text; pages with no text layer → Textract if configured, else reported in extraction coverage → quarantine if mandatory coverage fails. Deterministic IDs (sha256 of tenant/source/key; chunk of doc/version/extraction-version/ordinal/checksum). Publication: stage chunks → conditional flip of manifest `current_version` → mark published → retire old. Deletion = tombstone (immediate) then index purge; artifact retained if legal hold. Permission-only update, replay of failed/quarantined, periodic reconciliation. AWS orchestration: EventBridge→SQS→Step Functions (Standard) using `sqs:sendMessage.waitForTaskToken` to ECS workers; locally an in-process orchestrator runs the same steps.

**Observability:** OTel trace id end-to-end (API→workflow→gateway→ingestion via message attributes). `Tracer` adapter: noop (default) / LangSmith, both behind a redaction layer (no tokens, no raw source text by default; IDs/versions/metrics only) and a bounded drop-on-full queue; failures never affect responses. Separate audit log sink (authz decisions, citation access, admin actions).

**Infra (Terraform, validate only):** modules: network (private subnets, VPC endpoints, NAT + Network Firewall domain allowlist for Microsoft endpoints), kms, s3_documents (versioning, object lock, KMS), dynamodb (PITR), opensearch (VPC, FGAC, encryption, pinned version), ecs (api + worker services, scoped task roles), alb_waf, frontend (S3 + CloudFront OAC), ingestion (EventBridge/SQS+DLQ/SFN), secrets, ecr, observability (CloudWatch, CloudTrail), github_oidc. `envs/{dev,staging,prod}` with separate state/accounts. `infra/langsmith` separate (EKS + official LangSmith Helm/Terraform tooling; license prerequisite documented).

**CI (GitHub Actions, SHA-pinned):** ruff, mypy, pytest unit + integration (OpenSearch & DynamoDB Local service containers), web lint/type/test/build, gitleaks, pip-audit/npm audit, terraform fmt/validate (+tflint), docker builds. Deploy workflow uses OIDC role assumption + environment approvals — written, not run.

## Your data: no SharePoint, a few PDFs (decided)
- **Primary sources are the local-file connector (dev) and the S3 connector (AWS).** Both use the same `SourceConnector` interface and pipeline, so your PDFs exercise the real ingestion path: validate → scan → parse → chunk → embed → publish.
- **Where to put them:** `data/private/<collection>/*.pdf`, which is **gitignored** (along with `data/private/**` and `*.pdf` outside `tests/fixtures`) so your documents never enter source control. In AWS, `scripts/upload-docs.sh` syncs that folder to the KMS-encrypted source bucket, and the S3 event → EventBridge → SQS → Step Functions path ingests them.
- **Permissions:** PDFs carry no ACLs, so each collection gets an `_access.yaml` (with optional per-file overrides) declaring tenant, allowed groups/users, denies, project, and sensitivity label. It maps to the dev IdP's synthetic users and groups (e.g. `alice@finance`, `bob@engineering`, `carol@other-tenant`). The connector treats this file as the *source permission record*, and a missing or invalid file means the document is quarantined (deny by default). In AWS the same file sits next to the objects (S3 object tags are a documented alternative).
- **Governance labels:** without an M365 tenant, labels come from `_access.yaml` and are tagged `governance_source: manual`, never presented as Purview. The Purview adapter and its mapping are still implemented and tested against documented response shapes, with status *implemented, not live-tested / blocked: no tenant*.
- **SharePoint:** the Graph connector (delta + permissions) is implemented and contract-tested against documented payloads, with status **blocked: no tenant**. To live-test it later you'd need an M365 tenant with SharePoint and an app registration with `Sites.Selected` admin consent. A developer sandbox may work, but eligibility is restricted, so I'll check that in the docs rather than assume it.
- **Tests vs. demo:** automated tests and gates use **synthetic, committed fixtures** (generated PDFs/DOCX/HTML, including a scanned image-only page, a table, and a prompt-injection doc) so results are reproducible. Your PDFs are for the demo and the optional `make eval-private` run, whose report is labeled with that dataset and never committed.
- **Scanned PDFs:** locally, image-only pages show up in the extraction coverage report, and quarantine applies if coverage falls below policy. In AWS, Textract handles them once enabled.
- **Quick start for you:** `make up` → drop PDFs into `data/private/demo/` + copy `_access.example.yaml` → `make ingest` → log in as different dev users and compare answers and citation access.

## Requirements coverage checklist (spec section → where it is satisfied)
Gaps found in the earlier draft are marked **[added]**.

**Boundaries.** Entra/Purview stay external. Purview supplies labels/classifications only and is not treated as a permission source. Source systems supply content, revisions, and ACLs. `packages/auth/policy_translation.py` explicitly maps source ACL + governance into `DocPermissionRecord`. Simulated integrations are never described as verified. Self-hosted LangSmith runs on AWS in its own Terraform root.

**Working method.** No autonomous agents or multi-agent delegation in the product. LLMs only fill typed slots, and code owns transitions. **[added]** Independent remote calls (BM25 ∥ k-NN, per-subquery searches, DynamoDB batch rechecks, embeddings batches) run concurrently with `asyncio.gather` + `TaskGroup`, each bounded by deadlines. Gates are never weakened. No secrets or doc text in logs, and a gitleaks pre-commit hook is added. After plan approval I execute all stages continuously and only stop at the Stage 7 apply approval or a real blocker.

**Chunk metadata [added explicit].** The OpenSearch mapping and Pydantic `ChunkRecord` contain `tenant_id, document_id, document_version, chunk_id, source_uri, page/location (page, bbox/char span), section (heading path), content_checksum, source_modified_at, extraction_version, embedding_version, allowed_principals, denied_principals, sensitivity_label, acl_version, governance_version, publication_status`, plus `project_ids, parent_section_id, neighbor ids, table_id/table_header`. Every chunk→source revision mapping is persisted in DynamoDB `chunk_manifest` (doc_id#version → chunk ids). A test asserts every indexed chunk has all required fields.

**Ingestion extras [added explicit].** Type validation uses magic bytes plus extension allowlist (PDF/DOCX/TXT/HTML) and a configurable size limit. The malware scanner interface has dev (EICAR-signature, labeled simulated) and GuardDuty S3 adapters, and production startup fails with the dev scanner. Embedded images are reported as `unprocessed_image` in coverage and never silently dropped. Protected/encrypted files (IRM/MIP-encrypted, password PDFs) are quarantined with reason `protected_content`, with **no decryption**. A replay API/CLI covers failed and quarantined docs. Reconciliation compares the source listing with the manifest and emits deletes/updates.

**Purview [added explicit].** `docs/integrations/purview.md` documents required tenant setup and Graph/Purview permissions (admin consent), supported source capabilities (M365/SharePoint sensitivity labels via Graph vs S3 assets via Purview Data Map scanning, with no parity assumed), refresh behavior (polling interval, `governance_version`, max staleness), network/region limitations (Purview account region, egress allowlist), and missing/stale behavior (deny for labeled-required collections, configurable `unknown_label` → deny). Endpoints are used only if verified in docs, with citations in sources.md.

**Query optimization.** `RetrievalPlan` schema as specified, with `max_subqueries` and `max_retrieval_retries` in config. Suggested filters are intersected with the mandatory authz filter and can never remove or widen it. A test feeds a malicious planner output (tenant swap, principal injection) and asserts it is ignored.

**Retrieval.** All 8 steps. The OpenSearch version is pinned and filtered k-NN behavior is verified and documented. Native hybrid/normalization is evaluated, with app-side RRF as the default if unsuitable. Diversity (max passages per doc), table context, and conflict preservation are each tested.

**Gateway [added explicit].** Modality checks, region/data-classification gate (chunk sensitivity ≤ model's approved max class), context-capacity check before the call, and **no use of Bedrock Intelligent Prompt Routing** (documented as a decision). Routing rules are deterministic YAML keyed on task + classification + context size, with quality evidence linked to eval report IDs. The usage ledger is in DynamoDB.

**Generation.** Prompt-injection fixtures (doc says "ignore instructions / call tool / reveal X") are expected to have no effect. No tools are exposed to the generator. Conflicting sources and outdated revisions are surfaced as labeled fields in the answer schema and UI. Standard mode returns only the validated answer, and SSE carries stage-progress events only. High-assurance mode releases nothing until validation and judge pass.

**Judge [added explicit].** Rubric `evals/rubrics/judge_v1.yaml`. Calibration: `evals/calibration/` holds a human-review template and a script computing agreement (Cohen's κ, confusion matrix) once human labels exist. With no human labels, reports say **"uncalibrated"** and show categorical outcomes, never probabilities.

**Observability.** OTel spans capture stage latency, retrieval/rerank config hash, doc IDs + versions, model + prompt versions, tokens + estimated cost, and retry/failure categories. LangSmith receives the same fields after redaction. `docs/integrations/langsmith.md` **[added]** covers self-hosted licensing (enterprise license key), prerequisites (EKS, RDS/ClickHouse/Redis per vendor chart, which are internal to LangSmith and not used by the app), and how local dev runs without it.

**Eval fixtures [added explicit].** `evals/datasets/synthetic_v1/` covers all 14 categories: exact identifiers, acronyms, multi-doc synthesis, conflicting revisions, tables + scanned (image-only PDF page), unanswerable, embedded prompt injection, unauthorized docs, revocation, deletion, duplicate events, model timeout + fallback, judge failure, and conversation-history access change. Each item is tagged with a category and expected docs/abstention.

**Metrics [added explicit].** Recall@k, nDCG@k, citation validity, abstention precision/recall, latency p50/p95/p99, cost per answer, ingestion freshness (source change → published), permission-revocation delay (revoke → first denied query), and human-reviewed groundedness (from calibration labels, otherwise reported "not measured"). Each report header records dataset ID + hash, n, config version, and `inference: simulated|live`. Deterministic pipeline tests (pytest) are kept separate from quality evals (`make eval`, `make eval-live`).

**Security tests [added explicit].** `tests/security/` covers cross-tenant, cross-group, explicit deny wins, direct citation `GET`, revocation (including a cached answer and history after revoke), stale ACL, unknown label, client-supplied tenant/group/filter headers ignored, token attacks (alg=none, HS256 confusion, wrong iss/aud/tid, expired/nbf, ID token, missing scope, rotated kid), and prod-config startup failures.

**UI [added explicit].** Question entry, conversation view, citation panel with page/revision/freshness ("source updated X, ACL synced Y"), conflict/outdated badges, thumbs feedback + comment → `POST /feedback` (stored in DynamoDB, audited), ARIA-live loading, error and abstention states, keyboard navigation, and a vitest + axe accessibility check.

**Infra extras [added explicit].** TLS everywhere (ACM on ALB/CloudFront, OpenSearch HTTPS enforced, TLS 1.2+ policy). Backup/recovery: DynamoDB PITR, OpenSearch automated + manual S3 snapshot repo, S3 versioning + replication option, AWS Backup plan, and RPO/RTO stated in the runbook. Versioned config: model registry, retrieval config, and policy stored as versioned objects (S3 versioned config bucket + `config_version` stamped on every trace). Env isolation: separate AWS accounts recommended, separate state and tfvars, and no cross-env references. Microsoft egress: security groups + NAT in minimal dev, plus a Network Firewall domain allowlist module (`enable_network_firewall`, on in staging/prod).

**Docs [added].** `docs/integration-status.md` matrix (each integration × implemented-verified / not-live-tested / simulated / blocked), `docs/local-vs-aws.md`, `docs/sources.md`, `docs/runbooks/{deploy,rollback,recovery}.md`, `docs/cost-drivers.md`, `docs/operational-limits.md`, `docs/production-readiness.md` (required live checks, load test plan, security review, pen test, DPIA items, prod config). progress.md also records "new session: re-read this + inspect repo; don't trust prior claims."

## Stages & gates (each ends with an update to `docs/progress.md`: work, files, commands, actual results, unverified, blockers, next)
0. **Specify:** architecture.md (trust boundaries, data flows), assumptions.md, threat-model.md (STRIDE + RAG-specific), implementation-plan.md, progress.md, sources.md. Gate: docs exist and cover required topics.
1. **Vertical slice:** compose stack, dev IdP + synthetic users/tenants, synthetic docs ingested (text/markdown), real BM25 + k-NN, fixture models, citations, authz everywhere, minimal web UI (question, conversation, citations, freshness, loading/error/abstain states). **Gate:** `tests/e2e/test_authz_slice.py` — user A gets answer citing doc X; user B (other group) and user C (other tenant) cannot retrieve, cite, or `GET /citations/{id}` it.
2. **Lifecycle:** PDF/DOCX/HTML parsing, versioned publish, idempotency, deletion, revocation, quarantine, replay. Gate: tests for duplicate events, worker restart mid-pipeline, version replacement, deletion, permission-only change.
3. **Quality path:** Bedrock adapters, planner, RRF, reranking, routing/fallbacks, packing. Gate: deterministic integration tests pass; opt-in `-m live_bedrock` smoke tests reported as *not run* (no creds).
4. **Enterprise integrations:** Entra verifier + overage, source permissions, Purview adapter, SharePoint, prod config checks. Gate: security tests (forged/expired/wrong-aud/wrong-tenant/ID-token/alg-none/rotated-key) pass; live checks marked *blocked*.
5. **Judge & observability:** judge workflow + repair, LangSmith adapter, eval runner, feedback, audit. Gate: tests for judge failure, unsupported claims, redaction, retry exhaustion, high-assurance buffering.
6. **AWS readiness:** Terraform, CI/CD, deploy/rollback, recovery runbook, cost drivers, operational limits. Gate: all builds/tests/static checks + `terraform validate` pass; deployment status reported separately as *not deployed*.
7. **Deploy dev to AWS (awsnew / us-west-2), approval-gated:**
   1. You refresh credentials; I verify with `sts get-caller-identity` and check Bedrock model access for the pinned IDs (read-only).
   2. `scripts/bootstrap-state.sh` (creates the S3 state bucket + KMS key) → **ask before running** since it creates resources.
   3. `terraform plan -out` for `envs/dev`; present resource list + estimated monthly cost (minimal dev ≈ $250–400/mo + Bedrock usage). **Apply only on your yes.**
   4. Build + push api/worker images to ECR, upload web build to S3, invalidate CloudFront, run the ECS deploy.
   5. Cloud smoke tests (`-m cloud`): health, OpenSearch connectivity, Bedrock embed/generate/rerank via the gateway, synthetic-doc ingestion through EventBridge/SQS/Step Functions, and the authz gate (allowed vs denied user) against the deployed API.
   6. Auth in the deployed dev env: Entra if you provide an app registration (tenant ID, API app ID, scopes); otherwise the API **refuses to start in non-local mode with the dev IdP**, so the smoke tests need the Entra registration. This is a dependency on you, and I'll report it as blocked if it's missing.
   7. `scripts/destroy-dev.sh` documented (run only on your request). Results are recorded in progress.md as *deployed-dev, verified/unverified per check*.

## Verification
- `make up` (docker compose) → web at localhost, dev login as synthetic users, ask questions, open citations.
- `make test` (unit), `make test-integration` (real OpenSearch + DynamoDB Local), `make test-e2e` (Stage 1 gate), `make lint typecheck`, `make eval` (fixture-labeled report), `make tf-validate` (Dockerized terraform for every root).
- AWS: `make tf-plan ENV=dev` (Dockerized terraform, `AWS_PROFILE=awsnew`), then apply on your approval; `make smoke-cloud` against the deployed ALB/CloudFront URLs.
- Final handoff lists: what works, run instructions, gates passed (with actual output), live-tested integrations (Bedrock and the AWS services if Stage 7 runs; Entra/Purview/SharePoint only if you provide tenant access), blocked/unverified items, and remaining actions that need your authorization (prod/staging apply, Entra app registration, Purview/Graph admin consent, LangSmith license + EKS apply, GitHub OIDC role trust).
