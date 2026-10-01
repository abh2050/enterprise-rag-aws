# Repository guide

This guide maps the complete project surface to its purpose. Start with the [README](../README.md) for the story, the [architecture](architecture.md) for trust boundaries, and [verification](verification/README.md) for what was actually exercised. Source code and current configuration take precedence over historical comments and diagram annotations.

## Applications

| Path | Responsibility |
|---|---|
| [`apps/api/erp_api/main.py`](../apps/api/erp_api/main.py) | Application lifespan/wiring; identity and authorization dependencies; health, question, history, citation, download, feedback and admin routes |
| [`apps/api/Dockerfile`](../apps/api/Dockerfile) | Python application image used by API and ingestion tasks |
| [`apps/web/src/App.tsx`](../apps/web/src/App.tsx) | Application shell and authentication-dependent experience |
| [`apps/web/src/auth.ts`](../apps/web/src/auth.ts) | Local identity and MSAL/Entra authentication paths |
| [`apps/web/src/api.ts`](../apps/web/src/api.ts) | Typed HTTP client, request/response contracts and errors |
| [`apps/web/src/components`](../apps/web/src/components) | `Chat`, `Answer`, `CitationPanel`, `DevLogin` |
| [`apps/web/src/App.test.tsx`](../apps/web/src/App.test.tsx) | UI regression and accessibility checks |
| [`apps/web/nginx.conf`](../apps/web/nginx.conf) | Container serving and API proxy behavior |
| [`apps/web/Dockerfile`](../apps/web/Dockerfile) | Local web image; AWS delivery separately publishes the SPA to S3 |
| [`index.html`](../index.html) | Static project portfolio; separate from the React application |

## Identity and authorization — `packages/auth/erp_auth`

- [`tokens.py`](../packages/auth/erp_auth/tokens.py): token policy, RS256 validation and JWKS caching/rotation.
- [`entra.py`](../packages/auth/erp_auth/entra.py): tenant-bound issuer/JWKS construction and Entra verifier setup.
- [`devidp.py`](../packages/auth/erp_auth/devidp.py): synthetic local RS256 identity provider; not a production bypass.
- [`directory.py`](../packages/auth/erp_auth/directory.py): synthetic directory and Graph transitive group lookup with caching.
- [`graph.py`](../packages/auth/erp_auth/graph.py): app credential acquisition, Secrets Manager loading and fail-closed directory wiring.
- [`context.py`](../packages/auth/erp_auth/context.py): server-side entitlement mapping and trusted authorization context.
- [`models.py`](../packages/auth/erp_auth/models.py): verified identity, principals, labels, permission records and decision reasons.
- [`policy.py`](../packages/auth/erp_auth/policy.py): authoritative `decide()` and mandatory OpenSearch prefilters.
- [`policy_translation.py`](../packages/auth/erp_auth/policy_translation.py): separate source permissions and governance metadata translated into enforceable records.

## Retrieval and orchestration — `packages/rag/erp_rag`

| Files / directory | Responsibility |
|---|---|
| [`config.py`](../packages/rag/erp_rag/config.py), [`runtime.py`](../packages/rag/erp_rag/runtime.py) | Settings, environment safety checks and runtime composition |
| [`schemas.py`](../packages/rag/erp_rag/schemas.py) | Typed plans, chunks, evidence, generated output, citations and responses |
| [`service.py`](../packages/rag/erp_rag/service.py) | Request lifecycle, answer cache, history revalidation, generation/judge integration and audit |
| [`workflow.py`](../packages/rag/erp_rag/workflow.py) | Legal state transitions, counters, terminal states and redacted checkpoints |
| [`planner.py`](../packages/rag/erp_rag/planner.py), [`text.py`](../packages/rag/erp_rag/text.py) | Bounded plan normalization, narrowing constraints, identifiers, token estimates and injection heuristics |
| [`retrieval.py`](../packages/rag/erp_rag/retrieval.py) | Concurrent lexical/vector search, RRF, dedupe, authorization, reranking, expansion, packing and sufficiency |
| [`generation.py`](../packages/rag/erp_rag/generation.py), [`prompts.py`](../packages/rag/erp_rag/prompts.py) | Structured generation contract, untrusted evidence wrapping, output validation and server-resolved citations |
| [`judge.py`](../packages/rag/erp_rag/judge.py) | Versioned rubric, categorical judgments and supported-claim filtering |
| [`ids.py`](../packages/rag/erp_rag/ids.py) | Stable document, version, chunk and event identifiers |
| [`search/opensearch.py`](../packages/rag/erp_rag/search/opensearch.py) | Index mappings, embedding-version indexes, filtered BM25/k-NN and chunk lifecycle operations |
| [`stores/dynamo.py`](../packages/rag/erp_rag/stores/dynamo.py) | Nine-table schemas, JSON records, expiring entries and permission-store operations |
| [`gateway/gateway.py`](../packages/rag/erp_rag/gateway/gateway.py) | Registry, route eligibility, budgets, deadlines, retries, fallback, concurrency and usage records |
| [`gateway/types.py`](../packages/rag/erp_rag/gateway/types.py) | Provider/task contracts, model specifications, usage and typed request/response objects |
| [`gateway/providers/bedrock.py`](../packages/rag/erp_rag/gateway/providers/bedrock.py) | Regional Bedrock clients, Titan embeddings, Converse operations and reranking paths |
| [`gateway/providers/fixture.py`](../packages/rag/erp_rag/gateway/providers/fixture.py) | Deterministic simulated model behavior for local development and regression tests |

