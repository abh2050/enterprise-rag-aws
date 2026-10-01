# Evaluation report — synthetic_v1 (SIMULATED)

> INFERENCE SIMULATED with deterministic FIXTURE models. These numbers validate the pipeline (authorization, retrieval plumbing, citation integrity, workflow) and are NOT evidence of model quality.

* Dataset: `synthetic_v1` sha256 `b1a69592b5da6e03…`, n=22 questions + 6 scenarios
* Config: `{"retrieval": "retrieval-v1", "models": "models-fixture-v1", "rubric": "judge-v1", "authz_policy": "authz-policy-v1"}`
* Generated: 2026-10-01T01:23:26.493002+00:00

| Metric | Value |
|---|---|
| recall_at_5 | 1.0 |
| recall_at_10 | 1.0 |
| ndcg_at_10 | 0.9517 |
| expected_document_cited_rate | 0.9286 |
| citation_validity_rate | 1.0 |
| authorization_violations | 0 |
| prompt_injection_string_hits | 0 |
| abstention_precision | 1.0 |
| abstention_recall | 1.0 |
| abstention_accuracy | 1.0 |
| latency_ms_p50 | 65.5121 |
| latency_ms_p95 | 88.9148 |
| latency_ms_p99 | 92.1965 |
| cost_usd_per_answer_mean | 0.0 |
| ingestion_documents | 14 |
| ingestion_outcomes | {'published': 13, 'quarantined': 1} |
| ingestion_total_ms | 13304.8652 |
| ingestion_freshness_ms_single_doc | 929.9182 |
| permission_revocation_delay_ms | 93.3462 |
| human_reviewed_groundedness | not measured — no human-reviewed labels |
| judge_calibration | UNCALIBRATED |

## Scenarios

| id | category | passed |
|---|---|---|
| s03 | duplicate_ingestion_events | True |
| s06 | conversation_history_access_change | True |
| s01 | permission_revocation | True |
| s02 | deletion | True |
| s04 | model_timeout_fallback | True |
| s05 | judge_failure | True |

## Items

| id | category | status | expected | cited | R@10 | violation |
|---|---|---|---|---|---|---|
| q01 | exact_identifier | answered | Q3 FY2024 Budget Memo | Q3 FY2024 Budget Memo | 1.0 | False |
| q02 | exact_identifier | limited | Production Incident Runbook | Production Incident Runbook | 1.0 | False |
| q03 | acronym | answered | Production Incident Runbook | Production Incident Runbook | 1.0 | False |
| q04 | acronym | answered | Paid Time Off Policy | Paid Time Off Policy | 1.0 | False |
| q05 | acronym | answered | Paid Time Off Policy | Paid Time Off Policy | 1.0 | False |
| q06 | multi_document | answered | Paid Time Off Policy, Travel & Expense Policy (2024) | Travel & Expense Policy (2023), Travel & Expense Policy (2024) | 1.0 | False |
| q07 | conflicting_revisions | answered | Travel & Expense Policy (2024) | Travel & Expense Policy (2023), Travel & Expense Policy (2024) | 1.0 | False |
| q08 | table | answered | Travel & Expense Policy (2024) | Travel & Expense Policy (2024) | 1.0 | False |
| q09 | table_pdf | answered | Logistics Handbook | Logistics Handbook | 1.0 | False |
| q10 | docx | answered | Security Awareness Policy | Security Awareness Policy | 1.0 | False |
| q11 | scanned | abstained | — | — | — | False |
| q12 | unanswerable | abstained | — | — | — | False |
| q13 | unanswerable | abstained | — | — | — | False |
| q14 | prompt_injection | answered | Office Handbook | Office Handbook | 1.0 | False |
| q15 | unauthorized | abstained | — | — | — | False |
| q16 | unauthorized | abstained | — | — | — | False |
| q17 | unauthorized | abstained | — | — | — | False |
| q18 | unauthorized | abstained | — | — | — | False |
| q19 | project_access | answered | Project Atlas Roadmap | Project Atlas Roadmap | 1.0 | False |
| q20 | cross_tenant | answered | Globex Expense Guidelines | Globex Expense Guidelines | 1.0 | False |
| q21 | html | answered | Deployment Guide | Deployment Guide | 1.0 | False |
| q22 | unlabeled | abstained | — | — | — | False |
