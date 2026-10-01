"""AWS ingestion worker: Step Functions task-token consumer.

Flow: S3 → EventBridge → SQS → Pipe → Step Functions → ``sqs:sendMessage.waitForTaskToken`` → THIS worker.
Each message carries ``taskToken`` + the Pipe batch (``records``). The worker maps records to
``ChangeEvent``s, runs the checkpointed pipeline, heartbeats while working, and reports
``SendTaskSuccess``/``SendTaskFailure`` (error ``Ingestion.Retryable`` triggers the state machine retry).

Live status: implemented-not-live-tested (mapping is unit-tested).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

from erp_connectors.base import ChangeEvent
from erp_connectors.s3 import S3Connector
from erp_ingestion.pipeline import IngestionPipeline, IngestResult
from erp_ingestion.wiring import build_pipeline
from erp_observability.tracing import trace_context
from erp_rag.config import Settings
from erp_rag.runtime import build_core

log = logging.getLogger("erp.worker")


def parse_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract EventBridge/S3 events or reconcile commands from a Pipe batch (list of SQS records)."""
    out: list[dict[str, Any]] = []
    for record in records:
        body = record.get("body", record)
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except ValueError:
                continue
        if not isinstance(body, dict):
            continue
        if body.get("kind") == "reconcile":
            out.append({"kind": "reconcile", "source": body.get("source")})
            continue
        if body.get("kind") == "replay":
            out.append(
                {
                    "kind": "replay",
                    "tenant_id": body.get("tenant_id"),
                    "document_id": body.get("document_id"),
                    "actor": body.get("actor", "admin"),
                }
            )
            continue
        if body.get("source") == "aws.s3" and body.get("detail-type") in ("Object Created", "Object Deleted"):
            detail = body.get("detail", {})
            out.append(
                {
                    "kind": "upsert" if body["detail-type"] == "Object Created" else "delete",
                    "bucket": detail.get("bucket", {}).get("name"),
                    "key": detail.get("object", {}).get("key"),
                    "revision": detail.get("object", {}).get("version-id")
                    or detail.get("object", {}).get("etag"),
                    "trace_id": body.get("id", "").replace("-", "")[:32] or None,
                }
            )
    return out


async def process(pipeline: IngestionPipeline, item: dict[str, Any]) -> list[IngestResult]:
    if item["kind"] == "replay":
        return [
            await pipeline.replay_document(
                str(item["tenant_id"]), str(item["document_id"]), actor=str(item.get("actor"))
            )
        ]
    if item["kind"] == "reconcile":
        name = str(item.get("source"))
        results = await pipeline.sync(name)  # detects permission-file changes, new/changed/deleted objects
        return results
    connector = pipeline.connectors.get(f"s3-{item['bucket']}")
    if not isinstance(connector, S3Connector) or not item.get("key"):
        raise ValueError("event for an unconfigured source")
    key: str = item["key"]
    if key.endswith("/_access.yaml") or key == "_access.yaml":
        return await pipeline.sync(connector.name)  # permission manifest changed → permission events
    ref = await connector.ref_for_key(key)
    event = ChangeEvent(
        kind=item["kind"],
        ref=ref,
        source_revision=str(item.get("revision") or "unknown"),
        detected_at=datetime.now(UTC),
        trace_id=item.get("trace_id"),
    )
    return [await pipeline.handle(event)]


async def run_forever() -> None:
    import boto3

    settings = Settings(service_role="worker")
    queue_url = os.environ["ERP_WORKER_QUEUE_URL"]
    sqs = boto3.client("sqs", region_name=settings.aws_region)
    sfn = boto3.client("stepfunctions", region_name=settings.aws_region)
    core = await build_core(settings)
    api_role = os.environ.get("ERP_OPENSEARCH_API_ROLE_ARN")
    if api_role:
        from erp_ingestion.opensearch_security import ensure_api_role_mapping

        await ensure_api_role_mapping(
            core.os_client, api_role_arn=api_role, index_pattern=f"{core.index.name}*"
        )
        log.info("opensearch FGAC role mapping ensured for the API role")
    pipeline = build_pipeline(core)
    log.info("worker started")
    while True:
        resp = await asyncio.to_thread(
            sqs.receive_message,
            QueueUrl=queue_url,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=20,
            VisibilityTimeout=900,
        )
        for message in resp.get("Messages", []):
            body = json.loads(message["Body"])
            token = body["taskToken"]

            async def heartbeat(tok: str = token) -> None:
                while True:
                    await asyncio.sleep(240)
                    await asyncio.to_thread(sfn.send_task_heartbeat, taskToken=tok)

            hb = asyncio.create_task(heartbeat())
            try:
                results: list[IngestResult] = []
                for item in parse_records(body.get("records", [])):
                    with trace_context(item.get("trace_id")):
                        results += await process(pipeline, item)
                failed = [r for r in results if r.outcome == "failed"]
                if failed:
                    await asyncio.to_thread(
                        sfn.send_task_failure,
                        taskToken=token,
                        error="Ingestion.Retryable",
                        cause=f"{len(failed)} event(s) failed; see events table",
                    )
                else:
                    summary = json.dumps(
                        [{"outcome": r.outcome, "document_id": r.document_id} for r in results]
                    )
                    await asyncio.to_thread(sfn.send_task_success, taskToken=token, output=summary[:250000])
            except Exception as exc:
                log.warning("task failed: %s", type(exc).__name__)
                await asyncio.to_thread(
                    sfn.send_task_failure,
                    taskToken=token,
                    error="Ingestion.Retryable",
                    cause=type(exc).__name__,
                )
            finally:
                hb.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await hb
            await asyncio.to_thread(
                sqs.delete_message, QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"]
            )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