## Sources, artifacts and extraction — `packages/connectors/erp_connectors`

| Files | Responsibility |
|---|---|
| [`base.py`](../packages/connectors/erp_connectors/base.py) | Source references, change events, fetched content and connector/governance protocols |
| [`localfs.py`](../packages/connectors/erp_connectors/localfs.py) | Local change detection and manual governance |
| [`s3.py`](../packages/connectors/erp_connectors/s3.py) | Object/version capture, reconciliation and S3 manifest governance |
| [`sharepoint.py`](../packages/connectors/erp_connectors/sharepoint.py) | Graph delta, paging/resync, content and conservative permission translation |
| [`purview.py`](../packages/connectors/erp_connectors/purview.py) | Graph sensitivity-label and Purview Data Map adapters; classification only |
| [`access_manifest.py`](../packages/connectors/erp_connectors/access_manifest.py) | Collection and per-file `_access.yaml` parsing and overrides |
| [`artifacts.py`](../packages/connectors/erp_connectors/artifacts.py) | Local/S3 landing, parsed and quarantine storage; encryption headers and legal-hold handling |
| [`scanning.py`](../packages/connectors/erp_connectors/scanning.py) | Simulated local scanner, GuardDuty tag adapter and deployed ClamAV INSTREAM adapter |
| [`parsing.py`](../packages/connectors/erp_connectors/parsing.py) | Validation, PDF/DOCX/HTML/text extraction, coverage and protected-content checks |
| [`chunking.py`](../packages/connectors/erp_connectors/chunking.py) | Section-aware chunks, split tables with repeated headers and neighbor links |
| [`textract.py`](../packages/connectors/erp_connectors/textract.py) | Per-page scanned-PDF OCR fallback |

## Ingestion service — `services/ingestion/erp_ingestion`

[`pipeline.py`](../services/ingestion/erp_ingestion/pipeline.py) owns event claims, idempotent steps, checkpoints, conditional publication, permission updates, retirement, revocation, deletion, replay and reconciliation. [`wiring.py`](../services/ingestion/erp_ingestion/wiring.py) composes the adapters. [`cli.py`](../services/ingestion/erp_ingestion/cli.py) exposes sync, status, reconcile and replay-event commands. [`sqs_worker.py`](../services/ingestion/erp_ingestion/sqs_worker.py) handles callback-token messages and worker orchestration. [`opensearch_security.py`](../services/ingestion/erp_ingestion/opensearch_security.py) configures search security roles/mappings.

## Observability — `packages/observability/erp_observability`

[`audit.py`](../packages/observability/erp_observability/audit.py) separates security audit records from normal logs and implements file, memory, logger and CloudWatch sinks. [`redaction.py`](../packages/observability/erp_observability/redaction.py) scrubs secrets/content. [`tracing.py`](../packages/observability/erp_observability/tracing.py) supplies request context, spans and bounded background export. [`langsmith_exporter.py`](../packages/observability/erp_observability/langsmith_exporter.py) exports redacted run trees to an optional endpoint.

## Infrastructure — `infra/terraform`

