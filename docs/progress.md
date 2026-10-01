# Progress log

> **New session?** Read this file, then inspect the repository (`git status`, run `make test`) before
> continuing. Do not rely on completion claims here without re-running the gates.

## Stage 0: Inspect and specify (2026-09-30)

**Completed**
* Inspected the repository: empty directory, not a git repo. Ran `git init` (branch `main`). No commits made.
* Checked the toolchain: Python 3.12.13, uv 0.6.14, Node 25.1, Docker 29.2.1 (8 GB), gh, AWS CLI 2.37
  (profiles `default` [no credentials], `awsnew`). No local terraform, so it runs via `hashicorp/terraform:1.13.3`.
* Verified external APIs and limits against official docs (see [sources.md](sources.md)).
* Pulled pinned images: `opensearchproject/opensearch:3.1.0`, `amazon/dynamodb-local:2.6.1`,
  `hashicorp/terraform:1.13.3`.

**Files**: `docs/architecture.md`, `docs/assumptions.md`, `docs/threat-model.md`,
`docs/implementation-plan.md`, `docs/sources.md`, `docs/integration-status.md`, `docs/progress.md`.

**Commands**: `git init`, `docker pull …`, `gh api repos/langchain-ai/{terraform,helm}/…`, WebFetch of docs.

**Gate**: required Stage 0 artifacts exist and cover trust boundaries, data flows, decisions, risks, and
integration status. **PASS** (document review).

**Unverified integrations**: all Microsoft and AWS live integrations (see integration-status.md).

**Blockers**: none for local work. AWS deployment needs `aws sso login --profile awsnew` (or `aws login`)
and the user's approval of the plan. Entra live tests need an app registration.

**Next**: Stage 1 vertical slice.

## Stage 1: Working local vertical slice (2026-09-30)

**Completed**
* Python packages: `erp_auth` (Entra/dev token verification, directory adapters, context builder, deny-by-default
  policy, permission translation), `erp_rag` (settings + prod safety checks, schemas, DynamoDB stores,
  OpenSearch index, model gateway + fixture/Bedrock providers, planner, hybrid retrieval with RRF, recheck,
  rerank, packing, generation validation, judge, workflow state machine, QA service), `erp_connectors`
  (local file connector, parsers, chunker, scanners, artifacts, Textract), `erp_ingestion` (checkpointed
  pipeline, CLI), `erp_observability` (redaction, bounded tracer, audit log), `erp_api` (FastAPI).
* Web UI (`apps/web`): dev sign-in with synthetic users, MSAL path for Entra, question entry, conversation,
  citations with source/ACL freshness, conflict/outdated badges, abstention, feedback, standard and
  high-assurance modes.
* Synthetic corpus `data/synthetic` (2 tenants, 11 documents covering tables, revisions, injection, project
  restriction, restricted label, unlabeled doc).
* Docker Compose, Makefile, Dockerfiles, `.env.example`.

**Commands executed (actual results)**
* `uv run ruff check .` → All checks passed. `uv run mypy packages apps services` (strict) → no issues (50 files).
* `uv run pytest tests/e2e -q` → **6 passed** (Stage 1 gate).
* `uv run pytest tests/unit tests/security -q` → 68 passed.
* `uv run pytest tests/integration -q` → 4 passed (efficient filtered k-NN proven on OpenSearch 3.1.0).
* `npm test` (apps/web) → 3 passed (includes axe a11y check). `npm run lint`, `npm run build` → OK.
* Manual: `erp-ingest sync --source all` → 11 published. `uvicorn` + curl: bob's answer cites only the runbook,
  extra request fields (`tenant_id`, `groups`) are rejected with 422, and oscar's group overage resolves via the directory adapter.

**Gate**: `tests/e2e/test_authz_slice.py`: alice retrieves and cites the 2024 expense policy. bob (other group)
and carol (other tenant) cannot retrieve it, cite it (`/api/citations/{id}` → same 404 as a nonexistent id), or
download it. **PASS**.

