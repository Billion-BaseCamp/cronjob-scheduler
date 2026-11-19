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
from app.portal.itr_json import itr_file_s3_key, itr_json_s3_key

logger = logging.getLogger(__name__)

PRIMARY_OBJECT = "evidence"


def evidence_s3_key(job_id: UUID | str) -> str:
    return f"portal-automation/{job_id}/{PRIMARY_OBJECT}.png"


class PdfUploadError(Exception):
    """The foreign-assets PDF did not land in S3. The job must fail."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class JsonUploadError(Exception):
    """The ITR JSON did not land in S3. The job must fail."""

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


def _delete_object(bucket: str, key: str) -> None:
    client = boto3.client("s3", region_name=settings.S3_REGION)
    client.delete_object(Bucket=bucket, Key=key)


async def upload_pdf(
    client_id: UUID | str,
    calendar_year: int,
    pdf: bytes,
    *,
    replace_keys: list[str] | None = None,
) -> str:
    """Store one password-protected foreign-assets PDF. Raises PdfUploadError.

    ``replace_keys`` are older copies of this year. They are removed after the
    new object is stored.
    """
    bucket = (settings.S3_BUCKET_NAME or "").strip()
    key = foreign_assets_pdf_key(client_id, calendar_year)
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
    for old in replace_keys or []:
        if not old or old == key or ".." in old or not old.startswith("portal-automation/"):
            continue
        try:
            await asyncio.to_thread(_delete_object, bucket, old)
        except Exception:
            logger.warning("Could not delete replaced foreign-assets PDF %s", old)
    return key


async def upload_itr_json(
    client_id: UUID | str,
    assessment_year: str,
    payload: bytes,
) -> str:
    """Store one ITR JSON. A later download for the same client and year overwrites it."""
    return await _upload_itr_file(
        assessment_year,
        payload,
        key=itr_json_s3_key(client_id, assessment_year),
        content_type="application/json",
        label="ITR JSON",
    )


async def upload_itr_file(
    client_id: UUID | str,
    assessment_year: str,
    payload: bytes,
    file_format: str,
) -> str:
    """Store the year's ITR file. JSON and XML use separate keys and overwrite themselves."""
    content_type = "application/xml" if file_format == "xml" else "application/json"
    label = "ITR XML" if file_format == "xml" else "ITR JSON"
    return await _upload_itr_file(
        assessment_year,
        payload,
        key=itr_file_s3_key(client_id, assessment_year, file_format),
        content_type=content_type,
        label=label,
    )


async def _upload_itr_file(
    assessment_year: str,
    payload: bytes,
    *,
    key: str,
    content_type: str,
    label: str,
) -> str:
    bucket = (settings.S3_BUCKET_NAME or "").strip()
    if not bucket:
        raise JsonUploadError("S3 bucket is not configured.")
    try:
        await asyncio.to_thread(_put_object, bucket, key, payload, content_type)
    except Exception as exc:
        code = ""
        response = getattr(exc, "response", None)
        if isinstance(response, dict):
            code = (response.get("Error") or {}).get("Code") or ""
        if code == "AccessDenied":
            raise JsonUploadError(f"S3 denied PutObject for the {label}.") from exc
        raise JsonUploadError(
            f"Could not store the A.Y. {assessment_year} {label}."
        ) from exc
    logger.info("Uploaded %s s3://%s/%s", label, bucket, key)
    return key
