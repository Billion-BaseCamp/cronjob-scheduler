"""Replace the current foreign-assets row for one client and calendar year."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from uuid import UUID

from nucleus.models.portal_automation import (
    ClientForeignAssetYear,
    PortalAutomationJob,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.portal.evidence import _delete_object

logger = logging.getLogger(__name__)

_KEEP_STATUSES = frozenset({"downloaded", "no_data", "year_not_in_dropdown"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def keys_to_replace(
    db: AsyncSession, client_id: UUID, calendar_year: int, keep: str
) -> list[str]:
    """Older S3 keys for this year, including copies stored on past jobs."""
    found: list[str] = []
    current = (
        await db.execute(
            select(ClientForeignAssetYear).where(
                ClientForeignAssetYear.client_id == client_id,
                ClientForeignAssetYear.calendar_year == calendar_year,
            )
        )
    ).scalar_one_or_none()
    if current is not None and current.s3_key and current.s3_key != keep:
        found.append(current.s3_key)

    jobs = (
        (
            await db.execute(
                select(PortalAutomationJob)
                .where(
                    PortalAutomationJob.client_id == client_id,
                    PortalAutomationJob.workflow == "FETCH_FOREIGN_ASSETS",
                )
                .order_by(PortalAutomationJob.created_at.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    for job in jobs:
        result = job.result if isinstance(job.result, dict) else {}
        years = result.get("years")
        if not isinstance(years, list):
            continue
        for item in years:
            if not isinstance(item, dict):
                continue
            try:
                year = int(item.get("calendar_year"))
            except (TypeError, ValueError):
                continue
            key = item.get("s3_key")
            if year == calendar_year and isinstance(key, str) and key and key != keep:
                found.append(key)
    unique: list[str] = []
    seen: set[str] = set()
    for key in found:
        if key in seen:
            continue
        seen.add(key)
        unique.append(key)
    return unique


async def drop_replaced_keys(keys: list[str]) -> None:
    bucket = (settings.S3_BUCKET_NAME or "").strip()
    if not bucket:
        return
    for key in keys:
        if ".." in key or not key.startswith("portal-automation/"):
            continue
        try:
            await asyncio.to_thread(_delete_object, bucket, key)
        except Exception:
            logger.warning("Could not delete replaced foreign-assets PDF %s", key)


async def remember_foreign_asset_year(
    db: AsyncSession,
    *,
    client_id: UUID,
    calendar_year: int,
    status: str,
    job_id: UUID,
    s3_key: str | None = None,
) -> None:
    """Upsert the year the dashboard shows. A failed download does not replace it."""
    if status not in _KEEP_STATUSES:
        return
    row = (
        await db.execute(
            select(ClientForeignAssetYear).where(
                ClientForeignAssetYear.client_id == client_id,
                ClientForeignAssetYear.calendar_year == calendar_year,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        db.add(
            ClientForeignAssetYear(
                client_id=client_id,
                calendar_year=calendar_year,
            status=status,
            s3_key=s3_key,
            job_id=job_id,
                fetched_at=_now(),
            )
        )
    else:
        row.status = status
        row.s3_key = s3_key
        row.job_id = job_id
        row.fetched_at = _now()
    await db.flush()
