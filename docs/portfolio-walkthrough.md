# Engineering walkthrough and demo

Use this as a speaking guide grounded in the repository. Describe your own contribution accurately; do not present unverified integrations, generated fixture output or proposed recovery targets as production results.

## A 60-second introduction

> I built a permission-aware enterprise RAG platform on AWS. It combines a React application and FastAPI service with document ingestion, hybrid OpenSearch retrieval, a policy-controlled Bedrock gateway and Terraform infrastructure. The important design decision is separating search from authority: OpenSearch finds candidate evidence, but DynamoDB decides whether it can still be used. That check also protects answer caches, conversation history, citations and downloads. I deployed the dev platform, exercised live Bedrock and the ingestion lifecycle with synthetic data, and preserved the verification reports. The next steps are real Entra integration, human-calibrated quality evaluation, load testing and production hardening.

## A five-minute architecture tour

1. **Problem — 45 seconds.** Explain why a company-wide chatbot cannot treat all documents as public. Access changes, stale indexes and cached answers make authorization a data-lifecycle problem.
2. **Architecture — 60 seconds.** Open the system overview. Trace CloudFront → internal ALB → Fargate API; then the S3 → EventBridge → queues/Pipe → Step Functions → worker ingestion path. Name the responsibility of OpenSearch, DynamoDB and Bedrock.
3. **Security — 60 seconds.** Open authorization. Explain server-derived identity context, prefilters on both search legs, authoritative rechecks, and revoked evidence in history/cache. Explain that a classification label never grants access.
4. **Lifecycle — 45 seconds.** Open ingestion. Explain scanning/OCR, checkpoints, duplicate events, staged chunks and conditional current-version publication. Tombstone first, clean the index later.
5. **Operations — 45 seconds.** Explain the KMS event-delivery issue and explicit S3 encryption header found during deployment. Show why testing resource configuration and testing the event path are different tasks.
6. **Evidence and boundaries — 45 seconds.** Show the 22-question live evaluation and 58/12/8 check counts. Say clearly that the users/documents were synthetic, Entra login was not exercised, and the judge is uncalibrated.

## A local product demo

Prerequisites and exact commands are in the [README](../README.md#local-quickstart).

1. Run `make install` and `make up`; inspect ingestion output, then open `http://localhost:5173`.
2. Sign in as Alice (Acme finance). Ask the New York travel per-diem question. Open the citation and discuss document/version/section and freshness information.
3. Sign in as Bob (Acme engineering). Ask the same finance question. Explain why permission filters prevent using finance evidence; then ask about tier-1 RPO/RTO to demonstrate an allowed engineering answer.
4. Sign in as Carol (Globex) to show the tenant boundary.
5. Switch between standard and high-assurance modes. Explain that both return buffered answers and local judgment is a fixture; the mode mechanics are real, the local model behavior is simulated.
6. Show [`test_revocation_and_history.py`](../tests/security/test_revocation_and_history.py) for the revocation scenario. It exercises cache/history/citation enforcement without implying an admin UI exists.
7. Show the [live evaluation receipt](verification/cloud-verify-dev.json) separately. Explain how the in-VPC synthetic-principal job differs from authenticated browser traffic.

Never connect this demonstration to private employer/customer documents without permission. The included synthetic corpus is sufficient to demonstrate the system.

## Questions worth preparing for

| Interview question | Repository-grounded answer |
|---|---|
| Why use both BM25 and vector retrieval? | Exact policy identifiers and acronyms benefit from lexical matching; semantic wording benefits from embeddings. App-side RRF combines ranks and keeps each search leg's mandatory authorization filter explicit. |
| Why is filtering the vector query insufficient? | The search index is eventually consistent. DynamoDB supplies current publication/version, revocation and ACL freshness before evidence is sent to models. |
| How do you prevent leakage through chat history? | History turns carry cited documents and are revalidated before reuse. A turn whose evidence is no longer authorized is withheld. |
| What happens if a document changes halfway through publication? | New chunks are staged; the authoritative current version changes conditionally. Readers reject stale versions. A temporary availability gap is preferable to combining versions. |
| What if a model times out? | Approved fallback chains, attempt deadlines, retries and circuit breaking bound the request. Classification/context eligibility still applies to fallback. |
| Why use a deterministic workflow? | Code controls legal transitions and retry/repair limits. Model output cannot authorize data or select arbitrary actions. |
| Does a valid citation prove the claim? | No. It proves the evidence reference is valid. Human-rated grounding, stronger evaluation and calibrated judging remain necessary. |
| Why ClamAV and LLM reranking? | The deployment record describes account/SCP restrictions on GuardDuty and the dedicated Rerank API. The implementation adapted while keeping scanning and ranking behind interfaces. |
| Is this highly available? | The dev OpenSearch footprint is single-node, with one NAT gateway. Staging/prod configurations exist; they are not recorded as deployed or load-tested. |
| Is it production-ready? | It has concrete live dev evidence, but real enterprise identity, representative quality, load/resilience, vulnerability remediation, restore drills and independent review remain open. |
| What is the recovery security concern? | Restoring DynamoDB can discard recent revocations. Reapply authorization changes from audit evidence before reopening access. |
| What is the actual model cost? | The ledger estimates cost from placeholder registry prices. The retained run mixes cached/uncached work and is not a billing or fresh-answer cost benchmark. |

## Three concrete accomplishment statements

Adapt the wording to your actual contribution and keep the evidence qualifiers:

- Built and deployed an AWS development RAG platform with FastAPI, React, Fargate, Bedrock, OpenSearch and DynamoDB, supported by modular Terraform and an event-driven document ingestion pipeline.
- Implemented deny-by-default document authorization across hybrid retrieval, answer caching, history, citations and downloads; the recorded synthetic live evaluation observed zero authorization violations across 22 questions.
- Developed checkpointed ingestion with ClamAV scanning, Textract OCR, versioned publication and recovery controls; retained AWS verification recorded eight passing lifecycle checks, 12 edge checks and 58 infrastructure posture checks.

## Evidence to have open

[Portfolio](../index.html) · [Source guide](repository-guide.md) · [Authorization policy](../packages/auth/erp_auth/policy.py) · [Ingestion pipeline](../services/ingestion/erp_ingestion/pipeline.py) · [Model gateway](../packages/rag/erp_rag/gateway/gateway.py) · [Verification limitations](verification/README.md) · [Production readiness](production-readiness.md).
