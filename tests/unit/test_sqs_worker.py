import json

from erp_ingestion.sqs_worker import parse_records


def test_parse_pipe_batch_of_s3_events_and_reconcile() -> None:
    s3_event = {
        "version": "0",
        "id": "1f2e3d4c-aaaa-bbbb-cccc-1234567890ab",
        "source": "aws.s3",
        "detail-type": "Object Created",
        "detail": {
            "bucket": {"name": "erp-dev-source-1"},
            "object": {"key": "hr/policy.pdf", "version-id": "v9"},
        },
    }
    deleted = {
        **s3_event,
        "detail-type": "Object Deleted",
        "detail": {"bucket": {"name": "erp-dev-source-1"}, "object": {"key": "hr/old.pdf"}},
    }
    records = [
        {"body": json.dumps(s3_event)},
        {"body": json.dumps(deleted)},
        {"body": json.dumps({"kind": "reconcile", "source": "s3-erp-dev-source-1"})},
        {"body": "not json"},
        {"body": json.dumps({"source": "aws.ec2"})},
    ]
    items = parse_records(records)
    assert items[0] == {
        "kind": "upsert",
        "bucket": "erp-dev-source-1",
        "key": "hr/policy.pdf",
        "revision": "v9",
        "trace_id": "1f2e3d4caaaabbbbcccc1234567890ab",
    }
    assert items[1]["kind"] == "delete" and items[1]["key"] == "hr/old.pdf"
    assert items[2] == {"kind": "reconcile", "source": "s3-erp-dev-source-1"}
    assert len(items) == 3


def test_parse_replay_command() -> None:
    body = {"kind": "replay", "tenant_id": "t1", "document_id": "doc_1", "actor": "erin"}
    assert parse_records([{"body": json.dumps(body)}]) == [
        {"kind": "replay", "tenant_id": "t1", "document_id": "doc_1", "actor": "erin"}
    ]
