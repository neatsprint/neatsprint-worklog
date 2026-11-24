"""S3 helpers for CSV exports."""

from __future__ import annotations

import uuid
from typing import Any

import boto3  # type: ignore[import-untyped]
from botocore.client import Config  # type: ignore[import-untyped]
from botocore.exceptions import BotoCoreError, ClientError  # type: ignore[import-untyped]
from fastapi import Request
from neatsprint_lib.errors import ApiError


def _client(request: Request, *, public: bool = False) -> Any:
    settings = request.app.state.settings
    if not settings.s3_endpoint or not settings.s3_access_key:
        raise ApiError(503, "dependency_unavailable", "Export storage is not configured.")
    endpoint = (
        settings.s3_public_endpoint
        if public and settings.s3_public_endpoint
        else settings.s3_endpoint
    )
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region,
        config=Config(signature_version="s3v4"),
    )


def upload_csv(request: Request, tenant_id: str, body: bytes) -> str:
    settings = request.app.state.settings
    key = f"tenant/{tenant_id}/exports/timesheet-{uuid.uuid4().hex}.csv"
    try:
        _client(request).put_object(
            Bucket=settings.s3_bucket,
            Key=key,
            Body=body,
            ContentType="text/csv",
        )
    except (BotoCoreError, ClientError) as exc:
        raise ApiError(503, "dependency_unavailable", "Could not store the export.") from exc
    return key


def presign_get(request: Request, key: str) -> str:
    settings = request.app.state.settings
    try:
        return str(
            _client(request, public=True).generate_presigned_url(
                "get_object",
                Params={"Bucket": settings.s3_bucket, "Key": key},
                ExpiresIn=300,
            )
        )
    except (BotoCoreError, ClientError) as exc:
        raise ApiError(503, "dependency_unavailable", "Could not prepare the download.") from exc
