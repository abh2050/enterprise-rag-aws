# Cost drivers (estimates, us-east-2, on-demand list prices as understood 2026-09-30 — verify with the
# AWS Pricing Calculator before approval; not a quote)

## Minimal dev (as configured in `envs/dev`)
| Item | Sizing | Approx. USD/month |
|---|---|---|
| OpenSearch Service | 1 × m7g.medium.search + 20 GB gp3 | 50–60 |
| NAT gateway | 1 × (hourly + data processing) | 35–45 |
| Fargate | api ARM 0.5 vCPU/1 GB + worker x86 1 vCPU/4 GB (app + ClamAV sidecar), 24×7 | 55–70 |
| ALB (internal) | 1 + low LCU | 18–25 |
| AWS WAF | 2 web ACLs + ~6 rules + requests | 15–20 |
| CloudWatch Logs/metrics/alarms | low volume | 5–20 |
| KMS (3 CMKs), Secrets Manager (2), SNS | | 5 |
| DynamoDB on-demand, S3, CloudFront, Step Functions, SQS, EventBridge | low volume | 1–10 |
| ClamAV (sidecar) | included in worker task. freshclam downloads ~200 MB/day via NAT | NAT data ~$0.3/month |
| CloudTrail | mgmt events (first trail free) + S3 data events | 1–5 |
| AWS Backup | DynamoDB + artifacts | 1–5 |
| **Subtotal (infra)** | | **≈ $190–$320/month** |
| Bedrock | Nova Pro/Lite tokens (generate, judge, LLM rerank), Titan embeddings | usage-based (see below) |

Interface VPC endpoints are disabled in dev (each costs ~$7.3/month per AZ, ≈$190/month for 13 endpoints × 2 AZs).
Enabling them in dev raises the total to ≈ $365–$490/month.

## Staging/prod drivers
Multi-AZ OpenSearch (3 × r7g.large + 3 dedicated masters) is the largest fixed cost. After that come NAT per AZ, Network
Firewall (~$0.395/endpoint-hour per AZ, plus data), interface endpoints, and Fargate scale-out.

## Per-answer model cost
The gateway records estimated cost per call (`usage` table, `est_cost_usd`) from registry prices. **Registry prices are
placeholders until confirmed against the Bedrock pricing page.** A standard answer makes 1 plan call (Nova Lite), 1–4 embedding calls,
1 LLM rerank call (≤40 passages ≈ 10–20k input tokens on Nova Pro) and 1 generation call. High-assurance adds 1–2 judge calls and possibly a repair generation.
Per-request budget: `max_cost_usd_per_request` (default $0.50).

## Self-hosted LangSmith (not applied)
EKS control plane (~$73), at least one m5.xlarge node, RDS, ElastiCache, NAT, plus the license. Several hundred USD/month at minimum.

## Cost controls
Destroy dev when idle (`terraform destroy`, with approval). Budgets/alerts are recommended per account. Per-request token/cost
budgets apply in the gateway. Retrieval and judge sampling rates are configurable.
