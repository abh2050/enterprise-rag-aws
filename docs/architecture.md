# Architecture: enterprise-rag-platform

Status: design of record for this repository. AWS **dev was deployed in us-east-2**; later
[verification snapshots](verification/README.md) record live application/ingestion and edge/posture checks.
Staging/prod and optional LangSmith are not recorded as deployed. Entra sign-in remains unverified.
Diagram topology is conceptual; current Terraform and the evidence matrix define environment-specific behavior.

## 1. System context

```
             ┌──────────────────── Microsoft (external) ─────────────────────┐
             │  Entra ID (OIDC, JWKS, tokens)   Microsoft Graph   Purview      │
             └──────▲───────────────────────────────▲────────────────▲───────┘
   browser (MSAL)   │ tokens                         │ groups, drive    │ labels /
        │           │                                │ items, perms     │ classifications
        ▼           │                                │                  │
  CloudFront ─ S3 (SPA)                              │ (NAT, allowlisted egress)
        │                                            │                  │
        ▼                                            │                  │
  WAF → internal ALB ──► ECS Fargate: API ────────────────────┘                  │
                   │  │  │                                              │
                   │  │  └──► Bedrock (NAT in dev; optional VPC endpoint): plan/embed/rerank/generate/judge
                   │  └─────► OpenSearch (VPC): BM25 + filtered k-NN
                   └────────► DynamoDB: manifests, ACLs, revocations, conversations, cache, usage, checkpoints
  S3 source bucket ─► EventBridge ─► SQS ─► Step Functions ─► SQS task queue ─► ECS Fargate: ingestion workers
                                                                   (scan, Textract, parse, chunk, embed, publish)
```

## 2. Trust boundaries

| # | Boundary | What crosses | Control |
|---|---|---|---|
| TB1 | Browser → API | Bearer access token, question text | WAF, TLS, token validation (sig/alg/iss/tid/aud/exp/nbf/scp-roles). Nothing else from the client is trusted for authorization. |
| TB2 | API → Microsoft | Graph calls (group overage, SharePoint, labels) | App credentials in Secrets Manager. Optional Network Firewall can restrict egress hosts; it is disabled in dev. Responses are validated against schemas. |
| TB3 | Source content → pipeline | Untrusted files | Landing area, type/size validation, malware scan, quarantine, parser isolation (worker task), no execution of embedded content. |
| TB4 | Retrieved text → LLM | Untrusted document text | Evidence wrapped as data, no tools exposed, structured output schema, citation validation, judge. |
| TB5 | LLM output → user | Model-generated text | Schema validation, citation IDs checked against packed evidence, server-side resolution of citation metadata, high-assurance buffering. |
| TB6 | App → telemetry | Traces, metrics | Redaction (no tokens, no raw document text), bounded queue, failures isolated. |
| TB7 | Search index vs authoritative store | Chunk ACL copies | The index is a **pre-filter** only. Authoritative permission records in DynamoDB are rechecked before any text reaches a model or user. |

## 3. Identity and authorization

* **Identity verification** (`packages/auth/erp_auth/tokens.py`, `entra.py`, `devidp.py`) produces a
  `VerifiedIdentity` (tenant, object id, scopes/roles, group claim or overage marker).
* **Authorization context** (`context.py`) is built server-side: groups come from the token or, on
  overage, from the `DirectoryAdapter` (Graph `transitiveMemberOf`). Clearance and project entitlements come
  from a server-side entitlement map, keyed by app roles or directory groups. Client headers are ignored.
* **Policy engine** (`policy.py`) evaluates `AuthzContext × DocPermissionRecord → Decision`. It denies by
  default and returns a reason code.
* **Translation** (`policy_translation.py`) maps source permissions (local `_access.yaml`, S3 sidecar,
  Graph permissions) plus governance metadata (manual or Purview) into `DocPermissionRecord`. Source
  permissions and classifications are separate fields and are never merged.

Enforcement points:

1. OpenSearch BM25 query: mandatory `bool.filter` (tenant, allowed principals ∩ user principals,
   `must_not` denied principals, permitted labels, `publication_status=published`).
2. OpenSearch k-NN query: the **same** filter inside `knn.<field>.filter` (Lucene efficient filtering).
3. Post-fusion recheck against DynamoDB `documents` (current version, acl_version, tombstone, revocation).
4. Neighbor or parent chunk expansion: only from chunks that passed the recheck, and the same document version.
5. Answer cache: key includes the principal-set hash, and every cited document is rechecked on read.
6. Conversation history: prior turns are replayed only if all their cited documents still pass the recheck.
   Otherwise the turn is redacted before it reaches the planner or generator.
7. Citation open/download: full recheck on every request.

**Fast revocation**: an admin or connector writes `revoked_at` / bumps `acl_version` in DynamoDB. Step 3
blocks the document on the next request. Index cleanup (`update_by_query`, delete) runs asynchronously
and is not on the security path.

## 4. Query workflow (deterministic state machine)

`packages/rag/erp_rag/workflow.py` has an explicit transition table:

```
RECEIVED → PLANNED → RETRIEVED → AUTHORIZED → RERANKED → PACKED → SUFFICIENCY_CHECKED
  → GENERATED → CITATIONS_VALIDATED → JUDGED → RELEASED
                         ↘ (insufficient) ABSTAINED
  JUDGED --revise (≤1)--> GENERATED ;  any state --error--> FAILED
  SUFFICIENCY_CHECKED --insufficient & retries left--> PLANNED (retrieval retry ≤ 1)
```

