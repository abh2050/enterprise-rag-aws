# Local substitutes versus AWS behavior

Status reflects the [retained 2026-10-01 verification](verification/README.md). No live checks were re-run for this documentation update.

| Concern | Local | AWS design / recorded behavior | Remaining boundary |
|---|---|---|---|
| Search | OpenSearch 3.1 container, security plugin disabled, loopback ports | Managed 3.1, VPC, FGAC/SigV4 and encryption; live retrieval recorded | Load, node failure and independent access review |
| State | DynamoDB Local | DynamoDB with KMS/PITR; live permissions, lifecycle and application state | Local does not exercise IAM/KMS; restore semantics need a drill |
| Artifacts | Filesystem; legal-hold marker | S3 encrypted/versioned artifact storage with Object Lock; live ingestion recorded | Full legal-hold/retention and recovery behavior not proven by configuration checks |
| Malware scanning | Simulated EICAR signature scanner | Real ClamAV sidecar; AWS EICAR quarantined | Signature updates, failure behavior and broader malware coverage |
| OCR | No default cloud OCR; scanned content may be quarantined | Textract scanned-PDF extraction recorded | Broader document/image quality and OCR cost/performance |
| Orchestration | In-process pipeline execution | EventBridge → SQS → Pipe → Step Functions callback → worker, recorded live | Backlog, DLQ/redrive, chaos and sustained throughput |
| Models | Deterministic FIXTURE provider | Titan/Nova live job with synthetic data | Human-grounded quality, calibration, load and model lifecycle/access |
| Identity | Dev RS256 IdP with synthetic users | Entra verifier/MSAL path implemented; edge rejects invalid tokens | Real Entra authentication and Graph overage remain unverified |
| Sources/governance | Local files and manual `_access.yaml` | S3 manual manifests live; Microsoft adapters implemented | SharePoint/Purview real-tenant validation |
| Audit | JSONL and in-memory test sinks | Dedicated CloudWatch audit group/key/retention configuration recorded | Complete delivery, access-isolation and recovery evidence |
| Tracing | Noop/in-memory, redaction tests | Optional redacted LangSmith exporter | Live LangSmith not verified; self-hosted platform not deployed |
| Networking | Loopback Docker services | Private ECS tasks/search, internal ALB, CloudFront VPC origin; edge/posture evidence | Dev NAT tradeoffs; staging/prod endpoints/firewall and failure paths |
| Delivery | Local scripts and Docker builds | ECR/ECS deployment recorded; GitHub workflows defined | Hosted CI/CD and environment/OIDC configuration checked separately |

The live evaluation constructs synthetic authorization contexts inside the VPC. It exercises application authorization with live AWS stores/models, but **does not replace an end-to-end browser → Entra → API authentication test**.
