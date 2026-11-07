"""Replace the current ITR JSON row for one client and assessment year."""

from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import UUID

from nucleus.models.portal_automation import ClientItrJsonYear
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

_KEEP_STATUSES = frozenset({"downloaded", "no_filing"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_date(value: str | None) -> date | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


async def remember_itr_json_year(
    db: AsyncSession,
    *,
    client_id: UUID,
    assessment_year: str,
    status: str,
    job_id: UUID,
    s3_key: str | None = None,
    itr_form: str | None = None,
    acknowledgement_no: str | None = None,
    filing_section: str | None = None,
    filing_date: str | None = None,
    e_verified_on: str | None = None,
    latest_filing_type: str | None = None,
    latest_filing_date: str | None = None,
    filings_count: int = 0,
    counts_by_filing_type: dict | None = None,
) -> None:
    """Upsert the year the screen shows. A failed download does not replace it."""
    if status not in _KEEP_STATUSES:
        return
    ay = assessment_year.strip()
    row = (
        await db.execute(
            select(ClientItrJsonYear).where(
                ClientItrJsonYear.client_id == client_id,
                ClientItrJsonYear.assessment_year == ay,
            )
        )
    ).scalar_one_or_none()
    values = dict(
        status=status,
        s3_key=s3_key,
        job_id=job_id,
        itr_form=itr_form or None,
        acknowledgement_no=acknowledgement_no or None,
        filing_section=filing_section or None,
        filing_date=_as_date(filing_date),
        e_verified_on=_as_date(e_verified_on),
        latest_filing_type=latest_filing_type or None,
        latest_filing_date=_as_date(latest_filing_date),
        filings_count=filings_count,
        counts_by_filing_type=counts_by_filing_type or {},
        fetched_at=_now(),
    )
    if row is None:
        db.add(
            ClientItrJsonYear(
                client_id=client_id,
                assessment_year=ay,
                **values,
            )
        )
    else:
        for key, value in values.items():
            setattr(row, key, value)
    await db.flush()