Each transition is checkpointed to DynamoDB `workflow` (keyed by `run_id`) with typed state payloads
(IDs and metadata only, no document text). LLMs fill typed slots (plan, rerank scores, answer, judge
verdict). Only code chooses transitions.

## 5. Retrieval

* Per subquery: BM25 (`top_k=50`) ∥ k-NN (`top_k=50`) run concurrently with identical authz filters.
* App-side Reciprocal Rank Fusion (k=60), dedupe by `chunk_id` and identical content checksum within a document version.
* Recheck → rerank (≤40 candidates, via the gateway) → pack (≤10 passages, token budget, ≤3 per document
  for diversity, table header preserved, conflicting revisions flagged) → sufficiency check.
* Defaults live in versioned config (`config/retrieval.v1.yaml`) and are **unvalidated starting points**.
* Native OpenSearch hybrid queries are not used: normalization/score pipelines would need extra pipeline
  config, and app-side RRF lets each leg carry and test its own mandatory filter.

## 6. Model gateway

One `ModelGateway` (`packages/rag/erp_rag/gateway/`) for plan, embed, rerank, generate, and judge. A
versioned YAML registry lists approved models with provider, region/geo, max data classification, context
window, modalities, price, and fallback chain. The gateway enforces the deadline, token and cost budgets,
per-model concurrency, bounded retries with jitter, circuit breaker, classification gate, and context
gate. It records a `RouteRecord` (route reason, model, config version, usage, latency, fallback outcome) in
the usage ledger. Bedrock Intelligent Prompt Routing is not used (unsupported for the chosen Claude models
and not cross-family).

## 7. Ingestion

Connector protocol: `list_changes(cursor)`, `fetch(ref)`, `get_permissions(ref)`, plus a governance adapter.
Pipeline steps are idempotent functions keyed by `(document_id, source_revision, step)` with DynamoDB
checkpoints:

`detect → capture → land → validate → scan → parse → normalize → chunk → attach → embed → publish → retire`

* Deterministic IDs: `document_id = sha256(tenant|source|source_key)[:32]`,
  `document_version = sha256(source_revision|checksum)[:16]`,
  `chunk_id = sha256(document_id|version|extraction_version|ordinal|content_checksum)[:32]`.
* Publication: chunks are bulk-indexed with `publication_status=staged`, then a DynamoDB conditional update
  flips `current_version`, then chunks are marked `published`, then superseded versions are retired. Because
  the recheck requires `chunk.document_version == current_version`, a reader can never mix versions.
* Deletion: tombstone first (immediate), then index purge. Artifacts are kept when `legal_hold=true`.
* Quarantine: failed validation, scan, protected content, or coverage below policy. Replay re-enters at `validate`.
* AWS: EventBridge → SQS → Step Functions Standard → `sqs:sendMessage.waitForTaskToken` → Fargate
  workers. Locally, an in-process orchestrator runs the same step functions.

## 8. Generation, citations, judge

* Prompt: system policy plus evidence blocks `<evidence id="E3" ...>` marked as untrusted data. No tools.
* Output schema: `{status: answered|limited|abstain, claims:[{text, evidence_ids[]}], conflicts[], notes}`.
* Citation validator: every `evidence_id` must exist in the packed set. Unknown IDs mean a deterministic
  failure. URL, title, page, and revision are resolved server-side from the chunk record.
* Judge: separate interface and rubric `evals/rubrics/judge_v1.yaml`. Its verdict can only make the outcome
  stricter (`pass → revise/abstain`), never override deterministic failures. At most one repair cycle.
* Modes: `standard` returns the validated answer, and judge sampling runs asynchronously. `high_assurance`
  buffers until synchronous judge validation completes. Both modes return buffered JSON; streaming is not implemented.

## 9. Observability

OTel trace IDs flow API → workflow → gateway → ingestion (via SQS message attributes). The `Tracer` adapter
(noop | LangSmith) receives only redacted span attributes. A bounded queue drops on overflow and records a
dropped-span counter. The security audit log (`erp.audit`) goes to a separate sink: a separate CloudWatch log
group with its own KMS key and IAM policy in AWS, and `var/audit/*.jsonl` locally.

## 10. Data stores

| Store | Contents | Authority |
|---|---|---|
| DynamoDB `documents` | manifest: current_version, status, acl (principals, denies), acl_version, acl_synced_at, governance, tombstone, legal_hold | **authoritative** |
| DynamoDB `chunk_manifest` | doc_id#version → chunk ids, extraction coverage | authoritative mapping |
| DynamoDB `workflow` | ingestion and query checkpoints | authoritative state |
| DynamoDB `conversations`, `answer_cache`, `feedback`, `usage` | per-user history, cache (TTL), feedback, route ledger | app data |
| OpenSearch `chunks-<embedding_version>` | text, vectors, ACL copy, metadata | derived, eventually consistent |
| S3 / local `var/artifacts` | landing, quarantine, parsed artifacts | artifacts (legal hold via Object Lock) |

## 11. Environments

`local` (Docker Compose, dev IdP, fixture models), plus `dev`, `staging`, and `prod` on AWS (separate
accounts recommended, separate state). Production startup refuses the dev IdP, fixture models, the dev
scanner, disabled TLS verification, or wildcard CORS (`erp_rag.config.Settings.assert_safe_for_environment`).
