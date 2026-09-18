"""Optional screenshot upload for portal-automation jobs.

One primary object per job: ``portal-automation/<job-id>/evidence.png``.
Retries overwrite the same key. The step name lives on ``job.result``.
"""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

import boto3

from app.core.config import settings

logger = logging.getLogger(__name__)

PRIMARY_OBJECT = "evidence"


def evidence_s3_key(job_id: UUID | str) -> str:
    return f"portal-automation/{job_id}/{PRIMARY_OBJECT}.png"


def _put_object(bucket: str, key: str, png: bytes) -> None:
    client = boto3.client("s3", region_name=settings.S3_REGION)
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=png,
        ContentType="image/png",
        ServerSideEncryption="AES256",
    )


async def upload_screenshot(job_id: UUID | str, step_name: str, png: bytes) -> str | None:
    bucket = (settings.S3_BUCKET_NAME or "").strip()
    if not bucket:
        return None
    key = evidence_s3_key(job_id)
    try:
        await asyncio.to_thread(_put_object, bucket, key, png)
    except Exception as exc:
        code = ""
        response = getattr(exc, "response", None)
        if isinstance(response, dict):
            code = (response.get("Error") or {}).get("Code") or ""
        if code == "AccessDenied":
            logger.warning(
                "S3 PutObject denied for s3://%s/%s — grant s3:PutObject on "
                "portal-automation/* to the IAM user/role actually used "
                "(env AWS keys override the instance role)",
                bucket,
                key,
            )
        else:
            logger.warning(
                "Skipping S3 evidence upload (credentials or bucket not configured)",
                exc_info=True,
            )
        return None
    logger.info(
        "Uploaded portal evidence step=%s s3://%s/%s",
        step_name,
        bucket,
        key,
    )
    return key
