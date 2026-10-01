# Production readiness

The development system has recorded AWS verification. Production readiness requires stronger and broader evidence. The [2026-10-01 reports](verification/README.md) establish a live synthetic evaluation, eight ingestion lifecycle checks, 12 edge checks and 58 infrastructure checks. They do not establish real enterprise identity, representative answer quality, scale, independent security approval or recovery guarantees.

## Already recorded in dev

- Live Titan/Nova retrieval and answer workflow against managed OpenSearch/DynamoDB: 22 questions and seven passing scenarios, with synthetic principals.
- PDF/DOCX publication, Textract OCR, ClamAV EICAR quarantine, version replacement, ACL-only changes, duplicate handling and deletion.
- CloudFront SPA/API health, private-origin readiness, security headers, unauthenticated/forged-token rejection and a WAF probe.
- Selected encryption, network, store, role, audit, backup and image configuration checks.

These checks should be repeated against the exact candidate deployment before a production release. A recorded PASS from the repository checker is not an independent compliance audit. The image check permits high findings: the retained report records **two HIGH and one MEDIUM finding per image architecture**, despite no CRITICAL findings.

## Identity and source integration

| Acceptance work | Required evidence |
|---|---|
| Real Entra sign-in | SPA → Entra → API with real tenant tokens; successful authorized answer, rejected ID/foreign-tenant tokens and signing-key rotation |
| Graph group overage | Real transitive membership, cache expiry/revocation behavior and fail-closed outage handling |
| SharePoint permissions | Live delta/paging/410 recovery, complete permissions for app identity, permission-only change, sharing-link denial and deletion |
| Purview | Actual label GUID mapping, protected/extraction cases, Data Map identity/freshness and residency review |
| Secrets | Tenant credentials and consent configured through intended secret paths; rotation exercised |

## Model quality and model operations

- Evaluate a reviewed, representative dataset beyond the 22 synthetic questions; include tables, revisions, missing evidence, adversarial sources and multi-document synthesis.
- Obtain human-reviewed claim labels and run judge calibration; do not interpret categorical judge output as a probability.
- Exercise approved primary and fallback routes, throttling and timeout behavior in the target account/region.
- Confirm model availability/lifecycle, organizational destination permissions and residency requirements before release.
- Replace placeholder registry prices; reconcile estimated usage with billing evidence before claiming per-answer cost.

## Load and resilience

- Define owner-approved latency, availability, throughput and revocation objectives, then measure them under concurrency and realistic corpus size.
- Test OpenSearch resource pressure/node failure, DynamoDB throttling, Bedrock unavailability and Graph outage.
- Measure ingestion throughput, backlog, queue age and callback-token recovery; exercise DLQs and redrive.
- Review multi-AZ search, NAT and endpoint/firewall configuration for the intended environment. Dev is single-node OpenSearch and is not HA.
- Implement and test streaming only if required; both current response modes are buffered.

## Security and supply chain

- Remediate the recorded container high/medium findings and rescan both architectures.
- Conduct independent threat-model review and penetration testing: authz bypass, prompt injection, connector SSRF, parser fuzzing and error/timing leaks.
- Review task roles, OpenSearch FGAC, KMS policies and WAF rules; separate break-glass administration from normal worker access.
- Add/validate image signing, SBOM handling and IaC security scanning where required.
- Review retention, legal hold, residency, privacy and audit access with data owners. Configuration alone does not prove policy compliance.

## Recovery and observability

- Execute a full restore drill with retained receipts. Reapply revocations created after the restore point before reopening traffic.
- Verify backup jobs, recoverability, alarm delivery, subscriptions and on-call escalation. Existing diagram annotations are not a substitute for detailed receipts.
- Verify audit-log delivery, redaction and access isolation end to end under failure/queue pressure.
- Measure achieved RPO/RTO against proposed objectives in the recovery runbook.
- Cross-region DR is not implemented; design it only when the business requires it.
- If LangSmith is adopted, provision/license it separately and test redacted export and outage behavior.

## Release and environments

Configure ACM/DNS and staging/prod HTTPS requirements, separate environment accounts/state, protected GitHub environments, GitHub OIDC role access and deployment variables. Exercise the CI and manual delivery workflows against a candidate image and preserve their run results. Deploy staging/prod only after the relevant checks above have owners and acceptance criteria.
