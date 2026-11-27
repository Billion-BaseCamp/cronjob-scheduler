"""Store Form 168 / ATS .txt into ``advance_tax_26as_uploads`` (file metadata only).

Replaces the S3 object pointer + ``content_hash`` on the quarter's upload row.
Never deletes Part1–10 — Sync in advance-tax owns parsing and part refresh.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.portal.form_168_txt import current_quarter_number, db_financial_year
from nucleus.models.advance_tax_models.advance_tax_26as_upload import AdvanceTax26asUpload
from nucleus.models.advance_tax_models.financial_year import FinancialYear
from nucleus.models.advance_tax_models.quarter import Quarter

logger = logging.getLogger(__name__)


class Form168StoreError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def resolve_fy_and_current_quarter(
    db: AsyncSession,
    *,
    client_id: UUID,
    tax_year: str,
) -> tuple[FinancialYear, Quarter]:
    fy_label = db_financial_year(tax_year)
    fy = (
        await db.execute(
            select(FinancialYear).where(
                FinancialYear.client_id == client_id,
                FinancialYear.financial_year == fy_label,
            )
        )
    ).scalar_one_or_none()
    if fy is None:
        raise Form168StoreError(
            "MISSING_FINANCIAL_YEAR",
            f"Financial year '{fy_label}' not found for this client.",
        )

    q_num = current_quarter_number()
    quarter = (
        await db.execute(
            select(Quarter).where(
                Quarter.financial_year_id == fy.id,
                Quarter.quarter_number == q_num,
            )
        )
    ).scalar_one_or_none()
    if quarter is None:
        raise Form168StoreError(
            "MISSING_QUARTER",
            f"Quarter Q{q_num} not found for financial year '{fy_label}'.",
        )
    return fy, quarter


async def remember_form_168_upload(
    db: AsyncSession,
    *,
    client_id: UUID,
    tax_year: str,
    pan: str,
    s3_key: str,
    bucket_name: str,
    original_filename: str,
    file_size_bytes: int,
    upload_id: UUID,
    content_hash: str,
) -> AdvanceTax26asUpload:
    """Upsert file metadata for the current quarter without touching Part1–10.

    - Existing row: update S3 pointer / hash / filename in place (same ``id``).
    - New row: insert with ``parse_status=pending``.
    - If the file hash changed on an existing row, set ``parse_status=pending`` so
      Sync UI can prompt a refresh; parsed parts are left intact.
    """
    fy, quarter = await resolve_fy_and_current_quarter(
        db, client_id=client_id, tax_year=tax_year
    )

    digest = (content_hash or "").strip().lower()
    if len(digest) != 64:
        raise Form168StoreError(
            "INVALID_CONTENT_HASH",
            "content_hash must be a 64-character SHA-256 hex digest.",
        )

    existing = (
        await db.execute(
            select(AdvanceTax26asUpload)
            .where(AdvanceTax26asUpload.quarter_id == quarter.id)
            .with_for_update()
        )
    ).scalar_one_or_none()

    if existing is not None:
        prev_hash = (existing.content_hash or "").strip().lower() or None
        existing.s3_key = s3_key
        existing.bucket_name = bucket_name
        existing.original_filename = original_filename[:255]
        existing.file_size_bytes = file_size_bytes
        existing.pan = (pan or "")[:10] or None
        existing.content_hash = digest
        existing.uploaded_at = _now()
        if prev_hash != digest:
            existing.parse_status = "pending"
        await db.flush()
        logger.info(
            "Updated Form 168 upload in place upload_id=%s quarter_id=%s hash_changed=%s",
            existing.id,
            quarter.id,
            prev_hash != digest,
        )
        return existing

    upload = AdvanceTax26asUpload(
        id=upload_id,
        client_id=client_id,
        financial_year_id=fy.id,
        quarter_id=quarter.id,
        s3_key=s3_key,
        bucket_name=bucket_name,
        original_filename=original_filename[:255],
        file_size_bytes=file_size_bytes,
        pan=(pan or "")[:10] or None,
        content_hash=digest,
        parse_status="pending",
        uploaded_at=_now(),
    )
    db.add(upload)
    await db.flush()
    return upload