**Unverified**: all answers come from FIXTURE models (lexical heuristics). They prove the pipeline, not answer quality.

**Next**: Stage 2: lifecycle tests (duplicate events, restart, version replacement, deletion, permission
changes, quarantine/replay) and PDF/DOCX/HTML parsing fixtures.

## Stage 2: Ingestion and lifecycle (2026-09-30)

**Completed**: PDF (pdfplumber/pypdf, headings by font size, ruled tables, per-page coverage, OCR hook),
DOCX (headings/tables, zip-bomb + encryption checks), HTML (scripts stripped, images reported with alt text), and
text/markdown parsers. Structure-aware chunker (heading boundaries, table splitting with repeated headers,
prev/next links). Checkpointed pipeline with event leases, versioned publication (conditional
`current_version` flip), retirement, tombstone deletion with legal-hold retention, permission-only updates,
quarantine (malware, protected content, validation, extraction coverage), replay, and reconciliation.
Synthetic binary fixtures are generated at test time (`evals/erp_evals/fixtures.py`).

**Commands / results**
* `uv run pytest tests/unit/test_parsers.py` → 14 passed.
* `uv run pytest tests/integration/test_ingestion_lifecycle.py` → **9 passed**: duplicate events, worker
  restart (crash at embed, parse checkpoint reused), expired vs active lease, version replacement, deletion
  ± legal hold, permission-only change, quarantine (EICAR, encrypted PDF, type mismatch, scanned page without
  OCR) and replay with OCR, reconciliation.
* `uv run pytest tests/security/test_revocation_and_history.py -s` → 5 passed. Measured local revocation delay
  (authoritative write → first denied answer) **≈105 ms**. This is a single local measurement, not an SLO.

**Gate**: duplicate events, worker restart, version replacement, deletion, and permission-only changes are
covered. **PASS**.

**Gaps**: S3 connector, Textract and GuardDuty adapters are implemented but not live-tested. Local OCR in tests is a stub function
standing in for the Textract adapter.

**Next**: Stage 3: gateway/routing unit tests, Bedrock contract tests, opt-in live smoke tests.

## Stage 3: Bedrock and retrieval quality path (2026-09-30)

**Completed**: Bedrock provider (Converse for plan/generate/judge with JSON output and thinking disabled, Titan V2
embeddings via InvokeModel, Bedrock Rerank via bedrock-agent-runtime), region pinning, error mapping.
Deterministic routing registry `config/models.bedrock.yaml` (us-west-2). Restricted data is limited to in-region
models, and since no in-region generator is approved, restricted evidence is never sent to generation. Planner
validation (narrowing-only constraints, invented identifiers dropped, bounded subqueries). RRF, dedupe, rerank,
token-budgeted packing with diversity, table headers and revision flags, sufficiency check with ≤1 retrieval retry.
**Fix found by tests**: one hung attempt could consume the whole task deadline and starve fallbacks. Added a
per-attempt timeout (`attempt_timeout_ms`).

**Results**: `pytest tests/unit` → 61 passed (gateway: fallback on timeout, bounded retries, invalid output,
non-retryable skip, classification/context gates, budget, circuit breaker, registry validation).
`tests/unit/test_bedrock_contract.py` → passed (Stubber request-shape and error-mapping contracts).
**Live Bedrock smoke tests (`tests/cloud/test_live_bedrock.py`): NOT RUN.** They are skipped as opt-in, and no AWS
credentials are available in this session.

**Gate**: deterministic integration tests pass. Live smoke tests exist and are explicitly reported as not run. **PASS** (deterministic).

## Stage 4: Enterprise integrations (2026-09-30)

