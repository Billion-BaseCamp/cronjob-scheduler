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
from app.portal.foreign_assets import foreign_assets_pdf_key

logger = logging.getLogger(__name__)

PRIMARY_OBJECT = "evidence"


def evidence_s3_key(job_id: UUID | str) -> str:
    return f"portal-automation/{job_id}/{PRIMARY_OBJECT}.png"


class PdfUploadError(Exception):
    """The foreign-assets PDF did not land in S3. The job must fail."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _put_object(bucket: str, key: str, body: bytes, content_type: str) -> None:
    client = boto3.client("s3", region_name=settings.S3_REGION)
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType=content_type,
        ServerSideEncryption="AES256",
    )


async def upload_screenshot(job_id: UUID | str, step_name: str, png: bytes) -> str | None:
    bucket = (settings.S3_BUCKET_NAME or "").strip()
    if not bucket:
        return None
    key = evidence_s3_key(job_id)
    try:
        await asyncio.to_thread(_put_object, bucket, key, png, "image/png")
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


async def upload_pdf(job_id: UUID | str, calendar_year: int, pdf: bytes) -> str:
    """Store one password-protected foreign-assets PDF. Raises PdfUploadError."""
    bucket = (settings.S3_BUCKET_NAME or "").strip()
    key = foreign_assets_pdf_key(job_id, calendar_year)
    if not bucket:
        raise PdfUploadError("S3 bucket is not configured.")
    try:
        await asyncio.to_thread(_put_object, bucket, key, pdf, "application/pdf")
    except Exception as exc:
        code = ""
        response = getattr(exc, "response", None)
        if isinstance(response, dict):
            code = (response.get("Error") or {}).get("Code") or ""
        if code == "AccessDenied":
            raise PdfUploadError(
                "S3 denied PutObject for the foreign-assets PDF."
            ) from exc
        raise PdfUploadError(
            f"Could not store the C.Y. {calendar_year} PDF."
        ) from exc
    logger.info("Uploaded foreign-assets PDF s3://%s/%s", bucket, key)
    return key
