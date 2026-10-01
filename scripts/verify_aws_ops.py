"""LIVE operational receipts for the dev stack (read-only; nothing is modified).

Collects: alarm → SNS action history, AWS Backup job outcomes, CloudTrail logging status, and an
audit-log scan that counts events and checks for token/secret/document-text leakage.

Usage: AWS_PROFILE=awsnew uv run python scripts/verify_aws_ops.py --env dev
Writes var/verification/ops-<env>.json.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3

LEAK_PATTERNS = {
    "jwt": re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."),
    "bearer": re.compile(r"Bearer\s+[A-Za-z0-9._-]{20,}", re.I),
    "aws_secret_key": re.compile(r"(?<![A-Za-z0-9/+=])[A-Za-z0-9/+=]{40}(?![A-Za-z0-9/+=])"),
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}


def corpus_sentences(root: Path) -> list[str]:
    """Distinctive synthetic-corpus sentences; finding one in the audit log would be a content leak."""
    out: list[str] = []
    for p in sorted(root.rglob("*")):
        if p.suffix.lower() in {".md", ".txt", ".html"} and p.is_file():
            for raw in p.read_text(errors="ignore").splitlines():
                line = re.sub(r"<[^>]+>", "", raw).strip()
                if len(line) >= 60:
                    out.append(line[:60])
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="dev")
    ap.add_argument("--region", default="us-east-2")
    a = ap.parse_args()
    name = f"erp-{a.env}"
    s = boto3.session.Session(region_name=a.region)
    results: list[dict[str, str]] = []

    def ok(check: str, passed: bool, evidence: str) -> None:
        results.append({"check": check, "status": "PASS" if passed else "FAIL", "evidence": evidence})
        print(f"{'PASS' if passed else 'FAIL'}  {check} — {evidence}")

    cw = s.client("cloudwatch")
    alarms = [m["AlarmName"] for m in cw.describe_alarms(AlarmNamePrefix=name)["MetricAlarms"]]
    actions = []
    for al in alarms:
        history = cw.describe_alarm_history(AlarmName=al, HistoryItemType="Action", MaxRecords=10)
        for h in history["AlarmHistoryItems"]:
            actions.append((h["Timestamp"], al, h["HistorySummary"]))
    actions.sort(reverse=True)
    succeeded = [x for x in actions if "Successfully executed action" in x[2]]
    ok(
        "Alarm action delivered to SNS",
        bool(succeeded),
        f"{len(alarms)} alarms; {len(succeeded)} successful SNS actions; latest "
        + (f"{succeeded[0][1]} at {succeeded[0][0].isoformat()}" if succeeded else "none"),
    )

    bk = s.client("backup")
    jobs = bk.list_backup_jobs(ByCreatedAfter=datetime.now(UTC) - timedelta(days=14))["BackupJobs"]
    by_state: dict[str, int] = {}
    for j in jobs:
        by_state[j["State"]] = by_state.get(j["State"], 0) + 1
    types = sorted({j["ResourceType"] for j in jobs if j["State"] == "COMPLETED"})
    ok(
        "AWS Backup jobs completed (last 14 days)",
        by_state.get("COMPLETED", 0) > 0 and by_state.get("FAILED", 0) == 0,
        f"states={by_state}; completed resource types={types}",
    )

    ct = s.client("cloudtrail")
    trails = [t for t in ct.describe_trails()["trailList"] if t["Name"].startswith(name)]
    if trails:
        st = ct.get_trail_status(Name=trails[0]["TrailARN"])
        last = st.get("LatestDeliveryTime")
        ok(
            "CloudTrail logging and delivering",
            bool(st.get("IsLogging")) and last is not None and not st.get("LatestDeliveryError"),
            f"IsLogging={st.get('IsLogging')}; latest delivery={last.isoformat() if last else None}; "
            f"delivery error={st.get('LatestDeliveryError') or 'none'}",
        )
    else:
        ok("CloudTrail logging and delivering", False, "trail not found")

    logs = s.client("logs")
    group = f"/{name}/security-audit"
    events: list[str] = []
    kinds: dict[str, int] = {}
    for page in logs.get_paginator("filter_log_events").paginate(logGroupName=group):
        for e in page["events"]:
            events.append(e["message"])
            try:
                k = json.loads(e["message"]).get("action", "?")
            except ValueError:
                k = "unparsed"
            kinds[k] = kinds.get(k, 0) + 1
    hits = {k: sum(1 for m in events if p.search(m)) for k, p in LEAK_PATTERNS.items()}
    sentences = corpus_sentences(Path("data/synthetic"))
    content_hits = sum(1 for m in events for snt in sentences if snt in m)
    ok(
        "Audit log populated without secret or document-text leakage",
        bool(events) and not any(hits.values()) and content_hits == 0,
        f"{len(events)} events; kinds={dict(sorted(kinds.items()))}; pattern hits={hits}; "
        f"corpus-sentence hits={content_hits} (checked {len(sentences)} sentences)",
    )

    out = Path("var/verification")
    out.mkdir(parents=True, exist_ok=True)
    (out / f"ops-{a.env}.json").write_text(
        json.dumps({"env": a.env, "checked_at": datetime.now(UTC).isoformat(), "results": results}, indent=2)
    )
    print(f"\n{sum(r['status'] == 'PASS' for r in results)}/{len(results)} PASS")


if __name__ == "__main__":
    main()