| Module | Resources / concern |
|---|---|
| [`stack`](../infra/terraform/modules/stack) | Composition, environment wiring and role/model resource relationships |
| [`network`](../infra/terraform/modules/network) | VPC, subnet tiers, routes, NAT, endpoints, flow logs and optional firewall |
| [`kms`](../infra/terraform/modules/kms) | Data, logs and audit keys and service grants |
| [`storage`](../infra/terraform/modules/storage) | Source/artifact/config buckets, encryption, versioning and artifact Object Lock |
| [`dynamodb`](../infra/terraform/modules/dynamodb) | Application tables, PITR, TTL and encryption |
| [`opensearch`](../infra/terraform/modules/opensearch) | Managed search domain, network and encryption settings |
| [`ecs`](../infra/terraform/modules/ecs) | Task roles, definitions, services, scaling and ClamAV sidecar |
| [`ingestion`](../infra/terraform/modules/ingestion) | EventBridge, Pipes, queues/DLQs, reconciliation schedule and Step Functions ASL |
| [`edge`](../infra/terraform/modules/edge) | CloudFront, OAC, web bucket, internal ALB, WAF and response headers |
| [`observability`](../infra/terraform/modules/observability) | Logs, audit retention, CloudTrail, SNS and alarms |
| [`backup`](../infra/terraform/modules/backup) | Backup vault, daily plan, selection and role |
| [`ecr`](../infra/terraform/modules/ecr) | Image repositories, scanning, encryption and lifecycle |
| [`secrets`](../infra/terraform/modules/secrets) | Secret containers and outputs; not committed credential values |
| [`github_oidc`](../infra/terraform/modules/github_oidc) | Optional GitHub federation and scoped application deployment permissions |

[`envs/dev`](../infra/terraform/envs/dev), [`envs/staging`](../infra/terraform/envs/staging) and [`envs/prod`](../infra/terraform/envs/prod) configure footprints with separate backend examples. [`infra/langsmith`](../infra/langsmith) is optional and independent of the core stack; it has not been deployed.

## Configuration and data

`config/` contains retrieval settings, live/fixture model registries, acronyms, identity/entitlement fixtures and governance mapping. `.env.example` documents runtime switches; `.env.local.example` supplies container-local defaults. The synthetic corpus covers two tenants, multiple groups, policy revisions, tables, project access, restricted and unlabeled documents, HTML, and an injection example. PDF/DOCX fixtures are generated by evaluation/test helpers. `data/private` provides instructions and an access-manifest example; real private documents remain ignored.

## Quality and delivery

- [`evals/erp_evals/runner.py`](../evals/erp_evals/runner.py): isolated evaluation namespace, item metrics, scenarios and report generation.
- [`evals/erp_evals/cloud_verify.py`](../evals/erp_evals/cloud_verify.py): in-VPC live job using synthetic principals, explicitly outside the HTTP authentication path.
- [`evals/erp_evals/calibration.py`](../evals/erp_evals/calibration.py): human/judge labels, confusion matrix, groundedness and Cohen's kappa.
- [`evals/datasets/synthetic_v1`](../evals/datasets/synthetic_v1): question and scenario records; [`evals/rubrics/judge_v1.yaml`](../evals/rubrics/judge_v1.yaml): versioned judgment contract.
- [`tests/unit`](../tests/unit), [`tests/security`](../tests/security), [`tests/integration`](../tests/integration), [`tests/e2e`](../tests/e2e), [`tests/cloud`](../tests/cloud): progressively broader verification with live calls opt-in.
- [`scripts`](../scripts): state bootstrap, ECS deploy/rollback, document upload, AWS edge/posture/lifecycle verification and diagram rendering.
- [`.github/workflows`](../.github/workflows): CI and manual application deployment definitions.
- [`Makefile`](../Makefile), [`docker-compose.yml`](../docker-compose.yml), [`pyproject.toml`](../pyproject.toml), [`uv.lock`](../uv.lock) and [`apps/web/package-lock.json`](../apps/web/package-lock.json): development commands, local services and reproducible dependency inputs.
- [`.pre-commit-config.yaml`](../.pre-commit-config.yaml): secret, lint and formatting hooks.
- [`.claude/skills/aws-architecture-diagram`](../.claude/skills/aws-architecture-diagram): existing draw.io conventions, AWS icon references and validation support used by the diagram toolchain.

## Documentation and artifacts

`docs/` contains design, integrations, runbooks, operational limits, historical progress and readiness. [`docs/verification`](verification) contains sanitized snapshots with original-file hashes. [`docs/diagrams`](diagrams) contains ten PNG/draw.io pairs and their existing generation helpers. `var/`, credentials, private documents, Terraform state and dependency/build directories are local artifacts, not publishable project evidence by default.
