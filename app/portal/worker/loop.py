"""Claim queued portal-automation jobs and run registered workflows.

One uvicorn process (--workers 1). Concurrency is N asyncio slots, each with
its own DB session and Chrome. Postgres SKIP LOCKED keeps claims unique.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from datetime import datetime, timezone
from uuid import UUID

from app.core.config import settings
from app.db.database import AsyncSessionLocal
from app.portal.store import claim_next_queued, touch_running_heartbeat
from app.portal.workflows.registry import get_runner

import nucleus.models  # noqa: F401  — register SQLAlchemy mappers
from nucleus.models.portal_automation import PortalAutomationJob

logger = logging.getLogger("portal_worker")

WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"
_stop = asyncio.Event()
_SHUTDOWN_WAIT_SECONDS = 120.0


async def _heartbeat(job_id: UUID, stop: asyncio.Event) -> None:
    interval = max(5.0, settings.WORKER_HEARTBEAT_SECONDS)
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except asyncio.TimeoutError:
            pass
        try:
            async with AsyncSessionLocal() as db:
                await touch_running_heartbeat(db, job_id)
                await db.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Heartbeat failed for job %s", job_id, exc_info=True)


async def process_one(db, worker_id: str) -> bool:
    job = await claim_next_queued(db, worker_id)
    if job is None:
        return False
    runner = get_runner(job.workflow)
    if runner is None:
        job.status = "failed"
        job.current_step = "DISPATCH"
        job.error_code = "UNKNOWN_WORKFLOW"
        job.error_message = f"Unknown workflow {job.workflow!r}"
        job.worker_id = None
        job.completed_at = datetime.now(timezone.utc)
        await db.commit()
        logger.error("Job %s has unknown workflow %s", job.id, job.workflow)
        return True
    job_id = job.id
    stop = asyncio.Event()
    hb = asyncio.create_task(_heartbeat(job_id, stop), name=f"heartbeat-{job_id}")
    try:
        await runner(db, job)
        await db.commit()
    except Exception:
        logger.exception("Job %s failed", job_id)
        await db.rollback()
        async with AsyncSessionLocal() as retry_db:
            claimed = await retry_db.get(PortalAutomationJob, job_id)
            if claimed is not None:
                claimed.status = "failed"
                claimed.error_code = "WORKER_EXCEPTION"
                claimed.error_message = "Worker crashed while processing this job."
                claimed.worker_id = None
                claimed.completed_at = datetime.now(timezone.utc)
                await retry_db.commit()
        return True
    finally:
        stop.set()
        hb.cancel()
        try:
            await hb
        except asyncio.CancelledError:
            pass
    logger.info("Job %s finished status=%s slot=%s", job.id, job.status, worker_id)
    return True


async def _slot(slot: int) -> None:
    worker_id = f"{WORKER_ID}:{slot}"
    logger.info("Portal worker slot %s id=%s", slot, worker_id)
    while not _stop.is_set():
        claimed = False
        try:
            async with AsyncSessionLocal() as db:
                claimed = await process_one(db, worker_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Worker slot %s iteration failed", slot)
        if _stop.is_set():
            return
        if not claimed:
            try:
                await asyncio.wait_for(
                    _stop.wait(), timeout=settings.WORKER_POLL_SECONDS
                )
                return
            except asyncio.TimeoutError:
                pass


async def run_forever() -> None:
    _stop.clear()
    n = settings.WORKER_CONCURRENCY
    logger.info(
        "Portal worker %s concurrency=%s poll=%ss heartbeat=%ss",
        WORKER_ID,
        n,
        settings.WORKER_POLL_SECONDS,
        settings.WORKER_HEARTBEAT_SECONDS,
    )
    tasks = [
        asyncio.create_task(_slot(i), name=f"portal-slot-{i}") for i in range(n)
    ]
    # Do not gather() the slots: cancelling gather cancels Chrome mid-job.
    parked = asyncio.Event()
    try:
        await parked.wait()
    except asyncio.CancelledError:
        _stop.set()
        _, pending = await asyncio.wait(tasks, timeout=_SHUTDOWN_WAIT_SECONDS)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        raise
