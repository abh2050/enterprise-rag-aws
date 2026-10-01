# Assumptions and decisions

Recorded 2026-09-30. Revisit any item marked **(revisit)** before production.

## Scope and environment
1. Greenfield repo. No prior conventions to preserve.
2. AWS target: profile `awsnew` (account 054772656600), region **us-east-2**, the only region the AWS Organization SCP permits for regional services (changed from us-west-2 on 2026-10-01). Minimal dev footprint. Self-hosted LangSmith
   Terraform is written but **not applied** (license and cost). Any apply needs explicit user approval
   after `terraform plan`.
3. No SharePoint or M365 tenant is available. Local file and S3 connectors are the primary sources. The
   SharePoint, Purview, and Entra adapters are implemented against documented contracts and are **blocked
   for live verification**.
4. The user's own PDFs live in `data/private/` (gitignored) and are never used in committed tests or
   reports.

## Identity
5. Entra v2 access tokens only (`ver=2.0`). v1 tokens are rejected. **(revisit)** if a client must use v1.
6. Allowed algorithms: `RS256` only.
7. Clearance and project entitlements come from **app roles** (`roles` claim) mapped server-side
   (`config/entitlements.yaml`), not from client input. Groups come from the token or Graph on overage.
8. Group overage uses Graph `GET /users/{oid}/transitiveMemberOf/microsoft.graph.group` with application
   permission `User.Read.All` (least-privileged per docs). Results are cached per user for 5 minutes.
   **(revisit)** TTL against the revocation SLO.
9. The local dev IdP issues RS256 tokens with issuer `http://localhost:8000/dev-idp` and audience
   `api://erp-local`. Production startup fails if it is enabled.

## Authorization policy
10. Sensitivity labels are ordered `public < internal < confidential < restricted`. Unknown labels deny.
11. Default `max_acl_age` is 24h. A document whose ACL was not synced within that window is denied.
    **(revisit)** per source.
12. Explicit denies always win over allows.
13. Project-restricted documents require the user to hold at least one listed project entitlement.
14. Sharing links from Graph (`link` permissions without named principals) are treated as ambiguous and
    deny. **(revisit)** if organization-scoped links must grant tenant-wide access.

## Retrieval and models
15. OpenSearch 3.1 (Amazon OpenSearch Service supports 3.1). Lucene HNSW with efficient filtering, cosine
    similarity (`cosinesimil`).
16. Local embeddings: deterministic feature-hashing vectors (dimension 384), labeled **FIXTURE**. AWS:
    Titan Text Embeddings V2 at dimension 1024. The index name includes the embedding version, so a switch
    requires a reindex.
17. **(superseded 2026-10-01; see 17a)** Generation/judge: `us.anthropic.claude-sonnet-5` (US geo inference profile, which may route across
    us-east-1, us-east-2, us-west-2 and Canada per AWS docs). Planner: `us.anthropic.claude-haiku-4-5-20251001-v1:0`
    (EOL no sooner than 2026-10-01, flagged as legacy-risk). Rerank: `amazon.rerank-v1:0` in-region. The
    registry restricts `restricted`-labeled evidence to in-region-only models, and since no in-region
    generator is approved, **restricted documents are not sent to generation** and those questions abstain.
    **(revisit)** with data owners.
17a. **Current models (us-east-2):** generate/judge/rerank use `us.amazon.nova-pro-v1:0` (US geo profile), with fallback to
    `amazon.nova-lite-v1:0` (in-region, the only approved route for restricted evidence). Planner: Nova Lite. Embeddings:
    Titan V2 1024 in-region. Reranking is **LLM-based** because Bedrock Rerank is SCP-denied and not offered in us-east-2.
    Judge and generator share a model family, which reduces judge independence (documented risk). Claude is disabled until the
    Anthropic agreement is accepted.
17b. Malware scanning in AWS uses a **ClamAV sidecar** (GuardDuty is SCP-denied). It is signature-based only, and the worker runs on
    x86_64 because the official image is amd64-only.
18. Token estimate: 4 characters ≈ 1 token for budgeting. Bedrock usage numbers are used for cost when
    available.
19. RRF k=60. Retrieval defaults match the spec and are unvalidated.

## Ingestion
20. Max file size 50 MB (configurable). Allowed types PDF/DOCX/TXT/HTML, checked by magic bytes and extension.
21. Local malware scan uses the EICAR test signature (labeled simulated). AWS uses GuardDuty Malware
    Protection for S3 tags (`GuardDutyMalwareScanStatus`). Anything except `NO_THREATS_FOUND` quarantines.
22. A page counts as "covered" if it yields ≥ 20 characters of text or OCR output. Default mandatory coverage
    is 100% of pages: any uncovered page quarantines the document with report `extraction_incomplete`.
    **(revisit)** per collection.
23. Encrypted or password-protected PDFs are quarantined (`protected_content`) and never decrypted.

## Delivery
24. Python 3.12, uv lockfile. Node 20+ for the web build (local Node 25 used, CI pinned to Node 22 LTS).
25. Terraform 1.13.3 via Docker image. AWS provider pinned in `versions.tf`.
26. No commits or pushes without user request.
