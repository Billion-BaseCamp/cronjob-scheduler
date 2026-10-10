"""Start and finish scheduled notice runs.

Starting a run creates one ``portal_automation_batches`` row (status
``running``) and queues one job per client. A partial unique index allows only
one running scheduled batch per workflow, so two processes or two triggers
cannot start overlapping runs.

The finalizer runs every few minutes. When every job in a batch has settled it
completes the batch; a monthly e-Proceedings batch also writes next month's
weekly list in the same transaction. A batch past its deadline is failed and
its queued jobs are cancelled. A failed monthly batch never replaces the list.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.db.database import AsyncSessionLocal
from app.portal.notice_alerts import CRON_REQUESTED_BY, CRON_SILENT_REQUESTED_BY
from app.portal.notice_schedule import (
    REASON_CARRIED_FORWARD,
    RUN_E_PROCEEDINGS_MONTHLY,
    RUN_E_PROCEEDINGS_WEEKLY,
    RUN_WORKFLOW,
    WAITING_STATUSES,
    JobOutcome,
    action_count_from_result,
    is_settled,
    monthly_run_ok,
    notice_assessment_year,
    select_watchlist,
    summarize,
    weekly_should_skip,
)
from app.portal.store import SCHEDULED_REQUESTED_BY_PATTERN

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
_INSERT_CHUNK = 500


@dataclass(frozen=True)
class StartResult:
    started: bool
    batch_id: Optional[UUID] = None
    enqueued: int = 0
    reason: str = ""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _models():
    from nucleus.models import Client
    from nucleus.models.portal_automation import (
        EProceedingsWatchlist,
        PortalAutomationBatch,
        PortalAutomationJob,
    )

    return Client, EProceedingsWatchlist, PortalAutomationBatch, PortalAutomationJob


def models_available() -> bool:
    """False on a nucleus release without the watchlist and batch run columns."""
    try:
        _, _, batch, _ = _models()
    except ImportError:
        return False
    return hasattr(batch, "run_type")


def _active_client(client_model):
    return or_(client_model.is_active.is_(None), client_model.is_active.is_(True))


def _limit_to_allowlist(stmt, client_model):
    allowlist = settings.NOTICE_CRON_CLIENT_IDS
    if allowlist is None:
        return stmt
    return stmt.where(client_model.id.in_(allowlist))


def _scope_summary() -> dict[str, Any]:
    allowlist = settings.NOTICE_CRON_CLIENT_IDS
    if allowlist is None:
        return {"scope": "all"}
    return {"scope": "allowlist", "allowlist_size": len(allowlist)}


async def _running_batch(db, batch_model, workflow: str):
    stmt = (
        select(batch_model)
        .where(
            batch_model.workflow == workflow,
            batch_model.status == "running",
            batch_model.run_type.isnot(None),
        )
        .with_for_update()
    )
    return (await db.execute(stmt)).scalars().first()


async def _cancel_queued(db, job_model, batch_id: UUID, reason: str, now: datetime) -> int:
    result = await db.execute(
        update(job_model)
        .where(job_model.batch_id == batch_id, job_model.status == "queued")
        .values(
            status="cancelled",
            error_code="BATCH_FAILED",
            error_message=reason,
            worker_id=None,
            completed_at=now,
            updated_at=now,
        )
    )
    return int(result.rowcount or 0)


async def _fail_batch(db, job_model, batch, reason: str, now: datetime) -> None:
    cancelled = await _cancel_queued(db, job_model, batch.id, reason, now)
    batch.status = "failed"
    batch.completed_at = now
    batch.error_message = reason
    batch.summary = {**(batch.summary or {}), "cancelled_queued": cancelled}


def _has_pan(client_model):
    return (
        client_model.pan_number.isnot(None),
        func.btrim(client_model.pan_number) != "",
    )


async def _all_eligible_clients(db, client_model) -> list[UUID]:
    stmt = select(client_model.id).where(
        _active_client(client_model),
        *_has_pan(client_model),
        client_model.it_portal_pass.isnot(None),
        func.btrim(client_model.it_portal_pass) != "",
    )
    stmt = _limit_to_allowlist(stmt, client_model)
    return list((await db.execute(stmt)).scalars().all())


async def _count_no_password(db, client_model) -> int:
    """Clients a monthly run leaves out for lack of a portal password.

    Counted rather than queued: a job would only end as MISSING_PASSWORD and
    email the advisor every month for clients who never gave a password.
    """
    stmt = select(func.count()).select_from(client_model).where(
        _active_client(client_model),
        *_has_pan(client_model),
        or_(
            client_model.it_portal_pass.is_(None),
            func.btrim(client_model.it_portal_pass) == "",
        ),
    )
    stmt = _limit_to_allowlist(stmt, client_model)
    return int((await db.execute(stmt)).scalar_one())


async def _latest_completed_monthly(db, batch_model, *, exclude: Optional[UUID] = None):
    stmt = select(batch_model).where(
        batch_model.run_type == RUN_E_PROCEEDINGS_MONTHLY,
        batch_model.status == "completed",
    )
    if exclude is not None:
        stmt = stmt.where(batch_model.id != exclude)
    stmt = stmt.order_by(batch_model.completed_at.desc().nulls_last()).limit(1)
    return (await db.execute(stmt)).scalars().first()


async def _monthly_list(db, watch_model, batch_id: UUID) -> list[UUID]:
    stmt = select(watch_model.client_id).where(watch_model.batch_id == batch_id)
    return list((await db.execute(stmt)).scalars().all())


async def _weekly_clients(db, client_model, watch_model, batch_model) -> list[UUID]:
    """Latest completed monthly list plus manual entries, active clients only."""
    monthly = await _latest_completed_monthly(db, batch_model)
    sources = [
        select(watch_model.client_id).where(
            watch_model.batch_id.is_(None), watch_model.removed_at.is_(None)
        )
    ]
    if monthly is not None:
        sources.append(
            select(watch_model.client_id).where(watch_model.batch_id == monthly.id)
        )
    ids: set[UUID] = set()
    for stmt in sources:
        ids.update((await db.execute(stmt)).scalars().all())
    if not ids:
        return []
    stmt = select(client_model.id).where(
        client_model.id.in_(ids), _active_client(client_model)
    )
    stmt = _limit_to_allowlist(stmt, client_model)
    return list((await db.execute(stmt)).scalars().all())


async def _supersede_waiting(db, job_model, workflow: str, now: datetime) -> int:
    """A scheduled job still waiting from the last run would block this one."""
    result = await db.execute(
        update(job_model)
        .where(
            job_model.workflow == workflow,
            job_model.status.in_(WAITING_STATUSES),
            func.coalesce(job_model.requested_by_sub, "").like(
                SCHEDULED_REQUESTED_BY_PATTERN
            ),
        )
        .values(
            status="cancelled",
            error_code="SUPERSEDED",
            error_message="Replaced by the next scheduled run.",
            worker_id=None,
            completed_at=now,
            updated_at=now,
        )
    )
    return int(result.rowcount or 0)


async def _enqueue(
    db,
    job_model,
    *,
    batch_id: UUID,
    workflow: str,
    client_ids: list[UUID],
    assessment_year: str,
    requested_by: str,
    run_date: date,
) -> int:
    """Insert one queued job per client; clients with an active job are skipped."""
    queued = 0
    for start in range(0, len(client_ids), _INSERT_CHUNK):
        rows = [
            {
                "id": uuid4(),
                "workflow": workflow,
                "client_id": client_id,
                "assessment_year": assessment_year,
                "batch_id": batch_id,
                "status": "queued",
                "current_step": "LOGIN",
                "attempt": 0,
                "idempotency_key": f"cron:{workflow}:{run_date.isoformat()}:{client_id}",
                "requested_by_sub": requested_by,
            }
            for client_id in client_ids[start : start + _INSERT_CHUNK]
        ]
        stmt = (
            insert(job_model)
            .values(rows)
            .on_conflict_do_nothing()
            .returning(job_model.id)
        )
        queued += len((await db.execute(stmt)).all())
    return queued


async def start_scheduled_run(
    run_type: str,
    *,
    manual: bool = False,
    send_email: bool = True,
    today: Optional[date] = None,
) -> StartResult:
    """Create the batch and queue its jobs. Never waits for the jobs to finish."""
    workflow = RUN_WORKFLOW[run_type]
    now = _now()
    today = today or now.astimezone(IST).date()

    if run_type == RUN_E_PROCEEDINGS_WEEKLY and not manual and weekly_should_skip(today):
        return StartResult(
            False, reason="The monthly run owns the 28th and the night after; weekly skipped."
        )

    client_model, watch_model, batch_model, job_model = _models()
    requested_by = CRON_REQUESTED_BY if send_email else CRON_SILENT_REQUESTED_BY
    assessment_year = notice_assessment_year(today)

    async with AsyncSessionLocal() as db:
        running = await _running_batch(db, batch_model, workflow)
        if running is not None:
            if (
                run_type == RUN_E_PROCEEDINGS_MONTHLY
                and running.run_type == RUN_E_PROCEEDINGS_WEEKLY
            ):
                await _fail_batch(
                    db, job_model, running, "Replaced by the monthly run.", now
                )
                await db.commit()
                logger.info("Weekly batch %s stopped for the monthly run", running.id)
            else:
                reason = f"{running.run_type} batch {running.id} is still running."
                await db.rollback()
                return StartResult(False, reason=reason)

        base = _scope_summary()
        if run_type == RUN_E_PROCEEDINGS_WEEKLY:
            client_ids = await _weekly_clients(db, client_model, watch_model, batch_model)
        else:
            client_ids = await _all_eligible_clients(db, client_model)
            base["skipped_no_password"] = await _count_no_password(db, client_model)

        batch_id = uuid4()
        db.add(
            batch_model(
                id=batch_id,
                workflow=workflow,
                assessment_year=assessment_year,
                requested_by_sub=requested_by,
                items=[],
                run_type=run_type,
                status="running",
                started_at=now,
                summary={**base, "eligible": len(client_ids)},
            )
        )
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            return StartResult(False, reason="Another process already started this run.")

    try:
        async with AsyncSessionLocal() as db:
            superseded = await _supersede_waiting(db, job_model, workflow, now)
            queued = await _enqueue(
                db,
                job_model,
                batch_id=batch_id,
                workflow=workflow,
                client_ids=client_ids,
                assessment_year=assessment_year,
                requested_by=requested_by,
                run_date=today,
            )
            batch = await db.get(batch_model, batch_id)
            batch.summary = {
                **base,
                "eligible": len(client_ids),
                "enqueued": queued,
                "skipped_active": len(client_ids) - queued,
                "superseded_waiting": superseded,
            }
            if queued == 0:
                batch.status = "failed"
                batch.completed_at = _now()
                batch.error_message = "No client checks were queued."
            await db.commit()
    except Exception:
        logger.exception("Queueing %s batch %s failed", run_type, batch_id)
        async with AsyncSessionLocal() as db:
            batch = await db.get(batch_model, batch_id)
            if batch is not None and batch.status == "running":
                await _fail_batch(
                    db, job_model, batch, "Queueing failed; see worker logs.", _now()
                )
                await db.commit()
        return StartResult(False, batch_id=batch_id, reason="Queueing failed.")

    if queued == 0:
        return StartResult(False, batch_id=batch_id, reason="No client checks were queued.")
    logger.info("Started %s batch %s with %d job(s)", run_type, batch_id, queued)
    return StartResult(True, batch_id=batch_id, enqueued=queued)


async def _load_outcomes(db, job_model, batch_id: UUID) -> list[JobOutcome]:
    stmt = select(
        job_model.id,
        job_model.client_id,
        job_model.status,
        job_model.error_code,
        job_model.result["action_required_count"].as_integer(),
    ).where(job_model.batch_id == batch_id)
    rows = (await db.execute(stmt)).all()

    missing = [row[0] for row in rows if row[2] == "completed" and row[4] is None]
    recomputed: dict[UUID, Optional[int]] = {}
    if missing:
        full = await db.execute(
            select(job_model.id, job_model.result).where(job_model.id.in_(missing))
        )
        recomputed = {job_id: action_count_from_result(result) for job_id, result in full.all()}

    return [
        JobOutcome(
            client_id=client_id,
            status=status,
            error_code=error_code,
            job_id=job_id,
            action_required_count=count if count is not None else recomputed.get(job_id),
        )
        for job_id, client_id, status, error_code, count in rows
    ]


async def _write_watchlist(db, watch_model, batch_id: UUID, entries) -> None:
    for start in range(0, len(entries), _INSERT_CHUNK):
        rows = [
            {
                "id": uuid4(),
                "batch_id": batch_id,
                "client_id": entry.client_id,
                "reason": entry.reason,
                "action_required_count": entry.action_required_count,
                "job_id": entry.job_id,
            }
            for entry in entries[start : start + _INSERT_CHUNK]
        ]
        await db.execute(insert(watch_model).values(rows))


async def _finalize_one(batch_id: UUID, now: datetime) -> Optional[str]:
    _, watch_model, batch_model, job_model = _models()
    async with AsyncSessionLocal() as db:
        batch = (
            await db.execute(
                select(batch_model)
                .where(batch_model.id == batch_id, batch_model.status == "running")
                .with_for_update(skip_locked=True)
            )
        ).scalars().first()
        if batch is None:
            return None

        counts: dict[str, Any] = dict(
            (
                await db.execute(
                    select(job_model.status, func.count())
                    .where(job_model.batch_id == batch_id)
                    .group_by(job_model.status)
                )
            ).all()
        )
        queueing_done = "enqueued" in (batch.summary or {})
        if not (queueing_done and is_settled(counts)):
            started = batch.started_at or batch.created_at
            deadline = timedelta(hours=settings.NOTICE_BATCH_DEADLINE_HOURS)
            if started is not None and now - started > deadline:
                await _fail_batch(
                    db,
                    job_model,
                    batch,
                    f"Not finished after {settings.NOTICE_BATCH_DEADLINE_HOURS} hours.",
                    now,
                )
                batch.summary = {**(batch.summary or {}), "status_counts": counts}
                await db.commit()
                return f"{batch.run_type} {batch_id}: failed (deadline)"
            await db.rollback()
            return None

        outcomes = await _load_outcomes(db, job_model, batch_id)
        summary = {**(batch.summary or {}), **summarize(outcomes), "status_counts": counts}

        if batch.run_type == RUN_E_PROCEEDINGS_MONTHLY:
            ok, reason = monthly_run_ok(
                summary, settings.E_PROCEEDINGS_MONTHLY_MIN_SUCCESS_RATIO
            )
            if not ok:
                batch.status = "failed"
                batch.completed_at = now
                batch.error_message = reason
                batch.summary = summary
                await db.commit()
                return f"{batch.run_type} {batch_id}: failed ({reason})"
            previous = await _latest_completed_monthly(db, batch_model, exclude=batch_id)
            previous_ids = (
                await _monthly_list(db, watch_model, previous.id) if previous else []
            )
            entries = select_watchlist(outcomes, previous_ids)
            await _write_watchlist(db, watch_model, batch_id, entries)
            summary["watchlist"] = len(entries)
            summary["carried_forward"] = sum(
                1 for entry in entries if entry.reason == REASON_CARRIED_FORWARD
            )
            summary["previous_batch_id"] = str(previous.id) if previous else None

        batch.status = "completed"
        batch.completed_at = now
        batch.error_message = None
        batch.summary = summary
        await db.commit()
        return f"{batch.run_type} {batch_id}: completed"


async def finalize_running_batches() -> list[str]:
    """Complete settled batches and fail ones past the deadline."""
    _, _, batch_model, _ = _models()
    async with AsyncSessionLocal() as db:
        ids = list(
            (
                await db.execute(
                    select(batch_model.id).where(
                        batch_model.status == "running",
                        batch_model.run_type.isnot(None),
                    )
                )
            ).scalars().all()
        )
    done: list[str] = []
    now = _now()
    for batch_id in ids:
        try:
            message = await _finalize_one(batch_id, now)
        except Exception:
            logger.exception("Finalizing notice batch %s failed", batch_id)
            continue
        if message:
            logger.info("Notice batch %s", message)
            done.append(message)
    return done
