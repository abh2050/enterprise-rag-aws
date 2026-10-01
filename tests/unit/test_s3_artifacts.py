import boto3
from botocore.stub import ANY, Stubber

from erp_connectors.artifacts import S3ArtifactStore


async def test_put_always_requests_sse_kms_and_legal_hold() -> None:
    s3 = boto3.client("s3", region_name="us-east-2", aws_access_key_id="x", aws_secret_access_key="x")
    with Stubber(s3) as stub:
        stub.add_response(
            "put_object",
            {},
            {"Bucket": "b", "Key": "landing/a.pdf", "Body": ANY, "ServerSideEncryption": "aws:kms"},
        )
        stub.add_response(
            "put_object",
            {},
            {
                "Bucket": "b",
                "Key": "landing/b.pdf",
                "Body": ANY,
                "ServerSideEncryption": "aws:kms",
                "ObjectLockLegalHoldStatus": "ON",
            },
        )
        store = S3ArtifactStore(s3, "b")
        await store.put("landing/a.pdf", b"x")
        await store.put("landing/b.pdf", b"x", legal_hold=True)
