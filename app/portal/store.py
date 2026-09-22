from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from nucleus.models import Client
from nucleus.models.portal_automation import PortalAutomationJob
from sqlalchemy import exists, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def claim_next_queued(
    db: AsyncSession, worker_id: str
) -> PortalAutomationJob | None:
    """Claim the oldest queued job that is safe to open a portal session for.

    Skips clients that already have a ``running`` job so two Chromes never share
    a PAN (Dual Login). Uses a transaction advisory lock per client so two
    slots cannot claim two queued jobs for the same client in parallel.
    """
    running_clients = (
        select(PortalAutomationJob.client_id)
        .where(PortalAutomationJob.status == "running")
        .distinct()
    )
    stmt = (
        select(PortalAutomationJob)
        .where(
            PortalAutomationJob.status == "queued",
            PortalAutomationJob.client_id.notin_(running_clients),
        )
        .order_by(PortalAutomationJob.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    row = (await db.execute(stmt)).scalars().first()
    if row is None:
        return None

    # Serialize claims for this client across worker slots / processes.
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:cid))"),
        {"cid": str(row.client_id)},
    )
    still_busy = await db.scalar(
        select(
            exists().where(
                PortalAutomationJob.client_id == row.client_id,
                PortalAutomationJob.status == "running",
                PortalAutomationJob.id != row.id,
            )
        )
    )
    if still_busy:
        await db.rollback()
        return None

    now = _now()
    row.status = "running"
    row.started_at = now
    row.updated_at = now
    row.worker_id = worker_id
    row.attempt = (row.attempt or 0) + 1
    row.waiting_since = None
    # Commit before Playwright so heartbeat/sweeper can see ``running`` and
    # the FOR UPDATE lock is not held across the browser session.
    await db.commit()
    await db.refresh(row)
    return row


async def touch_running_heartbeat(db: AsyncSession, job_id: UUID) -> None:
    await db.execute(
        update(PortalAutomationJob)
        .where(
            PortalAutomationJob.id == job_id,
            PortalAutomationJob.status == "running",
        )
        .values(updated_at=_now())
    )


async def get_job(db: AsyncSession, job_id: UUID) -> PortalAutomationJob | None:
    stmt = select(PortalAutomationJob).where(PortalAutomationJob.id == job_id)
    return (await db.execute(stmt)).scalars().first()


async def get_client(db: AsyncSession, client_id: UUID) -> Client | None:
    return await db.get(Client, client_id)


async def list_stale_running(
    db: AsyncSession, cutoff: datetime
) -> list[PortalAutomationJob]:
    heartbeat = func.coalesce(
        PortalAutomationJob.updated_at, PortalAutomationJob.started_at
    )
    stmt = select(PortalAutomationJob).where(
        PortalAutomationJob.status == "running",
        heartbeat.is_not(None),
        heartbeat < cutoff,
    )
    return list((await db.execute(stmt)).scalars().all())
