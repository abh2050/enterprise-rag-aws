# Rollback runbook

| What | How | Notes |
|---|---|---|
| Bad app release | Automatic: ECS deployment circuit breaker (`rollback=true`). Manual: `scripts/rollback-ecs.sh <cluster> <api-td:rev> <worker-td:rev>` | Task definition revisions are immutable, and images are immutable ECR tags (git SHA). |
| Bad SPA release | `aws s3 sync` the previous build (keep CI artifacts), or restore object versions (bucket versioning), then invalidate CloudFront | |
| Bad config (retrieval/model registry) | Config is versioned in the image. Roll back the image. Versions are stamped in every response (`config_versions`) and trace | |
| Bad infra change | `terraform apply` a plan from the previous commit (state is versioned in S3). Destructive diffs need a separate approval | Never `-target` around state drift without review |
| Embedding model change | Never in place. Create a new index (`<ns>-chunks-<new-embedding-version>`), reindex, switch the registry, keep the old index until verified | The gateway refuses a dimension mismatch |
| Bad ingestion batch | Revoke/tombstone the affected documents (`/api/admin/documents/{id}/revoke`), fix, then replay (`/replay`) | Revocation takes effect on the next request |