**Completed**: Entra verifier (JWKS rotation and rate-limited refresh), Graph directory adapter (overage, paging, fail
closed), client-credential token provider (Secrets Manager), S3 connector, SharePoint connector (delta,
permissions, content), Purview adapters (Graph extractSensitivityLabels and Data Map Atlas classifications) with a
versioned mapping, production configuration checks (API refuses to construct), and integration docs (`docs/integrations/`).

**Results**: `tests/security/test_tokens.py` (alg none, HS256 confusion, bad signature, wrong aud/iss/tid, ID token,
expired/nbf/lifetime/missing exp, v1, missing scope, app roles, rotation, kid-flood rate limit, overage) plus
`tests/security/test_prod_config.py` (10 unsafe settings × 3 environments) → all passed.
`tests/unit/test_enterprise_adapters.py` → 8 passed (contract only).

**Gate**: security tests pass. **Live verification BLOCKED** (no tenant, no Purview account, no AWS credentials):
Entra, Graph, SharePoint, Purview, S3 (live), GuardDuty, Textract. None is marked verified.

## Stage 5: Judging and observability (2026-09-30)

**Completed**: judge workflow (versioned rubric `evals/rubrics/judge_v1.yaml`, schema and consistency validation,
judge can only tighten outcomes, at most one repair cycle, high-assurance mode withholds anything unjudged, standard mode
uses async sampling). LangSmith exporter (run tree with dotted order, bounded per-trace buffers, `hide_inputs`/`hide_outputs`,
redacted attributes only). Bounded tracer (drop-on-full, exporter failures counted). Audit trail with redaction.
Feedback capture (`POST /api/feedback`, per-user DynamoDB partition, audited). Evaluation runner (`erp-eval`) and
calibration tooling (`python -m erp_evals.calibration`, Cohen's κ). The judge is **UNCALIBRATED** (no human labels).

**Results**
* `pytest tests/integration/test_judge_workflow.py tests/unit/test_observability.py` → 13 passed: judge failure
  (withheld), unsupported claims (single repair; persistent hallucination never released), fabricated citation
  dropped, retry exhaustion → abstain, timeout → approved fallback with persisted route record, redaction,
  telemetry failure isolation, LangSmith run tree without content.
* Full deterministic suite: `pytest -m "not live_bedrock and not cloud"` → **160 passed** (3 live tests deselected).
* `erp-eval` (dataset synthetic_v1, sha256 recorded in report, n=22 questions + 6 scenarios, **inference SIMULATED**):
  Recall@5 1.0, Recall@10 1.0, nDCG@10 0.952, citation validity 1.0, **authorization violations 0**, injection
  string hits 0, abstention precision/recall 1.0/1.0, expected-document-cited 0.929 (q06 multi-document: fixture
  cites both expense revisions but not the PTO policy), p50/p95/p99 latency 66/89/92 ms (local, fixture), cost/answer $0
  (fixture), revocation delay ≈93 ms, single-doc ingestion freshness ≈0.9 s. All 6 scenarios passed. Report:
  `evals/reports/*-synthetic_v1-simulated.{json,md}`. **These numbers describe the local pipeline with fake models
  and are not model-quality evidence.**
* Human-reviewed groundedness: **not measured** (no labels).

**Gate**: judge failure, unsupported claims, redaction, retry exhaustion, and high-assurance release behaviour are tested. **PASS**.

**Unverified**: LangSmith export to a real endpoint (no endpoint/key). Live-model eval (`erp-eval --provider bedrock`) not run.

## Stage 6: AWS readiness (2026-09-30)

**Completed**
* Terraform (`infra/terraform`): modules `network` (VPC, private app/data subnets, NAT, gateway + optional interface
  endpoints, flow logs, optional Network Firewall domain allowlist), `kms` (data/logs/audit CMKs), `storage` (source/
  artifacts/config buckets with BPA, SSE-KMS, versioning, TLS-only, Object Lock, GuardDuty Malware Protection plan),
  `dynamodb` (PITR, KMS, TTL), `opensearch` (3.1, VPC, FGAC+IAM, TLS 1.2, KMS, logs), `ecr`, `ecs` (Fargate ARM api +
  worker, scoped task roles, read-only rootfs, circuit breaker, autoscaling), `edge` (CloudFront + WAF + VPC origin →
  internal ALB + regional WAF, OAC SPA bucket, security headers), `ingestion` (EventBridge → SQS → Pipe → Step Functions
  task token → worker, DLQs, reconcile schedule), `observability` (audit log group, CloudTrail with S3 data events,
  alarms, SNS), `secrets`, `github_oidc`, `backup`, and `stack`. Environments dev/staging/prod with isolated state. Separate
  `infra/langsmith` root using the official upstream module.
* Runtime pieces for AWS: SFN task-token worker (`erp_ingestion.sqs_worker`), CloudWatch audit sink, S3 event key
  resolution.
* CI (`.github/workflows/ci.yml`, SHA-pinned actions): format, lint, mypy, pytest with OpenSearch + DynamoDB service
  containers, deterministic eval, pip-audit, web lint/type/test/build/npm audit, gitleaks, terraform fmt/validate, and an
  image build. Deploy (`deploy.yml`) uses GitHub OIDC (no long-lived keys) and environment approvals. Infra apply stays manual.
* Docs: deploy/rollback/recovery runbooks, cost drivers, operational limits, production readiness, local-vs-AWS, LangSmith.

**Commands / actual results**
* `make lint` → pass. `make typecheck` → pass (61 files, strict). `make test` → **161 passed**, 6 opt-in deselected.
* `make web-check` → lint/typecheck pass, 3 tests passed, build OK. `npm audit --omit=dev` → 0 vulnerabilities.
* `pip-audit` (hashed lock export) → no known vulnerabilities. gitleaks → only the intentional fake key in a test
  (allowlisted) plus gitignored paths.
* `make tf-fmt` → pass. `make tf-validate` → dev, staging, prod, langsmith **valid**.
* `docker build -f apps/api/Dockerfile` → OK. `docker compose --profile app up` → all healthy. Ingestion inside the
  container and a high-assurance answer through the nginx proxy → answered, judge passed. Container with
  `ERP_ENVIRONMENT=prod` and dev defaults → refused to start with `UnsafeConfigurationError`.

**Gate**: builds, tests, static checks and Terraform validation pass. **PASS**.

**Cloud deployment status (separate)**: **NOT DEPLOYED.** No `terraform plan` against an account yet. AWS credentials
for `awsnew` are not present in this session.

**Next**: Stage 7 (approval-gated). The user refreshes `awsnew` credentials and approves state bootstrap, then I run
`terraform plan` for dev and present the resource list and cost for approval.

## Stage 7: AWS deployment (dev), in progress (2026-10-01)

**Account checks (read-only unless noted)**: `awsnew` = dev account (ID redacted; role AccountFullAccessRole).
* AWS Organization SCP (`p-84dcnqrz`) **denies regional services in us-west-2, us-east-1, eu-west-1**. us-east-2
  allows all required services **except GuardDuty**.
* Bedrock (2 approved live calls): **Titan V2 embedding succeeded in us-east-2**. Claude Sonnet 5 was denied because the
  Anthropic Marketplace agreement is not accepted. `bedrock:Rerank` is SCP-denied in us-west-2 (1 approved call), and Rerank
  models are not offered in us-east-2.

**User decisions**: deploy to **us-east-2**. Use **Amazon Nova** now (Nova Pro generate/judge/rerank, Nova Lite planner and
in-region fallback). Claude stays in the registry, disabled. Replace GuardDuty with a **ClamAV sidecar**.

**Changes**: per-region Bedrock clients with an allowlist. `thinking` is sent only to Anthropic. LLM reranking (`rerank-prompt-v1`).
Registry `models-bedrock-v2` (us-east-2). `ClamdScanner` (INSTREAM). Terraform: us-east-2, GuardDuty off, worker on x86_64
with a `clamav/clamav:1.4.6` sidecar (4 GB), IAM scoped to Nova/Titan ARNs, `bedrock:Rerank` removed. Admin replay in AWS
goes via Step Functions to the worker. CI/deploy builds a multi-arch image.

**Verification**: real clamd 1.4.6 (local container): EICAR → `Eicar-Test-Signature`, clean data → OK, 30 MB stream OK.
`make lint typecheck test` → **168 passed**. `make tf-fmt tf-validate` → all 4 roots valid.

**Blocked on user approval**: create the Terraform state bucket (`scripts/bootstrap-state.sh dev`, us-east-2), then
`terraform plan` for review. No AWS resources have been created yet.

### Stage 7 result (2026-10-01): dev DEPLOYED to the dev account, us-east-2

* `terraform apply` (approved): 162 resources. The first run hit expired `aws login` session tokens mid-apply. State was
  recovered (`state push` of errored.tfstate + `force-unlock`), and Terraform now runs natively with a refreshing
  `credential_process`. The OpenSearch service-linked role already existed and was imported.
* Fixes found by the deployment (all now in the code):
  1. OpenSearch ingress used `for_each` over apply-time SG ids, so it now uses a static-key map.
  2. The worker refused to start without Entra settings. Added `service_role`, so only the API requires an IdP.
  3. Task definitions injected an empty Secrets Manager secret, which blocked task start. Secrets are now read at runtime by ARN.
  4. `S3ArtifactStore` omitted the SSE-KMS header, which the bucket policy denies. It now always sends it (unit-tested).
  5. EventBridge → KMS-encrypted SQS failed 18/18 because of a missing key-policy grant. Added grants for events and
     cloudwatch (also fixes alarm → SNS).
* **Verified in AWS (live)**: synthetic corpus uploaded to the source bucket. S3 → EventBridge → SQS → Pipe → Step Functions
  (task token) → Fargate worker (x86 + ClamAV 1.4.6 sidecar) → Titan V2 embeddings (Bedrock us-east-2) → OpenSearch 3.1
  (VPC, FGAC/SigV4) → DynamoDB. **36 executions succeeded**, worker outcomes `published: 11, unchanged: 22` (the deletes
  of non-existent docs), 11 documents `published` in DynamoDB, and 11 embedding route records in the usage ledger.
* **Not verified in AWS**: the query/answer path (API service runs 0 tasks until an Entra app registration is provided),
  Nova generate/judge/rerank invocations, CloudFront → VPC origin → ALB serving, and alarm delivery.
* Images: `erp-dev/app:v0.1.0-20261001b` (multi-arch). Running cost while deployed is ≈ $190–320/month. Destroy with
  `terraform destroy` on request.


## Documentation reconciliation (2026-10-01)

A later repository review found verification outputs newer than the initial Stage 7 narrative in `var/verification/`. Sanitized copies with original-file hashes are now preserved in [verification](verification/README.md). They record:

- `cloud-verify-dev.json` (17:17 UTC): 22 live-model questions and seven passing scenarios; synthetic principals, no HTTP Entra authentication, judge uncalibrated.
- `edge.json` (17:18 UTC): 12 passing CloudFront/private-origin/API health, header and rejection checks.
- `posture-dev.json` (17:18 UTC): 58 passing repository-defined posture checks; both API and worker at 1/1 tasks. Image findings include two HIGH and one MEDIUM per architecture.
- `lifecycle-dev.json` (16:52 UTC): eight passing ingestion checks, including Textract and ClamAV.

These results supersede earlier “API at 0 tasks” and “Nova/Textract not live-tested” status notes for that snapshot. They do not prove real Entra sign-in, current uptime, calibration, load performance or production readiness. No AWS verification calls or infrastructure changes were made as part of this documentation reconciliation. Application, configuration, infrastructure and test source files were preserved.
