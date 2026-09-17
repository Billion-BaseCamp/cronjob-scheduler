"""Time out portal jobs stuck in ``running`` with no worker heartbeat.

Started from FastAPI lifespan when ``PORTAL_WORKER_ENABLED``. Not a CronTrigger.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.database import AsyncSessionLocal
from app.portal.store import list_stale_running

logger = logging.getLogger("portal_worker")


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def sweep_stale(db: AsyncSession) -> int:
    minutes = settings.PORTAL_AUTOMATION_TIMEOUT_MINUTES
    cutoff = _now() - timedelta(minutes=minutes)
    rows = await list_stale_running(db, cutoff)
    for row in rows:
        row.status = "timed_out"
        row.completed_at = _now()
        row.error_code = "TIMED_OUT"
        row.error_message = (
            f"Timed out after {minutes} min with no worker heartbeat."
        )
        row.worker_id = None
    if rows:
        await db.commit()
        logger.info("Portal automation sweeper timed out %d stuck job(s)", len(rows))
    return len(rows)


async def run_forever() -> None:
    interval = max(15, settings.PORTAL_AUTOMATION_SWEEP_INTERVAL_SECONDS)
    logger.info(
        "Portal automation sweeper started (interval=%ss, timeout=%smin)",
        interval,
        settings.PORTAL_AUTOMATION_TIMEOUT_MINUTES,
    )
    while True:
        try:
            async with AsyncSessionLocal() as session:
                await sweep_stale(session)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Portal automation sweeper iteration failed")
        await asyncio.sleep(interval)
