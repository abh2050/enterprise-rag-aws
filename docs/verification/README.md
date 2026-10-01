# Verification evidence — 1 October 2026

These snapshots preserve existing verification outputs found in `var/verification/` during the repository documentation review. They were **not re-run as part of the documentation update**. Each JSON includes the original file SHA-256, timestamp, and provenance; infrastructure identifiers have been omitted or redacted. They describe the recorded dev deployment, not its current availability or production certification.

| Artifact | Recorded result | How it was produced |
|---|---|---|
| [Cloud evaluation](cloud-verify-dev.json) | 22 questions; 7/7 scenarios passed; recall@10 1.0; nDCG@10 0.9754; citation validity 1.0; 0 authorization violations | [`cloud_verify.py`](../../evals/erp_evals/cloud_verify.py), one-off in-VPC job against live Bedrock, managed OpenSearch and DynamoDB |
| [Edge](edge.json) | 12/12 passed | [`verify_aws_edge.py`](../../scripts/verify_aws_edge.py), CloudFront HTTP probes, readiness, headers and rejection checks |
| [Infrastructure posture](posture-dev.json) | 58/58 passed | [`verify_aws_posture.py`](../../scripts/verify_aws_posture.py), AWS configuration inspection |
| [Ingestion lifecycle](lifecycle-dev.json) | 8/8 passed | [`verify_aws_lifecycle.py`](../../scripts/verify_aws_lifecycle.py), synthetic uploads and lifecycle changes against AWS |
| [Cold-run evaluation](cloud-verify-dev-coldrun.json) | Same 22 questions on the previous image (…e); every item computed, no cache reuse; p50 2884 ms · p95 4012 ms · est. $0.0024/item; 7/7 scenarios | Same `cloud_verify.py` job, 17:09Z; the reference for cold-path latency and cost |
| [Operational receipts](ops-dev.json) | 4/4 passed | [`verify_aws_ops.py`](../../scripts/verify_aws_ops.py), read-only: alarm → SNS action history, AWS Backup job states, CloudTrail delivery status, audit-log scan (100 events, 0 token/secret/corpus-text hits) |

## What the live evaluation establishes

The report identifies `inference=live`, `models-bedrock-v2`, `retrieval-v1`, `judge-v1`, `authz-policy-v1`, and a dataset SHA-256. It exercises exact identifiers, acronyms, tables, revisions, multi-document questions, restricted/project access, cross-tenant access, abstention and a prompt-injection example. OpenSearch reported 38 chunks using Lucene and cosine similarity.

The seven scenarios cover an allowed answer, validated answer-cache reuse, group/tenant citation decisions, revocation before index cleanup, access restoration, synchronous high-assurance judging, and a document injection case. Citation-access decisions in this job call application policy directly; they do not demonstrate an authenticated HTTP citation request from Entra.

## Metrics and limits

| Metric | Recorded value | Interpretation |
|---|---|---|
| Questions | 22 | Small synthetic dataset; not a customer workload |
| Recall@10 | 1.0 | Expected documents retrieved in this dataset |
| nDCG@10 | 0.9754 | Ranking against the dataset's expected documents |
| Expected document cited rate | 1.0 | Expected-source citation metric, not human-rated correctness |
| Citation validity | 1.0 | References resolve to authorized evidence; does not prove each claim is true |
| Authorization violations / injection-string hits | 0 / 0 | Observed in these cases; not a general security guarantee |
| Abstention accuracy | 1.0 | Correct abstention decisions for the recorded test set |
| Latency p50 / p95 / p99 | 45.37 / 3556.05 / 3609.44 ms | Mixed run includes reused work and zero-cost items; not a cold-path or load benchmark |
| Mean estimated model cost | $0.001030 per item | Registry-derived estimate, with placeholder prices and mixed cached/uncached work; not an AWS bill |
| Revocation check delay | 3347.06 ms | One recorded scenario; not a measured service-level objective |
| Judge | UNCALIBRATED | Human-reviewed groundedness and judge calibration remain open |

**Identity boundary:** synthetic principals were constructed inside the VPC job. Real Microsoft Entra sign-in, real tenant tokens, Graph group overage, SharePoint, and Purview remain unverified. Edge probes demonstrate health and unauthenticated rejection, not a successful Entra-authenticated answer.

**Infrastructure boundary:** 58 passing checks are the result of this repository's checker, not an independent audit. The ECR check only requires no **CRITICAL** findings; its evidence still records **two HIGH and one MEDIUM** finding per architecture. Those findings are in zlib, gcc-14 runtime libraries and dash, and Debian 13 had no fixed versions on 2026-10-01. The disposition and pre-production plan are in [security-scan.md](../security-scan.md).

**Recovery boundary:** PITR, Object Lock, and a daily backup configuration are reported. A complete restore drill, recovery timing, and cross-region recovery are not established by these snapshots. [ops-dev.json](ops-dev.json) records a completed DynamoDB backup job and a successful alarm → SNS action. Neither one is a restore test.

**Diagram boundary:** the ten existing diagrams are retained as architecture artifacts. Each figure on a diagram points to a file in this directory, and recovery targets are labelled as proposed. This evidence directory is the source for the metrics quoted in the README and portfolio.

## Local fixture evaluation

The separate [SIMULATED report](../../evals/reports/20261001T012326Z-synthetic_v1-simulated.md) records 22 questions and six scenarios using deterministic fixture models. It is useful for regression testing of the pipeline. Its retrieval, latency, and zero-cost results must not be presented as live model quality. Its dataset hash differs from the live run, so the two reports are not a controlled model comparison.
