# Production Incident Runbook

Procedure ID: SOP-ENG-112.

## Severity Levels

A SEV1 incident is a full outage of a customer-facing service. A SEV2 incident is a partial
degradation affecting more than ten percent of requests.

## Recovery Objectives

For tier-1 services the RPO is 15 minutes and the RTO is 1 hour. For tier-2 services the RPO is
4 hours and the RTO is 8 hours.

## Escalation

The on-call engineer must acknowledge a SEV1 page within 5 minutes. If the incident is not
mitigated within 30 minutes, escalate to the engineering manager on call.

## SLA

The customer SLA for tier-1 services is 99.9 percent monthly availability.
