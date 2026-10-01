# Recovery runbook

## Objectives (proposed, to be agreed with owners)
| Store | Mechanism | RPO | RTO |
|---|---|---|---|
| DynamoDB (authoritative ACLs, manifests) | PITR (35 days) + AWS Backup daily | ≤ 5 min (PITR) | ~1 h (restore to new table + switch prefix) |
| S3 artifacts/source | Versioning + AWS Backup (artifacts) + Object Lock legal holds | 0 for overwritten objects (versions) | minutes |
| OpenSearch | Service automated snapshots (hourly, 14 days). **Derived data**: can also be rebuilt by re-ingesting from S3 + DynamoDB | ≤ 1 h | hours (re-ingest scales with corpus) |
| Secrets | Secrets Manager versioning | n/a | minutes |

## Procedures
1. **DynamoDB table corruption**: `aws dynamodb restore-table-to-point-in-time --source-table-name erp-dev-documents --target-table-name erp-dev-restore-documents --restore-date-time <t>`.
   Point `ERP_TABLE_PREFIX` at the restored tables (all tables restored to the same point in time), then redeploy.
   **Revocations made after the restore point are lost.** Re-apply them from the audit log (`document.revoke` events) before
   reopening traffic.
2. **OpenSearch domain loss**: create the domain (Terraform), then either restore the latest automated snapshot (`_snapshot/cs-automated`)
   or reindex: run the reconcile state machine for each source. Retrieval stays safe during the gap because the authoritative
   recheck uses DynamoDB.
3. **Region outage**: not covered in this phase (single region). Cross-region DR needs DynamoDB global tables or
   cross-region backup copy, S3 CRR, and a second OpenSearch domain. Listed in production-readiness.
4. **Compromised credentials**: rotate the Graph secret (Secrets Manager), revoke the Entra app secret, and invalidate sessions (short
   token lifetimes). Use the audit log and CloudTrail for scoping.
5. **Ingestion backlog/DLQ**: inspect `erp-<env>-worker-tasks-dlq`, fix the cause, and replay with `erp-ingest replay-event <id>` or by re-driving the DLQ.
