"""Generate the repo's AWS architecture diagrams (.drawio). Run: python docs/diagrams/build_diagrams.py

Export PNGs with scripts/render-diagrams.sh. Every diagram reflects the Terraform in infra/terraform and the
dev deployment verified on 2026-10-01; anything not deployed is labelled as such on the diagram.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _drawio import Page, write  # noqa: E402

OUT = Path(__file__).parent
SUB = "enterprise-rag-platform · dev env (us-east-2) · 2026-10-01"


def overview() -> Page:
    p = Page("ov", "System overview", 2600, 1500)
    p.title("Enterprise RAG on AWS — system overview", SUB + " · deployed &amp; live-verified")
    p.icon("users", "users", "Employees<br>(browser SPA)", 60, 470)
    p.icon("entra", "saml_token", "Microsoft Entra ID (external)<br>OIDC tokens + JWKS<br><i>placeholder tenant → API fails closed</i>", 60, 200, lp="t")
    p.group("cloud", "cloud", "AWS Cloud · account 054772656600", 250, 140, 2320, 1330)
    p.group("reg", "region", "us-east-2 (only region allowed by org SCP)", 280, 180, 2270, 1270, "cloud")
    p.icon("waf", "waf", "AWS WAF<br>edge + ALB ACLs", 330, 250, "reg", lp="r")
    p.icon("cf", "cloudfront", "CloudFront<br>TLS · security headers", 330, 470, "reg")
    p.icon("web", "s3", "S3 · web bucket<br>(private, OAC)", 330, 760, "reg")
    p.group("vpc", "vpc", "VPC · 2 AZ · private app + data subnets", 520, 250, 940, 870, "reg")
    p.icon("alb", "application_load_balancer", "Internal ALB<br>(no public listener)", 580, 470, "vpc")
    p.icon("api", "fargate", "ECS Fargate · API<br>FastAPI · ARM64", 820, 470, "vpc")
    p.icon("os", "elasticsearch_service", "OpenSearch 3.1<br>BM25 + Lucene HNSW<br>FGAC · SigV4", 1100, 470, "vpc", lp="r")
    p.icon("ddb", "dynamodb", "DynamoDB<br>manifests · ACLs<br>revocations · cache", 1100, 720, "vpc", lp="r")
    p.icon("wk", "fargate", "ECS Fargate · Worker<br>x86 + ClamAV sidecar", 1100, 960, "vpc")
    p.icon("bed", "bedrock", "Amazon Bedrock (API + worker)<br>Nova Pro (US geo) · Nova Lite<br>Titan Embed v2 (in-region)", 1580, 290, "reg")
    p.icon("tx", "textract", "Amazon Textract<br>(OCR for scanned pages)", 1580, 560, "reg")
    p.icon("art", "s3", "S3 · artifacts<br>parsed · quarantine<br>Object Lock (legal hold)", 1580, 830, "reg")
    p.icon("src", "s3", "S3 · source docs<br>+ _access.yaml", 2320, 1250, "reg")
    p.icon("eb", "eventbridge", "EventBridge<br>object events", 2100, 1250, "reg")
    p.icon("q1", "sqs", "SQS · ingest-events<br>(+DLQ)", 1880, 1250, "reg")
    p.icon("pipe", "eventbridge_pipes", "EventBridge Pipe", 1660, 1250, "reg")
    p.icon("sfn", "step_functions", "Step Functions<br>ingestion workflow", 1440, 1250, "reg")
    p.icon("q2", "sqs", "SQS · worker-tasks<br>(task token)", 1220, 1250, "reg")
    p.dgroup("plat", "Platform services (apply to every component)", 1860, 250, 650, 840, "reg")
    p.icon("cw", "cloudwatch_2", "CloudWatch<br>logs · alarms", 1900, 320, "plat")
    p.icon("ct", "cloudtrail", "CloudTrail<br>(+ S3 data events)", 2110, 320, "plat")
    p.icon("sns", "sns", "SNS<br>alarm topic", 2320, 320, "plat")
    p.icon("kms", "key_management_service", "KMS CMKs<br>data · logs · audit", 1900, 560, "plat")
    p.icon("sm", "secrets_manager", "Secrets Manager<br>(read at runtime)", 2110, 560, "plat")
    p.icon("ecr", "ecr", "ECR<br>immutable · scan", 2320, 560, "plat")
    p.icon("bk", "backup", "AWS Backup<br>DynamoDB + artifacts", 1900, 800, "plat")
    p.note("SSE-KMS on every store · separate audit log group + key · alarms → SNS · "
           "image scanning · DynamoDB PITR · S3 versioning", 2090, 800, 400, 90, "plat", font=11)
    p.edge("users", "cf", "HTTPS")
    p.edge("users", "entra", "sign-in (MSAL)", "async", "t", "b")
    p.edge("waf", "cf", kind="async", exit="b", entry="t")
    p.edge("cf", "web", "OAC", ex=(1, 0.85), en=(1, 0.5))
    p.edge("cf", "alb", "VPC origin")
    p.edge("alb", "api")
    p.edge("api", "os", "filtered hybrid search")
    p.edge("api", "ddb", "authz recheck", ex=(1, 0.85), en=(0, 0.5))
    p.edge("api", "bed", "Converse", ex=(0.75, 0), en=(0, 0.5))
    p.edge("api", "entra", "JWKS", "async", ex=(0.25, 0), en=(1, 0.5))
    for a, b in (("src", "eb"), ("eb", "q1"), ("q1", "pipe"), ("pipe", "sfn"), ("sfn", "q2")):
        p.edge(a, b, exit="l", entry="r")
    p.edge("q2", "wk", "poll", ex=(0.5, 0), en=(1, 0.8))
    p.edge("wk", "ddb", "manifest flip", exit="t", entry="b")
    p.edge("wk", "os", ex=(0, 0.5), en=(0, 0.8))
    p.edge("wk", "art", ex=(1, 0.45), en=(0, 0.5))
    p.edge("wk", "tx", "OCR", ex=(1, 0.15), en=(0, 0.5))
    return p


def query_path() -> Page:
    p = Page("qp", "Query path", 2400, 1400)
    p.title("Query path — deterministic RAG workflow with authorization at every hop",
            SUB + " · each step is a checkpointed state; LLMs only fill typed slots")
    W, H = 230, 100
    a = [80 + 320 * i for i in range(7)]
    steps_a = [
        "<b>① Verify Entra JWT</b><br>RS256 · iss / aud / tid<br>exp · nbf · scp → else 401",
        "<b>② Build AuthzContext</b><br>server-side only: tenant,<br>user + groups, clearance, projects",
        "<b>③ Validated answer cache</b><br>key = principal-set hash;<br>evidence re-authorized on read",
        "<b>④ Plan query</b><br>rewrite · acronyms · ≤N subqueries<br>suggested filters only narrow",
        "<b>⑤ Hybrid retrieval</b><br>BM25 top-50 ∥ kNN top-50<br>same mandatory ACL filter",
        "<b>⑥ RRF fusion</b> (k = 60)<br>dedupe by chunk id",
        "<b>⑦ Authoritative recheck</b><br>batch decide() vs DynamoDB<br>revoked / stale → dropped",
    ]
    steps_b = [
        "<b>⑧ Rerank</b><br>≤40 candidates → top-k<br>LLM rerank (Nova)",
        "<b>⑨ Pack + sufficiency</b><br>≤10 passages · doc diversity<br>insufficient → retry once / abstain",
        "<b>⑩ Generate</b><br>evidence wrapped as untrusted data<br>structured claims + citation ids",
        "<b>⑪ Validate citations</b><br>ids ∈ packed evidence; page /<br>revision resolved server-side",
        "<b>⑫ Judge</b> (rubric judge-v1)<br>pass · revise (≤1 repair) · abstain<br>cannot override hard failures",
        "<b>⑬ Release or abstain</b><br>answered · limited · abstained<br>(high-assurance: buffered)",
        "<b>⑭ Audit + trace</b><br>decisions, citations, cost, latency<br>redacted — no document text",
    ]
    for i, t in enumerate(steps_a):
        p.box(f"a{i}", t, a[i], 380, W, H, font=11)
    for i, t in enumerate(steps_b):
        p.box(f"b{i}", t, a[6 - i], 760, W, H, font=11)
    for i in range(6):
        p.edge(f"a{i}", f"a{i + 1}")
        p.edge(f"b{i}", f"b{i + 1}", exit="l", entry="r")
    p.edge("a6", "b0", exit="b", entry="t")
    p.edge("a2", "b5", "cache hit", "async", exit="b", entry="t")
    cx = lambda i: a[i] + W // 2 - 39  # noqa: E731
    p.icon("entra", "saml_token", "Entra ID JWKS<br>(cached, rotates on unknown kid)", cx(0), 170, lp="r")
    p.icon("graph", "internet", "Microsoft Graph<br>(group overage only)", cx(1), 170, lp="r")
    p.icon("ddb1", "dynamodb", "DynamoDB<br>answer cache · history", cx(2), 170, lp="r")
    p.icon("bed1", "bedrock", "Bedrock<br>Nova Lite (in-region)", cx(3), 170, lp="r")
    p.icon("os", "elasticsearch_service", "OpenSearch<br>BM25 + HNSW (filtered)", cx(4), 170, lp="r")
    p.icon("ddb2", "dynamodb", "DynamoDB<br>ACLs · revocations", cx(6), 170, lp="r")
    for i, n in ((0, "entra"), (1, "graph"), (2, "ddb1"), (3, "bed1"), (4, "os"), (6, "ddb2")):
        p.edge(f"a{i}", n, kind="async", exit="t", entry="b")
    p.icon("bed2", "bedrock", "Bedrock · Nova Pro (US geo)<br>→ fallback Nova Lite (in-region)", cx(2) + 640, 1040)
    p.icon("cw", "cloudwatch_2", "CloudWatch Logs<br>audit group (KMS)", cx(0), 1040)
    p.edge("b0", "bed2", "rerank", "async", exit="b", entry="r")
    p.edge("b2", "bed2", "generate", "async", exit="b", entry="t")
    p.edge("b4", "bed2", "judge", "async", exit="b", entry="l")
    p.edge("b6", "cw", kind="async", exit="b", entry="t")
    p.note("<b>Live result (in-VPC job, synthetic_v1, n=22, inference=live, judge UNCALIBRATED):</b> recall@10 1.0 · "
           "nDCG@10 0.975 · citation validity 100% · 0 authorization violations · 0 prompt-injection hits · "
           "abstention accuracy 1.0 · cold-path p50 2.9 s / p95 4.0 s · ≈ $0.0024 per answer",
           1300, 1180, 1050, 90, font=11)
    return p


def ingestion() -> Page:
    p = Page("in", "Ingestion pipeline", 2500, 1450)
    p.title("Ingestion — event-driven, checkpointed, versioned publish",
            SUB + " · S3 → EventBridge → SQS → Pipe → Step Functions (task token) → Fargate worker")
    y = 300
    p.icon("up", "users", "Uploader / sync<br>(upload-docs.sh, SSE-KMS)", 60, y)
    p.icon("src", "s3", "S3 · source bucket<br>docs + _access.yaml", 290, y)
    p.icon("eb", "eventbridge", "EventBridge rule<br>Object Created / Deleted", 520, y)
    p.icon("q1", "sqs", "SQS · ingest-events", 750, y, lp="t")
    p.icon("pipe", "eventbridge_pipes", "EventBridge Pipe", 980, y)
    p.icon("sfn", "step_functions", "Step Functions<br>ingestion (Standard)", 1210, y)
    p.icon("q2", "sqs", "SQS · worker-tasks<br>waitForTaskToken", 1440, y)
    p.icon("wk", "fargate", "ECS Fargate worker (x86)<br>+ ClamAV clamd sidecar<br>event leases · task heartbeats", 1670, y, lp="r")
    p.icon("dlq", "sqs", "DLQs (per queue)<br>alarm → SNS", 750, 520)
    p.icon("sch", "eventbridge_scheduler", "EventBridge Scheduler<br>periodic reconcile", 900, 100, lp="r")
    p.icon("adm", "fargate", "API · admin replay<br>(quarantined / failed)", 1250, 100, lp="r")
    for a_, b_ in (("up", "src"), ("src", "eb"), ("eb", "q1"), ("q1", "pipe"), ("pipe", "sfn"), ("sfn", "q2"), ("q2", "wk")):
        p.edge(a_, b_)
    p.edge("q1", "dlq", "maxReceive", "error", exit="b", entry="t")
    p.edge("sch", "sfn", kind="async", exit="b", entry="t", en=(0.15, 0))
    p.edge("adm", "sfn", "replay", "async", exit="b", entry="t", ex=(0.5, 1), en=(0.5, 0))
    p.edge("wk", "sfn", "SendTaskSuccess / Failure", "async", ex=(0.5, 0), en=(0.85, 0))
    p.dgroup("pl", "Worker pipeline — idempotent steps keyed doc_id#revision#step, checkpointed in DynamoDB "
             "(a restarted worker resumes at the last completed step)", 50, 640, 2410, 560)
    W = 230
    xs = [80 + 265 * i for i in range(9)]
    steps = [
        "<b>1 · Lease + detect</b><br>event lease · dedupe<br>duplicate → skip",
        "<b>2 · Capture identity</b><br>revision · sha256 · owner<br>ACL + labels (_access.yaml)",
        "<b>3 · Land + validate</b><br>magic bytes + allowlist · size<br>protected → quarantine",
        "<b>4 · Malware scan</b><br>ClamAV INSTREAM<br>infected / timeout → quarantine",
        "<b>5 · Parse + OCR</b><br>PDF · DOCX · HTML · TXT<br>image-only pages → Textract",
        "<b>6 · Chunk</b><br>heading path · table headers<br>deterministic chunk ids",
        "<b>7 · Embed</b><br>Titan Embed v2 · 1024-d<br>embedding_version stamped",
        "<b>8 · Stage + publish</b><br>index staged chunks →<br>conditional current_version flip",
        "<b>9 · Retire / tombstone</b><br>old version retired<br>delete → tombstone, then purge",
    ]
    for i, t in enumerate(steps):
        p.box(f"s{i}", t, xs[i], 720, W, 100, "pl", font=11)
        if i:
            p.edge(f"s{i - 1}", f"s{i}")
    p.edge("wk", "pl", exit="b", entry="t", ex=(0.5, 1), en=(0.72, 0))
    cx = lambda i: xs[i] + W // 2 - 39  # noqa: E731
    p.icon("art", "s3", "S3 · artifacts<br>landing · quarantine · parsed<br>Object Lock legal hold", cx(2), 1000, "pl")
    p.icon("clam", "container_1", "ClamAV sidecar<br>clamav/clamav:1.4.6", cx(3), 1000, "pl")
    p.icon("tx", "textract", "Amazon Textract", cx(4), 1000, "pl")
    p.icon("bed", "bedrock", "Bedrock<br>Titan Embed v2", cx(6), 1000, "pl")
    p.icon("os", "elasticsearch_service", "OpenSearch<br>erp-chunks-titan…-v1", cx(7), 1000, "pl")
    p.icon("ddb", "dynamodb", "DynamoDB<br>manifest · checkpoints<br>tombstones", cx(8), 1000, "pl")
    p.edge("s2", "art", kind="async", exit="b", entry="t")
    p.edge("s3", "clam", kind="async", exit="b", entry="t")
    p.edge("s3", "art", "quarantine", "error", ex=(0.2, 1), en=(1, 0.5))
    p.edge("s4", "tx", kind="async", exit="b", entry="t")
    p.edge("s6", "bed", kind="async", exit="b", entry="t")
    p.edge("s7", "os", kind="async", exit="b", entry="t")
    p.edge("s8", "ddb", kind="async", exit="b", entry="t")
    p.edge("s7", "ddb", "flip", "async", ex=(0.8, 1), en=(0, 0.5))
    p.note("<b>Live lifecycle checks on AWS — 8/8 PASS:</b> PDF published (source change → searchable ≈ 6 s) · DOCX · "
           "scanned page via Textract (100% coverage) · EICAR → quarantined · version replaced → old retired · "
           "permission-only change (ACL v1 → v2, no re-embed) · duplicate event ignored · delete → tombstoned",
           50, 1240, 1600, 80, font=11)
    p.note("<b>Also:</b> replay of failed/quarantined docs (admin API → Step Functions) · scheduled reconcile compares "
           "source listing with manifests · legal hold retains artifacts after delete",
           1700, 1240, 760, 80, font=11)
    return p


def network() -> Page:
    p = Page("nw", "Network & security", 2600, 1700)
    p.title("Network &amp; edge security — private by default",
            SUB + " · nothing in the VPC has a public IP; the only public entry is CloudFront")
    p.icon("users", "users", "Employees", 60, 640)
    p.icon("entra", "saml_token", "Entra ID JWKS<br>login.microsoftonline.com", 2440, 220)
    p.group("cloud", "cloud", "AWS Cloud", 200, 130, 2160, 1350)
    p.icon("waf", "waf", "WAF (CLOUDFRONT scope)<br>managed rules · rate limit", 250, 400, "cloud", lp="t")
    p.icon("cf", "cloudfront", "CloudFront<br>HTTPS only · HSTS · CSP", 250, 640, "cloud")
    p.group("reg", "region", "us-east-2", 420, 160, 1920, 1300, "cloud")
    p.icon("igw", "internet_gateway", "Internet gateway", 1120, 200, "reg", lp="l")
    p.group("vpc", "vpc", "VPC (flow logs → CloudWatch)", 460, 300, 1500, 1130, "reg")
    for k, x in (("a", 490), ("b", 1230)):
        p.group(f"az{k}", "az", f"Availability Zone {k.upper()}", x, 340, 700, 1070, "vpc")
        p.group(f"pub{k}", "public", "Public subnet", x + 20, 390, 660, 210, f"az{k}")
        p.group(f"app{k}", "private", "Private app subnet", x + 20, 630, 660, 330, f"az{k}")
        p.group(f"dat{k}", "private", "Private data subnet", x + 20, 990, 660, 390, f"az{k}")
    p.icon("nat", "nat_gateway", "NAT gateway<br>(single, dev)", 800, 460, "puba", lp="r")
    p.note("No NAT in this AZ in dev<br>(single_nat_gateway = true;<br>one per AZ in prod)", 1300, 440, 300, 80, "pubb", font=11)
    p.icon("alb", "application_load_balancer", "Internal ALB<br>(nodes in both AZs)<br>+ regional WAF", 540, 740, "appa")
    p.icon("api", "fargate", "ECS task · API", 880, 740, "appa", lp="r")
    p.icon("wk", "fargate", "ECS task · worker<br>+ ClamAV sidecar", 1360, 740, "appb", lp="r")
    p.icon("os", "elasticsearch_service", "OpenSearch domain<br>VPC-only · 1 node (dev)<br>HTTPS · FGAC · SigV4", 880, 1110, "data", lp="r")
    p.icon("gwe", "endpoints", "Gateway endpoints<br>S3 · DynamoDB", 1360, 1110, "datb", lp="r")
    p.icon("s3", "s3", "S3 buckets<br>(bucket policies:<br>VPC / role scoped)", 2060, 1020, "reg", lp="r")
    p.icon("ddb", "dynamodb", "DynamoDB", 2060, 1240, "reg", lp="r")
    p.dgroup("apis", "Regional AWS APIs<br>(via NAT in dev; interface<br>endpoints when enabled)", 2010, 330, 310, 600, "reg")
    for i, (n, l) in enumerate((("bedrock", "Bedrock"), ("textract", "Textract"), ("ecr", "ECR"),
                                ("cloudwatch_2", "CloudWatch"), ("secrets_manager", "Secrets"),
                                ("step_functions", "SFN · SQS"))):
        p.icon(f"r{i}", n, l, 2050 + (i % 2) * 150, 430 + (i // 2) * 170, "apis", size=64)
    p.edge("users", "cf", "HTTPS")
    p.edge("waf", "cf", kind="async", exit="b", entry="t")
    p.edge("cf", "alb", "VPC origin (private)")
    p.edge("alb", "api", "HTTP :8000 (SG)")
    p.edge("api", "os", "443 SigV4", exit="b", entry="t")
    p.edge("wk", "os", "443 SigV4", exit="l", entry="r", ex=(0, 0.7))
    p.edge("wk", "gwe", exit="b", entry="t")
    p.edge("gwe", "s3", ex=(1, 0.3), en=(0, 0.5))
    p.edge("gwe", "ddb", ex=(1, 0.7), en=(0, 0.5))
    p.edge("api", "nat", "egress", exit="t", entry="b")
    p.edge("nat", "igw", exit="t", entry="b")
    p.edge("igw", "entra", "JWKS (HTTPS)", "async")
    p.edge("igw", "apis", "TLS", "async", ex=(1, 0.8), en=(0, 0.1))
    p.note("<b>Security groups:</b> ALB ← CloudFront VPC-origin only · tasks ← ALB only (:8000) · OpenSearch ← tasks only (:443) · "
           "tasks egress 443 only. <b>Org SCP</b> pins regional services to us-east-2 and denies GuardDuty (→ ClamAV). "
           "<b>Prod toggles</b> (Terraform, off in dev): enable_interface_endpoints · NAT per AZ · enable_network_firewall "
           "(domain allowlist for Microsoft endpoints) · multi-AZ OpenSearch with dedicated masters.",
           460, 1500, 1880, 100, font=12)
    return p


def authorization() -> Page:
    p = Page("az", "Authorization enforcement", 2400, 1450)
    p.title("Document-level authorization — deny by default, enforced at seven points",
            SUB + " · the client never supplies tenant, groups or filters")
    y = 330
    p.icon("users", "users", "User (browser)", 60, y)
    p.icon("idp", "saml_token", "Entra ID (AWS / prod)<br><i>dev IdP only when ERP_ENVIRONMENT=local</i>", 300, y)
    p.box("tv", "<b>TokenVerifier</b><br>RS256 only · JWKS rotation<br>rejects ID tokens, alg=none,<br>HS256 confusion, wrong tid", 560, y - 25, 230, 130, font=11)
    p.box("ctx", "<b>AuthzContext</b><br>tenant · user + group principals<br>(Graph on overage) · clearance<br>· projects", 860, y - 25, 230, 130, font=11)
    p.box("dec", "<b>decide(ctx, DocPermissionRecord)</b><br>→ Allow | Deny(reason)<br><br>Deny: tenant mismatch · no principal overlap · explicit deny · project restriction · "
          "label above clearance / unknown · ACL stale · tombstoned / revoked · not current version",
          1160, y - 70, 300, 220, fill="#FFF7F7", stroke="#DD344C", font=11)
    for a_, b_ in (("users", "idp"), ("idp", "tv"), ("tv", "ctx"), ("ctx", "dec")):
        p.edge(a_, b_)
    p.dgroup("ep", "Enforcement points — each one calls decide()", 1540, 120, 520, 960, color="#DD344C")
    eps = [
        "<b>E1 · OpenSearch mandatory filter</b> — tenant, principals, must_not denied, label ≤ clearance, published only; on BM25 <i>and</i> kNN",
        "<b>E2 · Authoritative recheck</b> — batch against DynamoDB before rerank, generation and judge",
        "<b>E3 · Answer cache</b> — keyed by principal-set hash; evidence re-checked on every read",
        "<b>E4 · Conversation history</b> — turns whose evidence is no longer authorized are dropped",
        "<b>E5 · Citation open</b> — GET /api/citations/{id} re-runs decide()",
        "<b>E6 · Neighbour / parent chunks</b> — same filter + recheck",
        "<b>E7 · Audit</b> — every allow / deny with reason",
    ]
    for i, t in enumerate(eps):
        p.box(f"e{i}", t, 1570, 170 + i * 128, 460, 100, "ep", font=11, align="left")
    p.edge("dec", "ep", ex=(1, 0.5), en=(0, 0.25))
    p.icon("os", "elasticsearch_service", "OpenSearch<br>chunk ACL fields", 2200, 181, lp="b")
    p.icon("ddb", "dynamodb", "DynamoDB<br>ACLs · revocations<br>manifests", 2200, 440)
    p.icon("cw", "cloudwatch_2", "Audit log group<br>(KMS audit key)", 2200, 940)
    p.edge("e0", "os")
    p.edge("e1", "ddb", en=(0, 0.3))
    for i in (2, 3, 4):
        p.edge(f"e{i}", "ddb", kind="async", en=(0, 0.7))
    p.edge("e6", "cw")
    yb = 1180
    p.icon("doc", "documents", "Source document<br>+ ACL (_access.yaml / SharePoint)", 60, yb)
    p.box("gov", "<b>Governance labels</b><br>Purview adapter (blocked: no tenant)<br>dev: manual labels, tagged as such", 330, yb - 10, 260, 100, font=11)
    p.box("pt", "<b>policy_translation</b><br>source ACL + labels →<br>DocPermissionRecord<br>ambiguous / missing → deny", 680, yb - 20, 250, 120, font=11)
    p.icon("ddb2", "dynamodb", "DynamoDB<br>acl_version · governance_version", 1030, yb)
    p.box("rv", "<b>Revocation</b> (admin / ACL change)<br>DynamoDB write → effective on the next<br>request; index cleanup runs async<br><i>live: revoke → denied ≈ 2.7–3.3 s</i>", 1340, yb - 20, 300, 120, fill="#FFF7F7", stroke="#DD344C", font=11)
    p.edge("doc", "gov")
    p.edge("gov", "pt")
    p.edge("pt", "ddb2")
    p.edge("rv", "ddb2", exit="l", entry="r")
    p.note("<b>Live on AWS (2026-10-01):</b> 0 authorization violations across 22 questions × synthetic users in 2 tenants; "
           "owner group allowed, other group + other tenant denied on citation open; revoked doc blocked in answer, cache and "
           "history before index cleanup. HTTP 401 on missing / forged tokens via CloudFront. "
           "<i>Entra token validation is implemented + tested against signed test tokens, not live-verified (no tenant).</i>",
           1700, 1140, 640, 150, font=11)
    return p


def model_gateway() -> Page:
    p = Page("mg", "Model gateway", 2400, 1300)
    p.title("Model gateway — one choke point for every model call",
            SUB + " · registry models-bedrock-v2 · deterministic routing, no Bedrock Intelligent Prompt Routing")
    tasks = [
        ("plan", "<b>plan</b><br>deadline 8 s"),
        ("embed", "<b>embed</b><br>batch · 1024-d"),
        ("rerank", "<b>rerank</b><br>deadline 15 s"),
        ("gen", "<b>generate</b><br>deadline 45 s · ≤4 concurrent"),
        ("judge", "<b>judge</b><br>deadline 45 s · ≤4 concurrent"),
    ]
    for i, (k, t) in enumerate(tasks):
        p.box(k, t, 80, 230 + i * 160, 230, 80, font=11)
    p.dgroup("gw", "ModelGateway", 420, 180, 780, 840, color="#01A88D")
    inner = [
        "<b>Registry (YAML, versioned)</b><br>approved ids · region · residency · max classification · context · price",
        "<b>Classification gate</b><br>evidence sensitivity ≤ model max class; restricted → in-region only",
        "<b>Capacity check</b><br>prompt fits context window before the call",
        "<b>Budgets + deadlines</b><br>≤ $0.50 · ≤ 300k tokens per request; asyncio deadline per task",
        "<b>Retry + circuit breaker</b><br>jittered backoff · attempt_timeout_ms so a hung call can't starve fallback",
        "<b>Fallback chain</b><br>only within the approved chain for the task",
        "<b>Usage ledger</b><br>route reason · model · tokens · cost · latency · fallback outcome",
    ]
    for i, t in enumerate(inner):
        p.box(f"g{i}", t, 460 + (i % 2) * 370, 240 + (i // 2) * 190, 340, 120, "gw", font=11, align="left")
    for k, _ in tasks:
        p.edge(k, "gw", en=(0, 0.5))
    p.icon("ddb", "dynamodb", "DynamoDB · usage ledger", 900, 1100, lp="r")
    p.edge("gw", "ddb", kind="async", exit="b", entry="t", ex=(0.62, 1))
    p.group("reg", "region", "us-east-2 · in-region", 1360, 180, 520, 480)
    p.icon("lite", "bedrock", "<b>Nova Lite</b> amazon.nova-lite-v1:0<br>plan · fallback for rerank / generate / judge<br>max class: restricted", 1420, 260, "reg", lp="r")
    p.icon("titan", "bedrock", "<b>Titan Embed Text v2</b><br>1024-d · embedding_version<br>titan-embed-v2-1024-v1", 1420, 480, "reg", lp="r")
    p.dgroup("geo", "US cross-region inference profile (us.*) → us-east-1 · us-east-2 · us-west-2", 1360, 720, 520, 240)
    p.icon("pro", "bedrock", "<b>Nova Pro</b> us.amazon.nova-pro-v1:0<br>primary: rerank · generate · judge<br>max class: confidential", 1420, 790, "geo", lp="r")
    p.icon("claude", "bedrock", "<b>Claude Sonnet 5</b> (approved: false)<br>Anthropic agreement not accepted<br>— registry entry, never routed", 1990, 645, lp="b")
    p.edge("gw", "lite", "restricted / fallback", ex=(1, 0.15), en=(0, 0.5))
    p.edge("gw", "titan", "embed", ex=(1, 0.38), en=(0, 0.5))
    p.edge("gw", "pro", "≤ confidential", ex=(1, 0.75), en=(0, 0.5))
    p.edge("gw", "claude", "blocked (not approved)", "error", ex=(1, 0.6), en=(0, 0.5))
    p.note("<b>Why LLM rerank?</b> bedrock:Rerank is denied by the org SCP in the only regions offering it, so rerank "
           "runs as a scored Converse call (passages ≤ 1,500 chars). <b>Local:</b> a deterministic fixture provider "
           "(hash embeddings, extractive answers) is labelled FIXTURE and refused at startup outside ERP_ENVIRONMENT=local.",
           1360, 1060, 980, 100, font=11)
    return p


def observability() -> Page:
    p = Page("ob", "Observability & recovery", 2400, 1300)
    p.title("Observability, audit &amp; recovery", SUB + " · logs, audit and trail are separate, KMS-encrypted and retained")
    srcs = [("api", "fargate", "ECS API"), ("wk", "fargate", "ECS worker"), ("sfn", "step_functions", "Step Functions"),
            ("alb", "application_load_balancer", "ALB · WAF")]
    for i, (k, n, l) in enumerate(srcs):
        p.icon(k, n, l, 80, 200 + i * 230)
    p.icon("logs", "cloudwatch_2", "CloudWatch Logs<br>service + SFN + flow logs<br>(logs KMS key)", 520, 200)
    p.icon("audit", "cloudwatch_2", "Audit log group<br>authz decisions · citation opens<br>admin actions (audit KMS key)", 520, 480)
    p.icon("trail", "cloudtrail", "CloudTrail<br>management + S3 data events", 520, 820)
    p.icon("tb", "bucket", "Trail bucket<br>(versioned, KMS)", 820, 820)
    p.icon("alarm", "alarm", "Alarms: ALB 5xx · DLQ depth<br>OpenSearch red · SFN failed", 860, 200)
    p.icon("sns", "sns", "SNS → email", 1180, 200)
    p.box("trace", "<b>Trace id end-to-end</b><br>X-Trace-Id on every response → workflow → gateway → ingestion messages<br>"
          "tracer: noop in dev · LangSmith adapter (redacted, drop-on-full queue) — not deployed", 860, 440, 420, 130, font=11)
    p.icon("kms", "key_management_service", "KMS CMKs<br>data · logs · audit", 1180, 820)
    p.edge("api", "logs")
    p.edge("wk", "logs", en=(0, 0.7))
    p.edge("sfn", "logs", en=(0, 0.9))
    p.edge("api", "audit", "audit events", "async", ex=(1, 0.8), en=(0, 0.5), pts=((230, 262), (230, 519)))
    p.edge("alb", "trail", "API calls", "muted", en=(0, 0.5))
    p.edge("trail", "tb")
    p.edge("logs", "alarm", "metrics")
    p.edge("alarm", "sns")
    p.dgroup("rec", "Recovery (docs/runbooks/recovery.md)", 1480, 140, 860, 940, color="#7AA116")
    p.icon("bk", "backup", "AWS Backup (daily)<br>vault KMS-encrypted", 1540, 220, "rec", lp="r")
    p.icon("ddb", "dynamodb", "DynamoDB PITR (35 d)<br>RPO ≤ 5 min · RTO ≈ 1 h", 1540, 440, "rec", lp="r")
    p.icon("s3", "s3", "S3 versioning + Object Lock<br>legal hold on artifacts", 1540, 660, "rec", lp="r")
    p.icon("os", "elasticsearch_service", "OpenSearch hourly snapshots<br>derived data — rebuildable<br>by re-ingesting from S3 + DynamoDB", 1540, 880, "rec", lp="r")
    p.edge("bk", "ddb", kind="async", exit="b", entry="t")
    p.note("<b>Live checks:</b> 46 audit events with 0 secret / document-text leaks · alarm → SNS delivered · "
           "CloudTrail log files present · on-demand AWS Backup job COMPLETED · posture 58/58 PASS",
           1980, 220, 340, 170, "rec", font=11)
    p.note("<b>Never logged:</b> tokens, secrets, raw document text. Logs and traces carry ids, versions, "
           "hashes and metrics only (redaction layer + tests).", 80, 1150, 1200, 60, font=11)
    return p


def cicd() -> Page:
    p = Page("cd", "CI/CD", 2400, 1150)
    p.title("Build &amp; delivery", SUB + " · GitHub Actions (SHA-pinned) → OIDC → ECR / ECS / S3 + CloudFront")
    p.icon("dev", "user", "Engineer", 60, 380)
    p.icon("gh", "source_code", "GitHub repo<br>(to be connected)", 280, 380)
    p.box("ci", "<b>ci.yml</b><br>ruff · mypy --strict · pytest (unit, integration with real OpenSearch + DynamoDB Local, e2e, security) "
          "· vitest + axe · gitleaks · pip-audit / npm audit · terraform fmt + validate · docker build", 500, 330, 320, 180, font=11)
    p.box("dp", "<b>deploy.yml</b><br>environment approval<br>multi-arch buildx (ARM64 API, x86 worker)<br><i>written — not yet run</i>", 900, 350, 280, 140, font=11)
    p.icon("role", "role", "GitHub OIDC provider<br>+ scoped deploy role<br><i>not created in dev</i>", 1300, 380)
    p.icon("ecr", "ecr", "ECR<br>immutable tags · scan on push", 1600, 220)
    p.icon("ecs", "ecs", "ECS services<br>api · worker (rolling,<br>circuit-breaker rollback)", 1900, 220)
    p.icon("web", "s3", "S3 web bucket", 1600, 560)
    p.icon("cf", "cloudfront", "CloudFront<br>invalidation", 1900, 560)
    p.edge("dev", "gh", "PR")
    p.edge("gh", "ci")
    p.edge("ci", "dp", "main")
    p.edge("dp", "role", "OIDC", "muted")
    p.edge("role", "ecr", "push", ex=(1, 0.3), en=(0, 0.5))
    p.edge("ecr", "ecs", "digest")
    p.edge("role", "web", "sync", ex=(1, 0.7), en=(0, 0.5))
    p.edge("web", "cf")
    p.box("tf", "<b>Terraform 1.13 · infra/terraform/envs/{dev,staging,prod}</b><br>plan → human approval → apply; separate state per env",
          900, 720, 340, 100, font=11)
    p.icon("st", "bucket", "State bucket<br>versioned · KMS · lockfile", 1330, 730)
    p.edge("tf", "st")
    p.note("<b>How dev was actually deployed:</b> from a workstation with scripts/deploy-ecs.sh (same buildx multi-arch build, "
           "push to ECR, ECS update, wait for steady state) and scripts/rollback-ecs.sh — every Terraform apply was approved "
           "by a human. GitHub OIDC needs github_repository set; note that an org SCP currently denies "
           "iam:ListOpenIDConnectProviders, so confirm OIDC is permitted before relying on it.",
           60, 900, 1500, 110, font=11)
    return p


def local_dev() -> Page:
    p = Page("ld", "Local development", 2300, 1100)
    p.title("Local development — the whole product without cloud credentials",
            "docker compose + uv · same adapters as AWS where an emulator exists")
    p.icon("dev", "client", "Browser<br>localhost:5173", 60, 400)
    p.dgroup("dc", "docker compose (ports bound to 127.0.0.1)", 240, 150, 1180, 760)
    p.icon("web", "container_1", "web<br>React + Vite (nginx)", 320, 400, "dc")
    p.icon("api", "container_1", "api · FastAPI<br>+ dev IdP mounted<br>(ERP_ENVIRONMENT=local only)", 640, 400, "dc", lp="t")
    p.icon("os", "elasticsearch_service", "OpenSearch 3.1<br>(same engine as AWS)", 1040, 240, "dc")
    p.icon("ddb", "dynamodb", "DynamoDB Local<br>(same boto3 adapter)", 1040, 560, "dc")
    p.icon("fs", "documents", "artifacts volume<br>(filesystem ArtifactStore)", 640, 730, "dc", lp="r")
    p.icon("cli", "source_code", "erp-ingest CLI<br>in-process orchestrator<br>(same pipeline steps)", 1620, 400)
    p.icon("data", "documents", "data/synthetic (committed)<br>data/private (gitignored)", 1900, 400)
    p.icon("bed", "bedrock", "optional: live Bedrock<br>(AWS profile)", 1620, 760)
    p.edge("dev", "web")
    p.edge("web", "api", "/api")
    p.edge("api", "os")
    p.edge("api", "ddb", en=(0, 0.5))
    p.edge("api", "fs", exit="b", entry="t")
    p.edge("data", "cli", exit="l", entry="r")
    p.edge("cli", "os", ex=(0, 0.3), en=(1, 0.5))
    p.edge("cli", "ddb", ex=(0, 0.7), en=(1, 0.5))
    p.edge("cli", "bed", "ERP_MODEL_PROVIDER=bedrock", "muted", exit="b", entry="t")
    p.note("<b>Fixture models</b> (hash embeddings, extractive generator, lexical judge) are the default — every output is "
           "labelled <b>FIXTURE — not evidence of model quality</b>.<br><b>Production guard:</b> startup fails if ERP_ENVIRONMENT ≠ local "
           "and any of: dev IdP, fixture models, dev scanner, disabled security flags.", 240, 950, 1180, 100, font=11)
    return p


def langsmith() -> Page:
    p = Page("ls", "LangSmith (not deployed)", 2100, 1000)
    p.title("Optional: self-hosted LangSmith on AWS — NOT DEPLOYED",
            "infra/langsmith · upstream langchain-ai/terraform modules/aws/infra v0.16.97 · separate state · needs an Enterprise license")
    p.icon("app", "fargate", "RAG API / worker<br>tracer adapter", 80, 420)
    p.box("red", "<b>Redaction + bounded queue</b><br>ids · versions · metrics only<br>drop-on-full; never blocks answers", 330, 400, 260, 120, font=11)
    p.group("cloud", "cloud", "AWS Cloud · us-east-2 (planned)", 680, 150, 1360, 760)
    p.group("vpc", "vpc", "Separate VPC (create_vpc = true)", 720, 220, 1280, 650, "cloud")
    p.icon("eks", "eks", "EKS (private API endpoint)<br>LangSmith Helm chart<br>ClickHouse (in-cluster or managed)", 820, 420, "vpc")
    p.icon("rds", "rds", "RDS PostgreSQL<br>(postgres_source = external)", 1300, 280, "vpc", lp="r")
    p.icon("redis", "elasticache", "ElastiCache Redis<br>(redis_source = external)", 1300, 480, "vpc", lp="r")
    p.icon("blob", "s3", "S3 · trace blobs", 1300, 680, "vpc", lp="r")
    p.edge("app", "red")
    p.edge("red", "eks", "HTTPS (planned)", "muted")
    p.edge("eks", "rds", en=(0, 0.5))
    p.edge("eks", "redis")
    p.edge("eks", "blob", en=(0, 0.5))
    return p


def main() -> None:
    diagrams = {
        "01-system-overview": overview,
        "02-query-path": query_path,
        "03-ingestion-pipeline": ingestion,
        "04-network-security": network,
        "05-authorization-enforcement": authorization,
        "06-model-gateway": model_gateway,
        "07-observability-recovery": observability,
        "08-cicd-delivery": cicd,
        "09-local-development": local_dev,
        "10-langsmith-optional": langsmith,
    }
    only = sys.argv[1:]
    for name, fn in diagrams.items():
        if only and name not in only:
            continue
        write(str(OUT / f"{name}.drawio"), fn())
        print("wrote", name)


if __name__ == "__main__":
    main()
