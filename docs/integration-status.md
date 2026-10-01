# Integration status

**Evidence snapshot: 2026-10-01.** This matrix supersedes earlier “not deployed” notes. It is based on code review, the historical progress log and [preserved verification reports](verification/README.md), not a fresh live check during the documentation update.

Vocabulary: **recorded live** means the retained AWS report exercises the stated scope; **local/contract-tested** does not imply a real external-service integration; **implemented, not live-verified** means the integration code exists but the required live evidence is missing.

| Integration | Status | Evidence and boundary |
|---|---|---|
| Core AWS dev stack, us-east-2 | Recorded deployed and checked | [58/58 posture checks](verification/posture-dev.json); not a production certification or current uptime claim |
| Managed OpenSearch 3.1 | Recorded live | Live job reports 38 chunks, Lucene/cosine retrieval; posture records VPC, TLS, encryption and FGAC |
| DynamoDB | Recorded live | Permissions/state used by cloud evaluation and lifecycle checks; nine tables with encryption/PITR reported |
| Local OpenSearch / DynamoDB Local | Local-tested | Integration/e2e suite uses real local services; IAM/KMS/managed-service behavior needs AWS evidence |
| S3 sources and artifacts | Recorded live for ingestion | [Eight lifecycle checks](verification/lifecycle-dev.json); does not establish every retention/legal-hold behavior |
| EventBridge → SQS → Pipe → Step Functions → worker | Recorded live | Deployment log and lifecycle results; DLQ/chaos behavior still needs dedicated exercises |
| Titan V2 embeddings | Recorded live | AWS ingestion and live evaluation, 1,024-dimensional configured embeddings |
| Nova planning/reranking/generation/judging | Recorded live in VPC job | [22 questions and seven scenarios](verification/cloud-verify-dev.json); report records route chains, not proof every fallback route was taken |
| CloudFront → VPC origin → ALB → API | Recorded live for edge checks | [12/12 edge checks](verification/edge.json): SPA, readiness, headers, invalid/missing token rejection, WAF probe |
| Successful Entra-authenticated browser Q&A | Not live-verified | Edge health and in-VPC synthetic-principal evaluation are separate checks; no real tenant sign-in |
| Entra token verification | Implemented; local/contract-tested | Locally signed Entra-format tokens; tenant/app registration and live rotation checks remain |
| Graph group overage | Implemented, not live-verified | Directory/context tests; no real tenant membership evidence |
| SharePoint / OneDrive | Implemented, not live-verified | Contract tests; live app permission completeness and delta/ACL behavior remain |
| Purview governance | Implemented, not live-verified | Graph/Data Map adapters; local/S3 manual labels are explicitly manual, not Purview |
| Textract scanned-PDF OCR | Recorded live | Lifecycle receipt reports scanned page extraction and publication |
| ClamAV | Recorded local and AWS behavior | Local real-clamd test in progress log; AWS EICAR quarantine in lifecycle receipt |
| GuardDuty Malware Protection | Disabled under recorded account constraints | ClamAV sidecar is the deployed scanner |
| Bedrock dedicated Rerank API | Blocked under recorded account constraints | Converse-based LLM reranking configured instead |
| Claude registry entry | Disabled | Model agreement/access not established; `approved: false` |
| CloudWatch audit / CloudTrail | Configuration recorded; end-to-end coverage partial | Posture records dedicated audit retention/key and active CloudTrail; retained reports do not establish every delivery/redaction/recovery assertion |
| AWS Backup / DynamoDB PITR | Configuration recorded | Daily selection/PITR present; full restore drill and measured RPO/RTO not established |
| LangSmith exporter | Implemented, not live-verified | Fake-client tests; no retained live endpoint evidence |
| Optional self-hosted LangSmith | Terraform validated historically; not deployed | Independent root, enterprise license and separate deployment required |
| Staging / production | Configured; not recorded as deployed | Historical Terraform validation is not runtime validation |
| GitHub CI / OIDC delivery | Workflow definitions implemented | Dev was deployed via workstation scripts; hosted runs and OIDC setup must be verified separately |
| Fixture models | Simulated-local | Deterministic pipeline regression only; no live-model quality claim |

## Evidence reconciliation

The original progress log ended its dev deployment entry before later verification outputs were recorded. The newer receipts show API/worker tasks running, edge readiness, live Nova/Titan workflow results, Textract and lifecycle behavior. Earlier statements that the API is necessarily at zero tasks or that all Bedrock generation is untested are therefore historical. Terraform still defaults the API to zero tasks when no Entra API client ID is supplied; a running API/healthy edge does not prove a working real Entra tenant.
