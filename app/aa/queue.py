"""Job claiming and retry bookkeeping for the AA queue.

The claim uses ``FOR UPDATE SKIP LOCKED``, the same pattern as
``claim_next_queued`` in tax-engine-backend's portal automation. Two workers
never receive the same row, and a slow worker never blocks a fast one.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nucleus.models.account_aggregator import (
    AAJob,
    JOB_FAILED,
    JOB_QUEUED,
    JOB_RUNNING,
    JOB_SUCCEEDED,
)

from app.core.config import settings

logger = logging.getLogger(__name__)


async def claim_next_job(db: AsyncSession, worker_id: str) -> Optional[AAJob]:
    """Atomically claim the oldest due job.

    ``scheduled_for <= now()`` is part of the predicate so a job in backoff is
    invisible until its retry falls due — no separate scheduler needed.
    """
    now = datetime.now(timezone.utc)
    stmt = (
        select(AAJob)
        .where(AAJob.status == JOB_QUEUED, AAJob.scheduled_for <= now)
        .order_by(AAJob.scheduled_for.asc(), AAJob.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = (await db.execute(stmt)).scalars().first()
    if job is None:
        return None

    job.status = JOB_RUNNING
    job.worker_id = worker_id
    job.attempt = (job.attempt or 0) + 1
    job.started_at = now
    job.heartbeat_at = now
    await db.commit()
    await db.refresh(job)
    return job


async def mark_succeeded(db: AsyncSession, job: AAJob) -> None:
    job.status = JOB_SUCCEEDED
    job.finished_at = datetime.now(timezone.utc)
    job.error_code = None
    job.error_message = None
    await db.commit()


async def mark_failed(
    db: AsyncSession,
    job: AAJob,
    *,
    error_code: Optional[str],
    error_message: str,
    retryable: bool,
) -> None:
    """Finish a failed job, re-queueing it only when a retry can help.

    Retrying a terminal error — a rejected consent, an expired window — cannot
    succeed. It would hammer the vendor and risk rate-limiting the channel for
    every customer, so terminal failures stop immediately.
    """
    now = datetime.now(timezone.utc)
    job.error_code = error_code
    job.error_message = error_message[:2000]

    exhausted = job.attempt >= (job.max_attempts or settings.AA_FETCH_MAX_ATTEMPTS)
    if not retryable or exhausted:
        job.status = JOB_FAILED
        job.finished_at = now
        logger.error(
            "AA job %s (%s) failed permanently after %s attempt(s): %s",
            job.id,
            job.job_type,
            job.attempt,
            error_message,
        )
    else:
        backoff = settings.AA_FETCH_BACKOFF_BASE_SECONDS * (2 ** (job.attempt - 1))
        job.status = JOB_QUEUED
        job.worker_id = None
        job.started_at = None
        job.scheduled_for = now + timedelta(seconds=backoff)
        logger.warning(
            "AA job %s (%s) attempt %s failed, retrying in %ss: %s",
            job.id,
            job.job_type,
            job.attempt,
            backoff,
            error_message,
        )
    await db.commit()


async def heartbeat(db: AsyncSession, job: AAJob) -> None:
    job.heartbeat_at = datetime.now(timezone.utc)
    await db.commit()


async def requeue_stale_jobs(db: AsyncSession) -> int:
    """Rescue jobs whose worker died mid-run.

    A crashed process leaves its row in ``running`` forever; without this the
    customer's data silently never arrives. Runs under an advisory lock so only
    one instance sweeps.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(
        minutes=settings.AA_STALE_JOB_TIMEOUT_MINUTES
    )
    stmt = select(AAJob).where(
        AAJob.status == JOB_RUNNING,
        AAJob.heartbeat_at.isnot(None),
        AAJob.heartbeat_at < cutoff,
    )
    stale = list((await db.execute(stmt)).scalars().all())
    for job in stale:
        if job.attempt >= (job.max_attempts or settings.AA_FETCH_MAX_ATTEMPTS):
            job.status = JOB_FAILED
            job.finished_at = datetime.now(timezone.utc)
            job.error_code = "STALE"
            job.error_message = "Worker died and retry budget is exhausted"
        else:
            job.status = JOB_QUEUED
            job.worker_id = None
            job.started_at = None
            job.scheduled_for = datetime.now(timezone.utc)
    if stale:
        await db.commit()
        logger.warning("AA sweeper re-queued %s stale job(s)", len(stale))
    return len(stale)
