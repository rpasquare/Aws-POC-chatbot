"""Object storage. Originals, parse output and page images.

Nothing in here returns a durable public URL: every read handed to a browser
is a presigned URL with a short expiry. Requirement 12.2.
"""

from __future__ import annotations

from functools import lru_cache

import boto3
from botocore.client import BaseClient
from botocore.config import Config

from app.config import get_settings


@lru_cache(maxsize=1)
def client() -> BaseClient:
    settings = get_settings()
    return boto3.client(
        "s3",
        region_name=settings.aws_region,
        endpoint_url=settings.s3_endpoint_url,
        config=Config(signature_version="s3v4", retries={"max_attempts": 5, "mode": "standard"}),
    )


def document_prefix(project_id: str, document_id: str) -> str:
    return f"projects/{project_id}/documents/{document_id}"


def original_key(project_id: str, document_id: str) -> str:
    return f"{document_prefix(project_id, document_id)}/original.pdf"


def parsed_key(project_id: str, document_id: str) -> str:
    return f"{document_prefix(project_id, document_id)}/parsed.json"


def page_image_key(project_id: str, document_id: str, page_no: int) -> str:
    return f"{document_prefix(project_id, document_id)}/pages/{page_no}.webp"


def presigned_put(key: str, content_type: str) -> str:
    """Write credential for exactly one key. Requirement 5.2."""
    settings = get_settings()
    return client().generate_presigned_url(
        "put_object",
        Params={"Bucket": settings.s3_bucket, "Key": key, "ContentType": content_type},
        ExpiresIn=settings.upload_url_expiry_seconds,
    )


def presigned_get(key: str, expires_in: int | None = None) -> str:
    settings = get_settings()
    return client().generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.s3_bucket, "Key": key},
        ExpiresIn=expires_in or settings.page_image_url_expiry_seconds,
    )


def head(key: str) -> dict:
    return client().head_object(Bucket=get_settings().s3_bucket, Key=key)


def get_bytes(key: str) -> bytes:
    return client().get_object(Bucket=get_settings().s3_bucket, Key=key)["Body"].read()


def put_bytes(key: str, body: bytes, content_type: str) -> None:
    client().put_object(
        Bucket=get_settings().s3_bucket,
        Key=key,
        Body=body,
        ContentType=content_type,
        ServerSideEncryption="AES256",
    )


def delete_prefix(prefix: str) -> int:
    """Remove every object under a prefix. Used by the purge job."""
    bucket = get_settings().s3_bucket
    paginator = client().get_paginator("list_objects_v2")
    removed = 0
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if keys:
            client().delete_objects(Bucket=bucket, Delete={"Objects": keys})
            removed += len(keys)
    return removed
