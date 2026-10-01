# Microsoft Entra ID integration

**Status:** implemented-not-live-tested. Token validation is tested with locally generated keys in Entra v2
format (`tests/security/test_tokens.py`, `tests/unit/test_enterprise_adapters.py`). It has not been run against a real tenant.

## Required tenant setup (performed by a tenant admin; requires authorization)
1. **API app registration** (`enterprise-rag-api`)
   * Expose an API: Application ID URI `api://<api-client-id>`, delegated scope `access_as_user`.
   * Manifest: `"accessTokenAcceptedVersion": 2` (the API rejects v1 tokens).
   * App roles (assignable to users/groups): `Clearance.Internal`, `Clearance.Confidential`,
     `Clearance.Restricted`, `Project.<name>`, `Documents.Admin`. These are mapped by `config/entitlements.yaml`.
   * Optional claim `groups` (security groups). Group overage (more than 200) is handled by the directory adapter.
2. **SPA registration** (`enterprise-rag-web`): redirect URI = CloudFront URL, auth code + PKCE, API
   permission `api://<api-client-id>/access_as_user` (admin consent). The SPA requests an **access token**
   for that scope. ID tokens are never sent to the API.
3. **Graph app (daemon) credentials** for group overage and SharePoint: application permission
   `User.Read.All` (least privileged for `transitiveMemberOf`, per Graph docs). Admin consent is required. Store the
   client secret (or prefer a certificate) in AWS Secrets Manager at `ERP_GRAPH_CLIENT_SECRET_ARN`.

## API configuration
```
ERP_AUTH_PROVIDER=entra
ERP_ENTRA_API_CLIENT_ID=<api-client-id>          # expected aud
ERP_ENTRA_ALLOWED_TENANTS=["<tenant-guid>"]      # tid allowlist; issuer must be https://login.microsoftonline.com/<tid>/v2.0
ERP_ENTRA_REQUIRED_SCOPES=["access_as_user"]
ERP_ENTRA_ACCEPTED_APP_ROLES=[]                   # set for daemon callers using app roles
```

## Validation performed per request
RS256 only, signature via JWKS (`/discovery/v2.0/keys`, cached 24h, refreshed on unknown `kid` at most every 30s),
`tid` allowlist, issuer bound to `tid`, `aud`, `exp`/`nbf`/`iat` (60s skew, max 24h lifetime), `ver=2.0`,
and required `scp` or accepted `roles`. `preferred_username`/`name` are display-only.

## Live checks required (BLOCKED: no tenant)
* Acquire a real token via the SPA, then confirm `/api/me` returns the expected tenant, clearance and groups.
* An ID token sent as a bearer is rejected (401).
* A token from a non-allowlisted tenant is rejected.
* A user in >200 groups resolves groups via Graph, and Graph outage returns 503 (fail closed).
* Key rollover: the JWKS refresh picks up the new `kid`.
