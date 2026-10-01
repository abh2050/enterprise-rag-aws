# Container image security scan: 1 October 2026

Image `erp-dev/app:v0.1.0-20261001f` is the deployed dev image. It is a multi-arch index with linux/amd64 for the worker and linux/arm64 for the API, and both run the same Dockerfile, [apps/api/Dockerfile](../apps/api/Dockerfile).

## History

| Image | Base | ECR basic scan | Action |
|---|---|---|---|
| …-20261001e and earlier | `python:3.12-slim-bookworm` | **4 CRITICAL** (openssl, perl) · 20 HIGH | Rebuilt |
| …-20261001f (deployed) | `python:3.12.13-slim-trixie` + `apt-get upgrade` | **0 CRITICAL** · 2 HIGH · 1 MEDIUM per architecture | Accepted risk, tracked below |

The posture checker originally read scan results from the image *index*, but ECR records findings on each per-architecture manifest. That made the check pass without real evidence. [`scripts/verify_aws_posture.py`](../scripts/verify_aws_posture.py) now inspects every architecture manifest. It requires scan status `COMPLETE` and 0 CRITICAL findings, and the evidence string reports the HIGH/MEDIUM counts.

A local Trivy scan of the trixie image also found 0 CRITICAL findings. It reported 44 HIGH findings, and none had a fixed version available on the scan date. Trivy uses a broader advisory set than ECR basic scanning, so the two counts differ.

## Open findings on the deployed image (ECR basic scan, both architectures)

| Severity | CVE | Package | Installed | Fixed version in Debian 13 (checked 2026-10-01) |
|---|---|---|---|---|
| HIGH | CVE-2026-85091 | zlib | 1:1.3.dfsg+really1.3.1-1+b1 | none (`apt list --upgradable` empty) |
| HIGH | CVE-2026-102010 | gcc-14 (`libgcc-s1`, `gcc-14-base` runtime libs) | 14.2.0-19 | none |
| MEDIUM | CVE-2026-102473 | dash | 0.5.12-12 | none |

**Disposition:** these findings are accepted for the **dev** environment until Debian publishes fixes. The build already runs `apt-get upgrade`, so a rebuild picks up fixes as soon as they ship.

**Before production:**
1. Rebuild and rescan, then require 0 CRITICAL and 0 fixable HIGH findings.
2. Turn on ECR enhanced scanning (Amazon Inspector) for continuous re-evaluation.
3. Evaluate a distroless or minimal base. The API needs no shell; `dash` is present only because of the slim base.
4. Record each accepted risk with an owner and an expiry date.

Reproduce:

```bash
aws ecr describe-image-scan-findings --repository-name erp-dev/app \
  --image-id imageDigest=<per-arch manifest digest> --profile awsnew --region us-east-2
docker run --rm python:3.12.13-slim-trixie bash -c 'apt-get update -qq && apt list --upgradable'
```
