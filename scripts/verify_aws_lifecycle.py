"""LIVE ingestion-lifecycle verification against a deployed environment (SYNTHETIC data only).

Drives the real event path (S3 → EventBridge → SQS → Pipe → Step Functions → worker) and checks the
authoritative DynamoDB records. Writes var/verification/lifecycle-<env>.json.

Usage: uv run python scripts/verify_aws_lifecycle.py --env dev
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3

from erp_evals import fixtures

TENANT = "11111111-1111-4111-8111-111111111111"


def doc_id(source: str, key: str) -> str:
    return "doc_" + hashlib.sha256("\x1f".join([TENANT, source, key]).encode()).hexdigest()[:32]


class Lifecycle:
    def __init__(self, session: boto3.Session, account: str, env: str) -> None:
        n = f"erp-{env}"
        self.s3, self.ddb, self.sfn = (
            session.client("s3"),
            session.client("dynamodb"),
            session.client("stepfunctions"),
        )
        self.bucket = f"{n}-source-{account}"
        self.source = f"s3-{self.bucket}"
        self.docs_table, self.manifest_table = f"{n}-documents", f"{n}-chunk_manifest"
        self.sm = f"arn:aws:states:{session.region_name}:{account}:stateMachine:{n}-ingestion"
        self.results: list[dict[str, Any]] = []

    def put(self, key: str, data: bytes | str) -> float:
        body = data.encode() if isinstance(data, str) else data
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=body, ServerSideEncryption="aws:kms")
        return time.time()

    def record(self, key: str) -> dict[str, Any] | None:
        item = self.ddb.get_item(
            TableName=self.docs_table,
            ConsistentRead=True,
            Key={"tenant_id": {"S": TENANT}, "document_id": {"S": doc_id(self.source, key)}},
        ).get("Item")
        return item

    def wait_for(self, key: str, pred: Any, timeout: float = 600) -> tuple[dict[str, Any] | None, float]:
        start = time.time()
        while time.time() - start < timeout:
            rec = self.record(key)
            if rec is not None and pred(rec):
                return rec, time.time() - start
            time.sleep(5)
        return self.record(key), time.time() - start

    def ok(self, name: str, passed: bool, evidence: str) -> None:
        self.results.append({"check": name, "status": "PASS" if passed else "FAIL", "evidence": evidence})
        print(f"{'PASS' if passed else 'FAIL'}  {name} — {evidence}")

    def manifest(self, document_id: str, version: str) -> dict[str, Any]:
        item = self.ddb.get_item(
            TableName=self.manifest_table,
            ConsistentRead=True,
            Key={"document_id": {"S": document_id}, "document_version": {"S": version}},
        ).get("Item")
        return json.loads(item["data"]["S"]) if item else {}

    # ------------------------------------------------------------------ scenarios

    def run(self) -> None:
        access = f"tenant_id: {TENANT}\nallowed_groups: [acme-all-staff]\nsensitivity_label: internal\n"
        gen = "verification/generated/"
        self.put(
            gen + "_access.yaml",
            access + 'files:\n  logistics-handbook.pdf: {title: "Logistics Handbook"}\n'
            '  security-awareness.docx: {title: "Security Awareness Policy"}\n'
            '  warranty-scan.pdf: {title: "Warranty Terms (scanned)"}\n',
        )
        t0 = self.put(gen + "logistics-handbook.pdf", fixtures.pdf_with_table())
        self.put(gen + "security-awareness.docx", fixtures.docx_with_table())
        self.put(gen + "warranty-scan.pdf", fixtures.scanned_pdf())
        self.put("verification/quarantine/_access.yaml", access)
        self.put(
            "verification/quarantine/eicar.txt", fixtures.EICAR_TEXT
        )  # file must START with the EICAR string

        published = lambda r: r.get("status", {}).get("S") == "published"  # noqa: E731
        rec, _ = self.wait_for(gen + "logistics-handbook.pdf", published)
        freshness = time.time() - t0  # upload → published (≤5 s polling granularity)
        self.ok(
            "PDF with table parsed + embedded + published",
            rec is not None and published(rec),
            f"ingestion freshness ≈{freshness:.0f}s from upload to searchable",
        )
        rec, _ = self.wait_for(gen + "security-awareness.docx", published)
        self.ok("DOCX parsed + published", rec is not None and published(rec), "status=published")
        rec, _ = self.wait_for(
            gen + "warranty-scan.pdf", lambda r: r.get("status", {}).get("S") in ("published", "quarantined")
        )
        status = rec.get("status", {}).get("S") if rec else None
        cov = (
            self.manifest(doc_id(self.source, gen + "warranty-scan.pdf"), rec["current_version"]["S"]).get(
                "coverage"
            )
            if rec and "current_version" in rec
            else None
        )
        self.ok(
            "Scanned page OCR'd by Textract (no quarantine)",
            status == "published",
            f"status={status}; coverage={json.dumps(cov)[:160] if cov else 'n/a'}",
        )
        rec, _ = self.wait_for(
            "verification/quarantine/eicar.txt", lambda r: r.get("status", {}).get("S") == "quarantined"
        )
        self.ok(
            "ClamAV quarantines EICAR test file (never searchable)",
            rec is not None and rec["status"]["S"] == "quarantined",
            f"status={rec['status']['S'] if rec else None}",
        )

        lc = "verification/lifecycle/"
        self.put(
            lc + "_access.yaml",
            f"tenant_id: {TENANT}\nallowed_groups: [acme-finance]\nsensitivity_label: confidential\n",
        )
        self.put(lc + "rates.md", "# Rates\n\nThe zqverify reimbursement rate is 10 percent.\n")
        v1, _ = self.wait_for(lc + "rates.md", published)
        v1_version = v1["current_version"]["S"] if v1 else None
        self.put(lc + "rates.md", "# Rates\n\nThe zqverify reimbursement rate is 12 percent.\n")
        v2, _ = self.wait_for(
            lc + "rates.md", lambda r: published(r) and r["current_version"]["S"] != v1_version
        )
        did = doc_id(self.source, lc + "rates.md")
        old = self.manifest(did, v1_version) if v1_version else {}
        self.ok(
            "Version replacement: new revision published, old retired",
            v2 is not None and v2["current_version"]["S"] != v1_version and old.get("status") == "retired",
            f"v1={v1_version} → v2={v2['current_version']['S'] if v2 else None}; "
            f"old manifest={old.get('status')}",
        )

        acl_before = int(v2["acl_version"]["N"]) if v2 else -1
        self.put(
            lc + "_access.yaml",
            f"tenant_id: {TENANT}\nallowed_groups: [acme-engineering]\nsensitivity_label: confidential\n",
        )
        # A permission-manifest change triggers a connector sync in the worker.
        rec, _ = self.wait_for(lc + "rates.md", lambda r: int(r["acl_version"]["N"]) > acl_before)
        principals = [p["S"] for p in rec["allowed_principals"]["L"]] if rec else []
        self.ok(
            "Permission-only change: ACL version bumped, grants replaced, content unchanged",
            rec is not None
            and principals == ["group:acme-engineering"]
            and rec["current_version"]["S"] == v2["current_version"]["S"],
            f"acl_version {acl_before}→{rec['acl_version']['N'] if rec else None}; allowed={principals}",
        )

        # Duplicate delivery: re-submit the exact same S3 event through the state machine.
        event = {
            "source": "aws.s3",
            "detail-type": "Object Created",
            "id": "verify-duplicate-0001",
            "detail": {
                "bucket": {"name": self.bucket},
                "object": {"key": lc + "rates.md", "version-id": "duplicate-check"},
            },
        }
        outputs = []
        for _ in range(2):
            ex = self.sfn.start_execution(
                stateMachineArn=self.sm, input=json.dumps([{"body": json.dumps(event)}])
            )
            while True:
                d = self.sfn.describe_execution(executionArn=ex["executionArn"])
                if d["status"] != "RUNNING":
                    outputs.append(json.loads(d.get("output") or "[]"))
                    break
                time.sleep(3)
        outcomes = [o[0]["outcome"] if o else None for o in outputs]
        self.ok(
            "Duplicate event delivery is idempotent",
            outcomes[1] == "duplicate",
            f"first={outcomes[0]}, second={outcomes[1]}",
        )

        self.s3.delete_object(Bucket=self.bucket, Key=lc + "rates.md")
        rec, _ = self.wait_for(lc + "rates.md", lambda r: r.get("status", {}).get("S") == "deleted")
        self.ok(
            "Source deletion tombstones the document (revoked + deleted)",
            rec is not None and rec["status"]["S"] == "deleted" and rec["revoked"]["BOOL"],
            f"status={rec['status']['S'] if rec else None}",
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="dev")
    ap.add_argument("--profile", default="awsnew")
    ap.add_argument("--region", default="us-east-2")
    a = ap.parse_args()
    session = boto3.Session(profile_name=a.profile, region_name=a.region)
    account = session.client("sts").get_caller_identity()["Account"]
    lc = Lifecycle(session, account, a.env)
    lc.run()
    out = Path("var/verification")
    out.mkdir(parents=True, exist_ok=True)
    (out / f"lifecycle-{a.env}.json").write_text(
        json.dumps(
            {"env": a.env, "checked_at": datetime.now(UTC).isoformat(), "results": lc.results}, indent=2
        )
    )
    print(f"\n{sum(r['status'] == 'PASS' for r in lc.results)}/{len(lc.results)} PASS")


if __name__ == "__main__":
    main()
