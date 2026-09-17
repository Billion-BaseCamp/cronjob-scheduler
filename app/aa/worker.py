"""AA worker loops: claim-and-run, plus a stale-job sweeper.

Two loops with deliberately different concurrency rules.

**The claim loop is safe on every instance.** ``FOR UPDATE SKIP LOCKED``
guarantees two workers never take the same row, so running it everywhere simply
adds throughput.

**The sweeper must run on exactly one instance.** It re-queues jobs whose worker
died; if every replica did that simultaneously they would fight over the same
rows and could resurrect a job that is merely slow. It is therefore held behind
a Postgres advisory lock.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid

import redis.asyncio as redis

from app.aa.client import FinsenseClient, FinsenseError
from app.aa.handlers import HANDLERS
from app.aa.queue import (
    claim_next_job,
    mark_failed,
    mark_succeeded,
    requeue_stale_jobs,
)
from app.aa.token import FinsenseTokenManager
from app.core.config import settings
from app.core.locks import AA_SWEEPER_LOCK_KEY, advisory_lock
from app.db.database import AsyncSessionLocal

logger = logging.getLogger(__name__)

WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

_redis_client: redis.Redis | None = None


def _get_client() -> FinsenseClient:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(settings.AA_REDIS_URL, decode_responses=False)
    return FinsenseClient(FinsenseTokenManager(_redis_client))


async def _run_one_job(client: FinsenseClient) -> bool:
    """Claim and execute a single job. Returns True if one was processed."""
    async with AsyncSessionLocal() as db:
        job = await claim_next_job(db, WORKER_ID)
        if job is None:
            return False

        handler = HANDLERS.get(job.job_type)
        if handler is None:
            await mark_failed(
                db,
                job,
                error_code="UNKNOWN_JOB_TYPE",
                error_message=f"no handler for job_type={job.job_type}",
                retryable=False,
            )
            return True

        logger.info("AA job %s (%s) attempt %s started", job.id, job.job_type, job.attempt)
        try:
            await handler(db, client, job)
        except FinsenseError as exc:
            await db.rollback()
            await mark_failed(
                db,
                job,
                error_code=exc.code,
                error_message=str(exc),
                retryable=exc.retryable,
            )
        except Exception as exc:  # noqa: BLE001
            # An unexpected error is treated as retryable: it is more likely a
            # transient fault than a permanent one, and the attempt counter
            # still bounds it.
            await db.rollback()
            logger.exception("AA job %s raised unexpectedly", job.id)
            await mark_failed(
                db,
                job,
                error_code="UNEXPECTED",
                error_message=f"{type(exc).__name__}: {exc}",
                retryable=True,
            )
        else:
            await mark_succeeded(db, job)
        return True


async def run_worker() -> None:
    """Claim-and-run loop. Safe to run on every instance."""
    if not settings.AA_WORKER_ENABLED:
        logger.info("AA_WORKER_ENABLED is false — AA worker not started")
        return

    settings.require_finsense()
    settings.require_aa_encryption_key()

    interval = max(2, settings.AA_WORKER_POLL_INTERVAL_SECONDS)
    logger.info("AA worker started (id=%s, poll=%ss)", WORKER_ID, interval)
    client = _get_client()

    while True:
        try:
            # Drain the queue before sleeping; a burst of consents should not
            # trickle out one per poll interval.
            processed = True
            while processed:
                processed = await _run_one_job(client)
        except asyncio.CancelledError:
            logger.info("AA worker cancelled")
            raise
        except Exception:  # noqa: BLE001
            # Never let the loop die: one bad iteration must not stop every
            # future fetch.
            logger.exception("AA worker iteration failed")
        await asyncio.sleep(interval)


async def run_sweeper() -> None:
    """Re-queue stale jobs. Exactly one instance across the fleet."""
    if not settings.AA_SWEEPER_ENABLED:
        logger.info("AA_SWEEPER_ENABLED is false — AA sweeper not started")
        return

    interval = max(15, settings.AA_SWEEP_INTERVAL_SECONDS)

    async with advisory_lock(AA_SWEEPER_LOCK_KEY, name="aa-sweeper") as acquired:
        if not acquired:
            return
        logger.info("AA sweeper started (interval=%ss)", interval)
        while True:
            try:
                async with AsyncSessionLocal() as db:
                    await requeue_stale_jobs(db)
            except asyncio.CancelledError:
                logger.info("AA sweeper cancelled")
                raise
            except Exception:  # noqa: BLE001
                logger.exception("AA sweeper iteration failed")
            await asyncio.sleep(interval)
