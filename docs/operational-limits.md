# Operational limits (current configuration; unvalidated under load)

| Area | Limit / default | Where |
|---|---|---|
| Question length | 2000 chars | `planner.MAX_QUESTION_CHARS`, API schema |
| Upload size | 50 MB (`ERP_MAX_FILE_BYTES`) | parsing/validation |
| PDF pages | 2000 | `parsing.MAX_PDF_PAGES` |
| DOCX archive | ≤ 200 MB uncompressed, compression ratio ≤ 100 | zip-bomb guard |
| Retrieval | BM25 50, k-NN 50, rerank ≤ 40, ≤ 10 passages, ≤ 3 per doc, 6000-token context, ≤ 1 retrieval retry, ≤ 3 subqueries | `config/retrieval.v1.yaml` (starting values, not tuned) |
| Repair cycles | ≤ 1 | retrieval config |
| Gateway | per-task deadline + per-attempt timeout, retries 1–2, breaker opens after 5 failures for 30 s, concurrency 4–8/model | `config/models.*.yaml` |
| Budget | $0.50 and 300k tokens per request (Bedrock registry; fixture registry separate) | registry `budgets` |
| ACL freshness | 24 h max age, after which documents are denied | `ERP_MAX_ACL_AGE_SECONDS` |
| Group overage cache | 5 min (bounds revocation latency for group-based grants) | `GraphDirectory` |
| Answer cache TTL | 10 min, revalidated on every hit | `service.CACHE_TTL_SECONDS` |
| Telemetry | span queue 2048 (drop on full), audit queue 10 000 | tracer / audit sink |
| Worker | 1 message per poll, 15 min visibility, heartbeat 4 min, SFN timeout 1 h, 2 retries | ingestion module |
| Bedrock quotas | account/region defaults (tokens/min, requests/min) | request increases before load tests |
| OpenSearch (dev) | single node, no replicas, so it is **not HA** | dev env |

**Recorded evidence:** a synthetic live run and a single revocation scenario are preserved in [verification](verification/README.md). They are not load/SLO measurements.

**Not yet measured under representative load**: throughput, latency under concurrency, OpenSearch heap pressure at corpus size, Bedrock throttling
behaviour. A load test plan is in production-readiness.md.
