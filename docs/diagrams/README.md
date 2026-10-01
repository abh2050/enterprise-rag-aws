# Architecture diagram library

Ten existing AWS diagrams document the platform. PNGs are suitable for the README/portfolio; `.drawio` files are editable source. All images are local assets, so the static portfolio does not depend on a diagram-hosting service.

| View | PNG | Editable source | Focus |
|---|---|---|---|
| System overview | [Image](01-system-overview.drawio.png) | [draw.io](01-system-overview.drawio) | The complete AWS platform |
| Query & answer | [Image](02-query-path.drawio.png) | [draw.io](02-query-path.drawio) | From question to verified evidence |
| Document ingestion | [Image](03-ingestion-pipeline.drawio.png) | [draw.io](03-ingestion-pipeline.drawio) | A document has a lifecycle |
| Network security | [Image](04-network-security.drawio.png) | [draw.io](04-network-security.drawio) | Private services, controlled access |
| Authorization | [Image](05-authorization-enforcement.drawio.png) | [draw.io](05-authorization-enforcement.drawio) | Permissions survive every reuse path |
| Model gateway | [Image](06-model-gateway.drawio.png) | [draw.io](06-model-gateway.drawio) | One policy boundary for model calls |
| Observe & recover | [Image](07-observability-recovery.drawio.png) | [draw.io](07-observability-recovery.drawio) | Explain the system when it fails |
| CI/CD delivery | [Image](08-cicd-delivery.drawio.png) | [draw.io](08-cicd-delivery.drawio) | Repeatable delivery, explicit gates |
| Local development | [Image](09-local-development.drawio.png) | [draw.io](09-local-development.drawio) | Explore the product without credentials |
| Optional LangSmith | [Image](10-langsmith-optional.drawio.png) | [draw.io](10-langsmith-optional.drawio) | A separate observability extension |

## Reading the evidence correctly

These diagrams were present before the documentation update and have been preserved. They combine topology, implementation notes, historical results and proposed operational targets. The [verification directory](../verification/README.md) is authoritative for the result figures in the updated README and portfolio.

- The query diagram includes earlier cold-path timing/cost annotations. The retained report is a mixed-path run and must not be relabeled as a cold-path measurement.
- The observability diagram includes alarm-delivery and backup-job annotations without a corresponding detailed receipt in the preserved evidence set. Backup/PITR configuration is recorded; complete restore behavior and recovery times are not established.
- Recovery RPO/RTO values are proposed objectives.
- Authorization diagrams simplify the enforcement flow. `search_filter()` performs index prefiltering; `decide()` handles authoritative policy. Audit records a decision and is not itself an access-control gate. Downloads also perform policy checks.
- Microsoft Entra, Graph, SharePoint and Purview integration paths are implemented but not live-verified. Optional LangSmith is not deployed.
- CloudFront is a global edge service even when shown within a regional deployment grouping. DynamoDB is regional, not a resource deployed inside the VPC. The network view distinguishes gateway access from private compute placement.

## Existing generation workflow

The repository includes `build_diagrams.py`, `_drawio.py`, the existing AWS diagram skill under `.claude/skills/`, and [`scripts/render-diagrams.sh`](../../scripts/render-diagrams.sh). The shell workflow regenerates draw.io sources, validates them, and exports PNGs through a headless draw.io container. Generated images can be opened in draw.io for editing.

Regeneration replaces the source artifacts from the existing Python definitions; it is unnecessary just to view or publish the documentation. The documentation update did not modify those generators or the application code.
