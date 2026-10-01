# Self-hosted LangSmith on AWS (optional, separate)

Status: Terraform **written and validated, NOT applied** (the user chose not to deploy it for dev).

* **License:** self-hosted LangSmith is an Enterprise-plan add-on. A license key from LangChain sales is
  required before the Helm chart will run (docs.langchain.com/langsmith/self-hosted, accessed 2026-09-30).
* **Tooling (official):** infrastructure from `github.com/langchain-ai/terraform` `modules/aws/infra` (tag
  `v0.16.97`) and the application via the Helm chart `langchain-ai/helm` → `charts/langsmith` (latest stable tag
  observed `langsmith-0.16.36`). Components: EKS, PostgreSQL (RDS), Redis (ElastiCache), ClickHouse (in-cluster
  or managed), and S3 blob storage.
* **Isolation:** separate Terraform root and state, separate VPC, no shared IAM roles with the RAG stack.
* **Connect the RAG app:** set `ERP_TRACING=langsmith`, `ERP_LANGSMITH_ENDPOINT=<https endpoint>` and put the API
  key in Secrets Manager (`<name>/langsmith-api-key`), injected as `LANGSMITH_API_KEY`. The app only exports
  redacted span metadata (no document text, questions or answers).
* **Local development does not need LangSmith** (`ERP_TRACING=noop` default).

Apply (requires authorization, license and budget approval):
```
terraform init -backend-config=backend.hcl
terraform plan -out tf.plan     # review: EKS + RDS + ElastiCache + NAT ≈ several hundred USD/month minimum
terraform apply tf.plan
helm repo add langchain https://langchain-ai.github.io/helm && helm install langsmith langchain/langsmith \
  --version 0.16.36 -f values.yaml   # values per upstream docs, including the license key from a Secret
```
