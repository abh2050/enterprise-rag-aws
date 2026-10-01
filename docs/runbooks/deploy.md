# Deploy runbook (AWS, us-east-2)

> Nothing in this repository has been deployed unless `docs/progress.md` records an approved apply.
> Every step that creates billable resources or changes identity configuration needs explicit approval.

## 0. Prerequisites (owner: platform + identity admins)
1. AWS account per environment (dev/staging/prod), with the profile used here (`awsnew`) and credentials refreshed:
   `aws sso login --profile awsnew` (or `aws login`), then `aws sts get-caller-identity --profile awsnew`.
2. **Region:** us-east-2 is the only region the AWS Organization SCP permits for this account. Bedrock models in use:
   `amazon.titan-embed-text-v2:0` and `amazon.nova-lite-v1:0` (in-region), and `us.amazon.nova-pro-v1:0` (US geo).
   Run `RUN_LIVE_BEDROCK=1 make live-bedrock` before trusting answers. Claude requires accepting the Anthropic agreement first.
3. **Data-residency sign-off**: Nova Pro uses the US geo profile (us-east-1, us-east-2, us-west-2). Restricted evidence is
   routed only to in-region Nova Lite.
4. Entra app registrations (see `docs/integrations/entra.md`). Without them the deployed API refuses to start
   (dev IdP is local-only).
5. Optional: ACM certificates (ALB cert in us-east-2, CloudFront cert in us-east-1) and DNS names.

## 1. State bucket (creates resources; approval required)
`AWS_PROFILE=awsnew scripts/bootstrap-state.sh dev` writes `infra/terraform/envs/dev/backend.hcl`.

## 2. Plan and review
```
cd infra/terraform/envs/dev
AWS_PROFILE=awsnew terraform init -backend-config=backend.hcl
AWS_PROFILE=awsnew terraform plan -out dev.tfplan \
  -var 'entra_api_client_id=<api-app-id>' -var 'entra_allowed_tenants=["<tenant-guid>"]' \
  -var 'graph_client_id=<graph-app-id>' -var 'alarm_emails=["oncall@example.com"]'
terraform show -no-color dev.tfplan > dev.tfplan.txt   # attach to the change request
```
Review checklist: no public subnets for tasks or data, OpenSearch VPC-only, BPA on all buckets, KMS on every store, IAM
resource scopes, WAF associations, and the cost estimate (`docs/cost-drivers.md`).

## 3. Apply (approval required)
`terraform apply dev.tfplan`. The first apply uses image tag `bootstrap`, which does not exist yet, so ECS tasks will
fail until step 4. The circuit breaker keeps the service at 0 healthy tasks, and nothing is exposed.

## 4. Secrets (out of band, never in Terraform)
`aws secretsmanager put-secret-value --secret-id erp-dev/graph-client-secret --secret-string '{"client_secret":"…"}'`

## 5. Build, push, roll
Push to ECR and roll services: run the GitHub `deploy` workflow (OIDC role from the `github_deploy_role_arn` output), or locally:
```
aws ecr get-login-password | docker login --username AWS --password-stdin <repo-host>
docker buildx build --platform linux/arm64 -f apps/api/Dockerfile -t <repo-url>:<git-sha> --push .
scripts/deploy-ecs.sh erp-dev <repo-url>:<git-sha>
```
SPA: `VITE_AUTH_MODE=entra … npm run build`, then `aws s3 sync dist s3://<web_bucket> --delete`, then create a CloudFront invalidation.

## 6. Smoke tests (`tests/cloud`, opt-in)
`RUN_CLOUD_TESTS=1 ERP_CLOUD_BASE_URL=https://<cloudfront-domain> ERP_CLOUD_TOKEN_ALLOWED=… ERP_CLOUD_TOKEN_DENIED=… pytest tests/cloud -m cloud`.
Record outcomes in `docs/progress.md`.

## 7. Load documents
`scripts/upload-docs.sh data/private/demo s3://<source_bucket>/demo` triggers ingestion via EventBridge.
