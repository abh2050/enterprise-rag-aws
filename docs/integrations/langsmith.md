# LangSmith integration

* **Adapter:** `packages/observability/erp_observability/langsmith_exporter.py`, enabled with `ERP_TRACING=langsmith`.
  It exports redacted span metadata only: IDs, versions, counts, timings, model keys, and token and cost estimates.
  Questions, answers, and document text are never exported (`hide_inputs`/`hide_outputs` are also set).
  Telemetry failures are isolated by the bounded tracer.
* **Local development works without LangSmith** (`ERP_TRACING=noop`, the default).
* **Self-hosted on AWS:** separate Terraform root `infra/langsmith` (official `langchain-ai/terraform` module) plus the
  official Helm chart. It requires an **Enterprise license key**. Validated, **not applied**. See `infra/langsmith/README.md`.
* **Evaluation:** `erp-eval` writes JSON/Markdown reports locally. Uploading datasets/experiments to LangSmith is a
  follow-up once an endpoint exists, and each upload must keep the dataset, sample size and `inference: simulated|live` header.
* **Status:** implemented-not-live-tested (no endpoint/key available).
