"""Read-only LIVE posture checks for a deployed environment.

Usage: uv run python scripts/verify_aws_posture.py --env dev [--profile awsnew] [--region us-east-2]
Writes var/verification/posture-<env>.json and prints a PASS/FAIL table. Makes no changes.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3

Result = dict[str, Any]


class Checker:
    def __init__(self, session: boto3.Session, account: str, env: str) -> None:
        self.s, self.account, self.env = session, account, env
        self.name = f"erp-{env}"
        self.results: list[Result] = []

    def c(self, service: str) -> Any:
        return self.s.client(service)

    def check(self, area: str, control: str, fn: Callable[[], tuple[bool, str]]) -> None:
        try:
            ok, evidence = fn()
        except Exception as exc:  # a failed lookup is a failed check, never a silent pass
            ok, evidence = False, f"error: {type(exc).__name__}: {str(exc)[:160]}"
        self.results.append(
            {"area": area, "control": control, "status": "PASS" if ok else "FAIL", "evidence": evidence}
        )

    # ------------------------------------------------------------------ checks

    def run(self) -> list[Result]:
        n, acct = self.name, self.account
        buckets = {
            k: f"{n}-{k}-{acct}" for k in ("source", "artifacts", "config", "web", "cloudtrail", "s3logs")
        }
        s3 = self.c("s3")

        for key, bucket in buckets.items():

            def bpa(b: str = bucket) -> tuple[bool, str]:
                cfg = s3.get_public_access_block(Bucket=b)["PublicAccessBlockConfiguration"]
                return all(cfg.values()), json.dumps(cfg)

            self.check("storage", f"S3 {key}: Block Public Access (all four)", bpa)

            def ver(b: str = bucket) -> tuple[bool, str]:
                st = s3.get_bucket_versioning(Bucket=b).get("Status")
                return st == "Enabled", f"versioning={st}"

            if key != "s3logs":
                self.check("storage", f"S3 {key}: versioning", ver)

            def enc(b: str = bucket, k: str = key) -> tuple[bool, str]:
                rule = s3.get_bucket_encryption(Bucket=b)["ServerSideEncryptionConfiguration"]["Rules"][0]
                algo = rule["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"]
                need_kms = k in ("source", "artifacts", "config")
                return (algo == "aws:kms") if need_kms else algo in ("AES256", "aws:kms"), f"SSE={algo}"

            self.check("storage", f"S3 {key}: default encryption", enc)

        def object_lock() -> tuple[bool, str]:
            cfg = s3.get_object_lock_configuration(Bucket=buckets["artifacts"])["ObjectLockConfiguration"]
            return cfg.get("ObjectLockEnabled") == "Enabled", json.dumps(cfg)

        self.check("storage", "S3 artifacts: Object Lock enabled (legal holds)", object_lock)

        def tls_only() -> tuple[bool, str]:
            pol = json.loads(s3.get_bucket_policy(Bucket=buckets["artifacts"])["Policy"])
            deny = [
                st
                for st in pol["Statement"]
                if st["Effect"] == "Deny" and "aws:SecureTransport" in json.dumps(st)
            ]
            return bool(deny), f"{len(deny)} TLS-deny statement(s)"

        self.check("storage", "S3 artifacts: deny non-TLS requests", tls_only)

        ddb = self.c("dynamodb")
        tables = [t for t in ddb.list_tables()["TableNames"] if t.startswith(f"{n}-")]
        self.check(
            "data", "DynamoDB: 9 application tables exist", lambda: (len(tables) == 9, ", ".join(tables))
        )
        for t in tables:

            def pitr(t: str = t) -> tuple[bool, str]:
                st = ddb.describe_continuous_backups(TableName=t)["ContinuousBackupsDescription"]
                return st["PointInTimeRecoveryDescription"]["PointInTimeRecoveryStatus"] == "ENABLED", "PITR"

            self.check("data", f"DynamoDB {t}: point-in-time recovery", pitr)

            def sse(t: str = t) -> tuple[bool, str]:
                d = ddb.describe_table(TableName=t)["Table"].get("SSEDescription", {})
                return d.get("SSEType") == "KMS" and d.get("Status") == "ENABLED", json.dumps(d)[:120]

            self.check("data", f"DynamoDB {t}: KMS encryption", sse)

        os_ = self.c("opensearch")

        def os_domain() -> tuple[bool, str]:
            d = os_.describe_domain(DomainName=f"{n}-search")["DomainStatus"]
            ok = (
                d["EngineVersion"] == "OpenSearch_3.1"
                and bool(d.get("VPCOptions", {}).get("VPCId"))
                and d["EncryptionAtRestOptions"]["Enabled"]
                and d["NodeToNodeEncryptionOptions"]["Enabled"]
                and d["DomainEndpointOptions"]["EnforceHTTPS"]
                and d["DomainEndpointOptions"]["TLSSecurityPolicy"].startswith("Policy-Min-TLS-1-2")
                and d["AdvancedSecurityOptions"]["Enabled"]
                and not d["AdvancedSecurityOptions"].get("AnonymousAuthEnabled")
            )
            return ok, (
                f"{d['EngineVersion']}, VPC-only, at-rest+node-to-node encryption, HTTPS "
                f"{d['DomainEndpointOptions']['TLSSecurityPolicy']}, FGAC enabled"
            )

        self.check("search", "OpenSearch: pinned 3.1, VPC-only, encrypted, TLS1.2+, FGAC", os_domain)

        kms = self.c("kms")
        for alias in ("data", "logs", "audit"):

            def rot(a: str = alias) -> tuple[bool, str]:
                key_id = kms.describe_key(KeyId=f"alias/{n}-{a}")["KeyMetadata"]["KeyId"]
                return kms.get_key_rotation_status(KeyId=key_id)["KeyRotationEnabled"], f"alias/{n}-{a}"

            self.check("crypto", f"KMS {alias} key: automatic rotation", rot)

        ecs = self.c("ecs")

        def services() -> tuple[bool, str]:
            svcs = ecs.describe_services(cluster=n, services=["api", "worker"])["services"]
            desc = {s["serviceName"]: f"{s['runningCount']}/{s['desiredCount']}" for s in svcs}
            ok = all(s["runningCount"] == s["desiredCount"] and s["desiredCount"] > 0 for s in svcs)
            return ok, json.dumps(desc)

        self.check("compute", "ECS: api and worker services running at desired count", services)

        def task_network() -> tuple[bool, str]:
            ec2 = self.c("ec2")
            arns = ecs.list_tasks(cluster=n)["taskArns"]
            tasks = ecs.describe_tasks(cluster=n, tasks=arns)["tasks"] if arns else []
            enis = [
                d["value"]
                for t in tasks
                for a in t["attachments"]
                for d in a["details"]
                if d["name"] == "networkInterfaceId"
            ]
            public = (
                [
                    e
                    for e in ec2.describe_network_interfaces(NetworkInterfaceIds=enis)["NetworkInterfaces"]
                    if e.get("Association", {}).get("PublicIp")
                ]
                if enis
                else []
            )
            return bool(enis) and not public, f"{len(enis)} task ENIs, {len(public)} with public IPs"

        self.check("network", "ECS tasks: private subnets, no public IPs", task_network)

        def readonly_fs() -> tuple[bool, str]:
            out = []
            for fam in (f"{n}-api", f"{n}-worker"):
                td = ecs.describe_task_definition(taskDefinition=fam)["taskDefinition"]
                app = next(c for c in td["containerDefinitions"] if c["name"] in ("api", "worker"))
                out.append(f"{fam}:readonlyRoot={app.get('readonlyRootFilesystem')},user={app.get('user')}")
            return all("readonlyRoot=True,user=app" in o for o in out), "; ".join(out)

        self.check("compute", "ECS app containers: read-only root FS, non-root user", readonly_fs)

        elb = self.c("elbv2")

        def alb() -> tuple[bool, str]:
            lb = next(
                lb_
                for lb_ in elb.describe_load_balancers()["LoadBalancers"]
                if lb_["LoadBalancerName"].startswith(f"{n}-api")
            )
            tgs = elb.describe_target_groups(LoadBalancerArn=lb["LoadBalancerArn"])["TargetGroups"]
            health = elb.describe_target_health(TargetGroupArn=tgs[0]["TargetGroupArn"])[
                "TargetHealthDescriptions"
            ]
            states = [h["TargetHealth"]["State"] for h in health]
            return lb["Scheme"] == "internal" and states and all(
                s == "healthy" for s in states
            ), f"scheme={lb['Scheme']}, targets={states}"

        self.check("ingress", "ALB: internal-only, API targets healthy", alb)

        waf_r = self.c("wafv2")

        def regional_waf() -> tuple[bool, str]:
            lb = next(
                lb_
                for lb_ in elb.describe_load_balancers()["LoadBalancers"]
                if lb_["LoadBalancerName"].startswith(f"{n}-api")
            )
            acl = waf_r.get_web_acl_for_resource(ResourceArn=lb["LoadBalancerArn"]).get("WebACL")
            return bool(acl), f"web ACL={acl['Name'] if acl else None}"

        self.check("ingress", "WAF (regional) associated with ALB", regional_waf)

        cf = self.c("cloudfront")

        def cloudfront() -> tuple[bool, str]:
            dist = next(
                d
                for d in cf.list_distributions()["DistributionList"]["Items"]
                if any(o["Id"] == "api" for o in d["Origins"]["Items"]) and n in json.dumps(d["Origins"])
            )
            beh = [b for b in dist["CacheBehaviors"].get("Items", [])]
            ok = (
                bool(dist.get("WebACLId"))
                and dist["DefaultCacheBehavior"]["ViewerProtocolPolicy"] == "redirect-to-https"
                and all(b["ViewerProtocolPolicy"] == "https-only" for b in beh)
                and any(
                    "VpcOriginConfig" in o and o["VpcOriginConfig"].get("VpcOriginId")
                    for o in dist["Origins"]["Items"]
                )
            )
            return ok, f"{dist['DomainName']}: WAF attached, HTTPS enforced, API via VPC origin"

        self.check("ingress", "CloudFront: WAF, HTTPS-only, private VPC origin", cloudfront)

        sqs = self.c("sqs")
        for q in ("ingest-events", "worker-tasks"):

            def queue(q: str = q) -> tuple[bool, str]:
                url = sqs.get_queue_url(QueueName=f"{n}-{q}")["QueueUrl"]
                a = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["All"])["Attributes"]
                return bool(a.get("KmsMasterKeyId")) and "deadLetterTargetArn" in a.get(
                    "RedrivePolicy", ""
                ), "KMS + DLQ"

            self.check("ingestion", f"SQS {q}: KMS-encrypted with DLQ", queue)

        sfn = self.c("stepfunctions")

        def state_machine() -> tuple[bool, str]:
            arn = f"arn:aws:states:{self.s.region_name}:{self.account}:stateMachine:{n}-ingestion"
            d = sfn.describe_state_machine(stateMachineArn=arn)
            return d["status"] == "ACTIVE" and d["tracingConfiguration"]["enabled"] and d[
                "loggingConfiguration"
            ]["level"] != "OFF", "ACTIVE, X-Ray tracing, logging"

        self.check("ingestion", "Step Functions: active, traced, logged", state_machine)

        ct = self.c("cloudtrail")

        def trail() -> tuple[bool, str]:
            t = ct.describe_trails(trailNameList=[n])["trailList"][0]
            st = ct.get_trail_status(Name=n)
            sels = ct.get_event_selectors(TrailName=n).get("AdvancedEventSelectors", [])
            data = any("Data" in json.dumps(s) for s in sels)
            ok = (
                st["IsLogging"]
                and t["IsMultiRegionTrail"]
                and t["LogFileValidationEnabled"]
                and bool(t.get("KmsKeyId"))
                and data
            )
            return ok, "logging, multi-region, log-file validation, KMS, S3 data events on document buckets"

        self.check("audit", "CloudTrail: logging + validation + KMS + data events", trail)

        logs = self.c("logs")

        def audit_group() -> tuple[bool, str]:
            g = logs.describe_log_groups(logGroupNamePrefix=f"/{n}/security-audit")["logGroups"][0]
            return bool(g.get("kmsKeyId")) and g.get(
                "retentionInDays", 0
            ) >= 2555, f"retention={g.get('retentionInDays')}d, KMS"

        self.check("audit", "Security audit log group: dedicated KMS key, ≥7y retention", audit_group)

        bk = self.c("backup")

        def backup() -> tuple[bool, str]:
            plan = next(
                p for p in bk.list_backup_plans()["BackupPlansList"] if p["BackupPlanName"] == f"{n}-daily"
            )
            sel = bk.list_backup_selections(BackupPlanId=plan["BackupPlanId"])["BackupSelectionsList"]
            return bool(sel), f"plan {plan['BackupPlanName']} with {len(sel)} selection(s)"

        self.check("recovery", "AWS Backup: daily plan with selection", backup)

        ecr = self.c("ecr")

        def ecr_repo() -> tuple[bool, str]:
            r = ecr.describe_repositories(repositoryNames=[f"{n}/app"])["repositories"][0]
            return (
                r["imageTagMutability"] == "IMMUTABLE"
                and r["imageScanningConfiguration"]["scanOnPush"]
                and r["encryptionConfiguration"]["encryptionType"] == "KMS"
            ), "immutable tags, scan on push, KMS"

        self.check("supply-chain", "ECR: immutable tags, scan on push, KMS", ecr_repo)

        def image_scan() -> tuple[bool, str]:
            # Multi-arch tags point at an OCI index; scans live on the per-architecture manifests.
            imgs = ecr.describe_images(repositoryName=f"{n}/app", filter={"tagStatus": "TAGGED"})[
                "imageDetails"
            ]
            latest = max(imgs, key=lambda i: i["imagePushedAt"])
            index = json.loads(
                ecr.batch_get_image(
                    repositoryName=f"{n}/app", imageIds=[{"imageDigest": latest["imageDigest"]}]
                )["images"][0]["imageManifest"]
            )
            per_arch = [
                m
                for m in index.get("manifests", [])
                if m.get("annotations", {}).get("vnd.docker.reference.type") != "attestation-manifest"
            ]
            summary = []
            ok = bool(per_arch)
            for m in per_arch:
                f = ecr.describe_image_scan_findings(
                    repositoryName=f"{n}/app", imageId={"imageDigest": m["digest"]}
                )
                status = f["imageScanStatus"]["status"]
                counts = f.get("imageScanFindings", {}).get("findingSeverityCounts", {})
                ok = ok and status == "COMPLETE" and counts.get("CRITICAL", 0) == 0
                summary.append(f"{m['platform']['architecture']}: {status} {counts}")
            return ok, f"{latest.get('imageTags')}: " + "; ".join(summary)

        self.check("supply-chain", "ECR image scan: no CRITICAL findings", image_scan)

        iam = self.c("iam")

        def task_roles() -> tuple[bool, str]:
            wild = []
            for role in iam.list_roles()["Roles"]:
                if not role["RoleName"].startswith((f"{n}-api-", f"{n}-worker-")):
                    continue
                for pname in iam.list_role_policies(RoleName=role["RoleName"])["PolicyNames"]:
                    doc = iam.get_role_policy(RoleName=role["RoleName"], PolicyName=pname)["PolicyDocument"]
                    for st in doc["Statement"]:
                        acts = st["Action"] if isinstance(st["Action"], list) else [st["Action"]]
                        if any(a == "*" or a.endswith(":*") for a in acts):
                            wild.append(f"{role['RoleName']}:{st.get('Sid')}")
            return not wild, "no wildcard actions in task-role policies" if not wild else f"wildcards: {wild}"

        self.check("iam", "Task roles: no wildcard actions", task_roles)

        def bedrock_scope() -> tuple[bool, str]:
            for role in iam.list_roles()["Roles"]:
                if role["RoleName"].startswith(f"{n}-api-"):
                    doc = iam.get_role_policy(
                        RoleName=role["RoleName"],
                        PolicyName=iam.list_role_policies(RoleName=role["RoleName"])["PolicyNames"][0],
                    )["PolicyDocument"]
                    st = next(s for s in doc["Statement"] if s.get("Sid") == "BedrockApprovedModelsOnly")
                    res = st["Resource"] if isinstance(st["Resource"], list) else [st["Resource"]]
                    return all(
                        "nova" in r or "titan" in r for r in res
                    ) and "*" not in res, f"{len(res)} model/profile ARNs"
            return False, "api role not found"

        self.check("iam", "Bedrock access limited to approved model ARNs", bedrock_scope)
        return self.results


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="dev")
    ap.add_argument("--profile", default="awsnew")
    ap.add_argument("--region", default="us-east-2")
    args = ap.parse_args()
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    account = session.client("sts").get_caller_identity()["Account"]
    results = Checker(session, account, args.env).run()
    out = Path("var/verification")
    out.mkdir(parents=True, exist_ok=True)
    report = {
        "env": args.env,
        "account": account,
        "region": args.region,
        "checked_at": datetime.now(UTC).isoformat(),
        "results": results,
    }
    (out / f"posture-{args.env}.json").write_text(json.dumps(report, indent=2))
    for r in results:
        print(f"{r['status']:4}  [{r['area']}] {r['control']} — {r['evidence']}")
    print(f"\n{sum(r['status'] == 'PASS' for r in results)}/{len(results)} PASS")


if __name__ == "__main__":
    main()
