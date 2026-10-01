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

The diagrams combine topology, implementation notes, recorded results and proposed operational targets. They were revised on 2026-10-01 after the documentation review, so that every figure on a diagram points to a file in the [verification directory](../verification/README.md). That directory is authoritative for result figures.

- The query diagram quotes the **cold run** ([cloud-verify-dev-coldrun.json](../verification/cloud-verify-dev-coldrun.json): previous image, every item computed, no cache reuse). The later [cloud-verify-dev.json](../verification/cloud-verify-dev.json) is a mixed warm/cold run, and its latency must not be read as cold-path.
- Every observability annotation now has a receipt in [ops-dev.json](../verification/ops-dev.json): alarm action → SNS, completed AWS Backup job (DynamoDB), CloudTrail delivery status, and an audit-log leak scan. A restore has **not** been drilled, so recovery times remain unestablished.
- Recovery RPO/RTO values are proposed objectives.
- The authorization diagram distinguishes the index prefilter (`search_filter()`, E1 and the neighbour fetch E6) from authoritative `decide()` (E2–E5, including downloads). Audit is drawn as a record, not a gate.
- Microsoft Entra, Graph, SharePoint and Purview integration paths are implemented but not live-verified. Optional LangSmith is not deployed.
- CloudFront and its WAF are drawn outside the regional grouping because they are global. DynamoDB is drawn outside the VPC because it is regional and reached through a gateway endpoint. No account identifiers appear on the diagrams.

## Existing generation workflow

The repository includes `build_diagrams.py`, `_drawio.py`, the existing AWS diagram skill under `.claude/skills/`, and [`scripts/render-diagrams.sh`](../../scripts/render-diagrams.sh). The shell workflow regenerates draw.io sources, validates them, and exports PNGs through a headless draw.io container. Generated images can be opened in draw.io for editing.

Regeneration replaces the source artifacts from the Python definitions in `build_diagrams.py`. You don't need it just to view or publish the documentation. To change a diagram, edit `build_diagrams.py` and run `scripts/render-diagrams.sh [name…]`.
