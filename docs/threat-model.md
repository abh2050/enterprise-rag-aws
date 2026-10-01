# Threat model

Method: STRIDE per trust boundary (see [architecture.md §2](architecture.md)) plus RAG-specific threats.
Each row names the mitigation and the test that guards it. Rows marked *planned* are not yet covered.

| ID | Threat | Boundary | Mitigation | Test |
|---|---|---|---|---|
| T1 | Forged / alg-confused / unsigned token | TB1 | RS256 allowlist, JWKS signature check, `alg=none` and HS* rejected | `tests/security/test_tokens.py` |
| T2 | Token for another API, or an ID token used as an access token | TB1 | `aud` must equal API client ID, `scp`/`roles` required, `ver=2.0` | `tests/security/test_tokens.py::test_id_token_is_not_an_access_token` |
| T3 | Cross-tenant token | TB1 | `tid` allowlist and issuer bound to `tid` | `test_tokens.py`, `tests/e2e/test_authz_slice.py` |
| T4 | Client-supplied tenant/groups/filters | TB1 | Context built server-side only. Request schemas have no authz fields, and extra fields are forbidden. | `tests/security/test_revocation_and_history.py::test_client_supplied_authorization_inputs_are_ignored` |
| T5 | Cross-group data leakage via search | TB7 | Mandatory filter on BM25 and k-NN, plus recheck | `tests/e2e/test_authz_slice.py` |
| T6 | Stale index ACL after revocation | TB7 | DynamoDB recheck before any model or user sees text | `tests/security/test_revocation_and_history.py::test_revocation_blocks_retrieval_cache_history_and_citations` |
| T7 | Direct citation/document fetch by ID | TB1 | Recheck on `/citations/{id}` and `/documents/{id}/download`. Responses are identical 404s for unauthorized and missing (no existence oracle). | `test_authz_slice.py` |
| T8 | Leakage via answer cache | TB7 | Cache key includes principal hash, and evidence is rechecked on read | same test (cache assertion) |
| T9 | Leakage via conversation history | TB7 | History turns are revalidated and redacted | `tests/security/test_revocation_and_history.py::test_history_is_private_and_revalidated_on_reuse` |
| T10 | Prompt injection in documents | TB4 | Evidence delimited as data, no tools, structured output, citation validation, judge | `tests/unit/test_retrieval_units.py::test_output_policy_drops_injected_text`, `::test_fixture_generator_never_repeats_embedded_instructions`, `tests/unit/test_bedrock_contract.py::test_evidence_is_delimited_as_untrusted_data`, eval q14 |
| T11 | Hallucinated / fabricated citations | TB5 | Citation IDs must be in the packed evidence set, and metadata is resolved server-side | `tests/unit/test_retrieval_units.py::test_unknown_citation_ids_are_dropped`, `tests/integration/test_judge_workflow.py::test_fabricated_citation_is_dropped_deterministically` |
| T12 | LLM planner loosens filters | TB4 | Suggested constraints can only narrow, and authz filters are composed after | `tests/unit/test_retrieval_units.py::test_planner_cannot_inject_authorization_fields`, `::test_planner_output_is_bounded_and_narrowing_only` |
| T13 | Malware in uploads | TB3 | Scan before parse, quarantine on non-clean or unknown | `tests/integration/test_ingestion_lifecycle.py::test_quarantine_and_replay` |
| T14 | Parser exploits / zip bombs | TB3 | Size limits, page limits, DOCX zip ratio limit, worker isolation (separate task role and controlled network paths; dev permits HTTPS egress via NAT) | `tests/unit/test_parsers.py::test_validation_failures` |
| T15 | Protected content decrypted | TB3 | Encrypted PDF detection → quarantine, never decrypted | `tests/unit/test_parsers.py::test_encrypted_pdf_is_protected_content` |
| T16 | Sensitive data in logs / traces | TB6 | Redaction layer, no raw text by default | `tests/unit/test_observability.py::test_redaction_removes_secrets_and_content` |
| T17 | Telemetry outage breaks requests | TB6 | Bounded non-blocking queue, exceptions swallowed and counted | `tests/unit/test_observability.py::test_telemetry_failures_never_block_or_raise` |
| T18 | Unapproved model / region fallback | TB4 | Registry allowlist, fallback chain only within approved entries, classification gate | `tests/unit/test_gateway.py` |
| T19 | Cost/DoS via model calls | TB1 | WAF rate limit, per-request token/cost budgets, concurrency limits, circuit breaker | `test_gateway.py` |
| T20 | Dev auth in production | config | Startup assertion | `tests/security/test_prod_config.py` |
| T21 | Version mixing during reindex/publish | TB7 | `current_version` recheck, staged → published flip | `tests/integration/test_ingestion_lifecycle.py::test_version_replacement_publishes_atomically` |
| T22 | Deleted document still retrievable | TB7 | Tombstone checked in recheck | `tests/integration/test_ingestion_lifecycle.py::test_deletion_blocks_access_and_respects_legal_hold` |
| T23 | Over-broad IAM | AWS | Per-service task roles, scoped resources, KMS key policies | Terraform review (scoped task-role policies in `infra/terraform/modules/ecs`); IaC static scanning (checkov/tfsec) is *planned* |
| T24 | Secrets in repo | delivery | gitleaks in CI + pre-commit, `.env` gitignored | CI |
| T25 | Existence oracle via timing/error | TB1 | Uniform 404 body. Timing is not fully equalized (*accepted risk*, documented). | — |
| T26 | Graph overage failure → over-permissive | TB2 | Overage + directory failure → request fails closed (HTTP 503); groups are never assumed | `tests/unit/test_enterprise_adapters.py::test_context_builder_uses_directory_on_overage_and_roles_for_clearance` |

## Residual risks (accepted for now, revisit before production)
* Search index holds a copy of document text and ACLs. Compromise of OpenSearch exposes text across
  tenants. Mitigation: VPC-only domain, FGAC, KMS. Index-per-tenant is an option for strict tenants.
* The configured Nova US geo inference profile lists US destination regions. This is a data-residency decision for owners.
* Fixture models give no evidence of quality or injection robustness for real LLMs. A small live synthetic eval is now [recorded](verification/README.md); representative adversarial evaluation and human review remain required.
* Group-membership cache TTL (5 min) bounds revocation latency for group-based grants.
