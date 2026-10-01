"""DynamoDB-backed authoritative state.

The same code runs against DynamoDB Local (development/tests) and AWS DynamoDB. boto3 calls run in a
worker thread so the event loop is never blocked.
"""

from __future__ import annotations

import asyncio
import functools
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, TypeVar

import boto3
from boto3.dynamodb.conditions import Key
from botocore.config import Config
from botocore.exceptions import ClientError

from erp_auth.models import DocPermissionRecord

T = TypeVar("T")

TABLES: dict[str, tuple[str, str | None]] = {
    # logical name: (partition key, sort key)
    "documents": ("tenant_id", "document_id"),
    "chunk_manifest": ("document_id", "document_version"),
    "workflow": ("run_id", "step"),
    "conversations": ("conversation_key", "turn"),
    "answer_cache": ("cache_key", None),
    "feedback": ("tenant_user", "feedback_id"),
    "usage": ("trace_id", "record_id"),
    "events": ("event_id", None),
    "connector_state": ("connector_id", None),
}


def _to_ddb(value: Any) -> Any:
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, frozenset | set):
        return sorted(_to_ddb(v) for v in value)
    if isinstance(value, list | tuple):
        return [_to_ddb(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_ddb(v) for k, v in value.items() if v is not None}
    return value


def _from_ddb(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, list):
        return [_from_ddb(v) for v in value]
    if isinstance(value, dict):
        return {k: _from_ddb(v) for k, v in value.items()}
    return value


class Dynamo:
    def __init__(self, *, prefix: str, region: str, endpoint_url: str | None) -> None:
        kwargs: dict[str, Any] = {
            "region_name": region,
            "config": Config(
                retries={"max_attempts": 4, "mode": "standard"}, connect_timeout=3, read_timeout=10
            ),
        }
        if endpoint_url:
            # DynamoDB Local accepts any credentials; never used against AWS.
            kwargs.update(endpoint_url=endpoint_url, aws_access_key_id="local", aws_secret_access_key="local")  # noqa: S106
        self._resource = boto3.resource("dynamodb", **kwargs)
        self._client = self._resource.meta.client
        self.prefix = prefix

    def table(self, logical: str) -> Any:
        return self._resource.Table(self.prefix + logical)

    async def run(self, fn: Callable[[], T]) -> T:
        return await asyncio.to_thread(fn)

    def ensure_tables(self) -> None:
        """Create tables if missing (local/test only; AWS tables come from Terraform)."""
        existing = set(self._client.list_tables().get("TableNames", []))
        for logical, (pk, sk) in TABLES.items():
            name = self.prefix + logical
            if name in existing:
                continue
            keys = [{"AttributeName": pk, "KeyType": "HASH"}]
            attrs = [{"AttributeName": pk, "AttributeType": "S"}]
            if sk:
                keys.append({"AttributeName": sk, "KeyType": "RANGE"})
                attrs.append({"AttributeName": sk, "AttributeType": "S"})
            self._client.create_table(
                TableName=name, KeySchema=keys, AttributeDefinitions=attrs, BillingMode="PAY_PER_REQUEST"
            )
        for logical in TABLES:
            self._client.get_waiter("table_exists").wait(TableName=self.prefix + logical)

    def drop_tables(self) -> None:
        existing = set(self._client.list_tables().get("TableNames", []))
        for logical in TABLES:
            name = self.prefix + logical
            if name in existing:
                self._client.delete_table(TableName=name)


# ---------------------------------------------------------------- permission records (authoritative)


def _record_to_item(record: DocPermissionRecord) -> dict[str, Any]:
    item: dict[str, Any] = _to_ddb(record.model_dump())
    return item


def _item_to_record(item: dict[str, Any]) -> DocPermissionRecord:
    data = _from_ddb(item)
    data.pop("updated_at", None)
    data.pop("chunk_count", None)
    return DocPermissionRecord.model_validate(data)


class StaleWriteError(Exception):
    """Conditional write lost a race (another writer advanced the record)."""


class PermissionStore:
    def __init__(self, db: Dynamo) -> None:
        self._db = db
        self._table = db.table("documents")

    async def get(self, tenant_id: str, document_id: str) -> DocPermissionRecord | None:
        def op() -> dict[str, Any] | None:
            resp = self._table.get_item(
                Key={"tenant_id": tenant_id, "document_id": document_id}, ConsistentRead=True
            )
            item: dict[str, Any] | None = resp.get("Item")
            return item

        item = await self._db.run(op)
        return _item_to_record(item) if item else None

    async def get_many(self, tenant_id: str, document_ids: list[str]) -> dict[str, DocPermissionRecord]:
        """Strongly consistent reads (BatchGetItem with ConsistentRead) for the recheck path."""
        unique = sorted(set(document_ids))
        out: dict[str, DocPermissionRecord] = {}
        name = self._table.name

        def op(batch: list[str]) -> list[dict[str, Any]]:
            request: dict[str, Any] = {
                name: {
                    "Keys": [{"tenant_id": tenant_id, "document_id": d} for d in batch],
                    "ConsistentRead": True,
                }
            }
            items: list[dict[str, Any]] = []
            for _ in range(5):
                resp = self._db._resource.batch_get_item(RequestItems=request)
                items.extend(resp.get("Responses", {}).get(name, []))
                request = resp.get("UnprocessedKeys") or {}
                if not request:
                    return items
                time.sleep(0.05)
            raise RuntimeError("unprocessed keys after retries")

        batches = [unique[i : i + 100] for i in range(0, len(unique), 100)]
        results = await asyncio.gather(*(self._db.run(functools.partial(op, b)) for b in batches))
        for items in results:
            for item in items:
                rec = _item_to_record(item)
                out[rec.document_id] = rec
        return out

    async def put(self, record: DocPermissionRecord, *, expected_acl_version: int | None = None) -> None:
        item = _record_to_item(record)
        item["updated_at"] = datetime.now(UTC).isoformat()

        def op() -> None:
            kwargs: dict[str, Any] = {"Item": item}
            if expected_acl_version is not None:
                kwargs["ConditionExpression"] = "attribute_not_exists(document_id) OR acl_version = :v"
                kwargs["ExpressionAttributeValues"] = {":v": expected_acl_version}
            try:
                self._table.put_item(**kwargs)
            except ClientError as exc:
                if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                    raise StaleWriteError(record.document_id) from exc
                raise

        await self._db.run(op)

    async def update_fields(
        self, tenant_id: str, document_id: str, fields: dict[str, Any], *, bump_acl: bool = False
    ) -> DocPermissionRecord:
        """Atomic partial update. ``bump_acl`` increments acl_version (revocation / permission change)."""
        fields = {**fields, "updated_at": datetime.now(UTC)}
        names = {f"#f{i}": k for i, k in enumerate(fields)}
        values = {f":v{i}": _to_ddb(v) for i, v in enumerate(fields.values())}
        sets = [f"#f{i} = :v{i}" for i in range(len(fields))]
        if bump_acl:
            names["#acl"] = "acl_version"
            values[":one"] = 1
            sets.append("#acl = #acl + :one")

        def op() -> dict[str, Any]:
            try:
                resp = self._table.update_item(
                    Key={"tenant_id": tenant_id, "document_id": document_id},
                    UpdateExpression="SET " + ", ".join(sets),
                    ConditionExpression="attribute_exists(document_id)",
                    ExpressionAttributeNames=names,
                    ExpressionAttributeValues=values,
                    ReturnValues="ALL_NEW",
                )
            except ClientError as exc:
                if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                    raise KeyError(document_id) from exc
                raise
            attrs: dict[str, Any] = resp["Attributes"]
            return attrs

        return _item_to_record(await self._db.run(op))

    async def publish_version(
        self, tenant_id: str, document_id: str, *, new_version: str, expected_version: str | None
    ) -> bool:
        """Conditionally flip current_version. Returns False if another writer already moved it."""

        def op() -> bool:
            cond = (
                "current_version = :expected" if expected_version else "attribute_not_exists(current_version)"
            )
            values: dict[str, Any] = {
                ":new": new_version,
                ":pub": "published",
                ":now": datetime.now(UTC).isoformat(),
            }
            if expected_version:
                values[":expected"] = expected_version
            try:
                self._table.update_item(
                    Key={"tenant_id": tenant_id, "document_id": document_id},
                    UpdateExpression="SET current_version = :new, #s = :pub, updated_at = :now",
                    ConditionExpression=f"attribute_exists(document_id) AND ({cond})",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues=values,
                )
            except ClientError as exc:
                if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                    return False
                raise
            return True

        return await self._db.run(op)

    async def list_tenant(self, tenant_id: str) -> list[DocPermissionRecord]:
        def op() -> list[dict[str, Any]]:
            items: list[dict[str, Any]] = []
            kwargs: dict[str, Any] = {"KeyConditionExpression": Key("tenant_id").eq(tenant_id)}
            while True:
                resp = self._table.query(**kwargs)
                items.extend(resp.get("Items", []))
                if "LastEvaluatedKey" not in resp:
                    return items
                kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

        return [_item_to_record(i) for i in await self._db.run(op)]


# ---------------------------------------------------------------- generic JSON item stores


class JsonTable:
    """Small helper for tables whose items are JSON documents."""

    def __init__(self, db: Dynamo, logical: str) -> None:
        self._db = db
        self._table = db.table(logical)
        self.pk, self.sk = TABLES[logical]

    async def put(self, key: dict[str, str], data: dict[str, Any], *, ttl: int | None = None) -> None:
        item: dict[str, Any] = {**key, "data": json.dumps(data, default=str, sort_keys=True)}
        if ttl is not None:
            item["expires_at"] = int(time.time()) + ttl
        await self._db.run(lambda: self._table.put_item(Item=item))

    async def put_if_absent(self, key: dict[str, str], data: dict[str, Any]) -> bool:
        item = {**key, "data": json.dumps(data, default=str, sort_keys=True)}

        def op() -> bool:
            try:
                self._table.put_item(Item=item, ConditionExpression=f"attribute_not_exists({self.pk})")
            except ClientError as exc:
                if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                    return False
                raise
            return True

        return await self._db.run(op)

    async def get(self, key: dict[str, str]) -> dict[str, Any] | None:
        def op() -> dict[str, Any] | None:
            resp = self._table.get_item(Key=key, ConsistentRead=True)
            item: dict[str, Any] | None = resp.get("Item")
            return item

        item = await self._db.run(op)
        if not item:
            return None
        if "expires_at" in item and int(item["expires_at"]) < int(time.time()):
            return None  # DynamoDB TTL deletion is lazy; never serve expired items
        loaded: dict[str, Any] = json.loads(item["data"])
        return loaded

    async def delete(self, key: dict[str, str]) -> None:
        await self._db.run(lambda: self._table.delete_item(Key=key))

    async def query(
        self, pk_value: str, *, limit: int = 100, newest_first: bool = False
    ) -> list[dict[str, Any]]:
        def op() -> list[dict[str, Any]]:
            resp = self._table.query(
                KeyConditionExpression=Key(self.pk).eq(pk_value),
                ScanIndexForward=not newest_first,
                Limit=limit,
                ConsistentRead=True,
            )
            items: list[dict[str, Any]] = resp.get("Items", [])
            return items

        items = await self._db.run(op)
        return [{"_sk": i.get(self.sk) if self.sk else None, **json.loads(i["data"])} for i in items]
