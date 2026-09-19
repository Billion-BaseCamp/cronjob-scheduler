from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from app.core.config import settings  # sets dummy DATABASE_URL_SYNC first
import nucleus.models  # noqa: F401  register Client, FY, Quarter, portal jobs
from app.core.logger import logger
from app.db.database import check_db_connection, engine
from app.jobs.birthday_reminder_job import setup_birthday_reminder_job
from app.jobs.financial_year_job import (
    setup_financial_year_job,
    start_scheduler,
    stop_scheduler,
)
from app.jobs.quarter_transition_job import setup_quarter_transition_job
from app.portal.worker.loop import WORKER_ID, run_forever
from app.portal.worker.status import poller_status
from app.portal.worker.sweeper import run_forever as run_portal_sweeper

# Uvicorn's dictConfig does not attach a root handler. Portal modules use
# logging.getLogger(__name__) and were falling through to lastResort (WARNING
# only, message-only). Same format as portal-automation-worker.
logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logging.getLogger("botocore").setLevel(logging.WARNING)
logging.getLogger("boto3").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)


async def _start_aa_worker(app: FastAPI) -> None:
    # Imported lazily and only when enabled: app.aa imports
    # nucleus.models.account_aggregator, which does not exist in older pinned
    # nucleus versions. A module-level import would raise ImportError at
    # startup and take down every cron job in this service.
    app.state.aa_tasks = []
    if not (settings.AA_WORKER_ENABLED or settings.AA_SWEEPER_ENABLED):
        return
    try:
        from app.aa.worker import (
            run_sweeper as run_aa_sweeper,
            run_worker as run_aa_worker,
        )
    except ImportError:
        logger.exception(
            "AA worker enabled but its models are unavailable — check the "
            "nucleus pin. Other cron jobs are unaffected."
        )
        return
    if settings.AA_WORKER_ENABLED:
        app.state.aa_tasks.append(
            asyncio.create_task(run_aa_worker(), name="aa-worker")
        )
    if settings.AA_SWEEPER_ENABLED:
        # Internally guarded by a Postgres advisory lock: safe to start on
        # every replica, but only one will actually sweep.
        app.state.aa_tasks.append(
            asyncio.create_task(run_aa_sweeper(), name="aa-sweeper")
        )
    if app.state.aa_tasks:
        logger.info("AA background tasks started: %s", len(app.state.aa_tasks))


async def _stop_aa_worker(app: FastAPI) -> None:
    tasks = getattr(app.state, "aa_tasks", [])
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("AA background tasks stopped")


async def _start_portal_poller(app: FastAPI) -> None:
    app.state.poller_task = None
    app.state.sweeper_task = None
    if not settings.PORTAL_WORKER_ENABLED:
        logger.info("Portal poller off: PORTAL_WORKER_ENABLED is false")
        return
    if not (settings.DOCUMENT_PASSWORD_ENCRYPTION_KEY or "").strip():
        logger.error(
            "DOCUMENT_PASSWORD_ENCRYPTION_KEY is empty — copy the same "
            "Fernet key tax-engine uses into this service .env, then restart. "
            "Poller not started so queued jobs stay queued."
        )
    else:
        if await check_db_connection():
            logger.info("Database connection verified")
        else:
            logger.warning(
                "Database is not reachable — portal poller still starting"
            )
        app.state.poller_task = asyncio.create_task(
            run_forever(), name="portal-worker"
        )
    app.state.sweeper_task = asyncio.create_task(
        run_portal_sweeper(), name="portal-automation-sweeper"
    )


async def _cancel_task(task, label: str) -> None:
    if task is None or task.done():
        return
    logger.info("Shutting down %s", label)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _stop_portal_poller(app: FastAPI) -> None:
    await _cancel_task(getattr(app.state, "poller_task", None), "Playwright poller")
    await _cancel_task(
        getattr(app.state, "sweeper_task", None), "portal automation sweeper"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=" * 60)
    logger.info(f"Starting {settings.APP_NAME} [{settings.APP_ENV}]")
    logger.info("=" * 60)

    try:
        await setup_financial_year_job()
        await setup_quarter_transition_job()
        await setup_birthday_reminder_job()
        start_scheduler()
        logger.success("Cron scheduler started")
        await _start_aa_worker(app)
        await _start_portal_poller(app)
        logger.info("Application ready")
    except Exception as e:
        logger.exception(f"Error during startup: {str(e)}")
        raise

    yield

    logger.info("=" * 60)
    logger.info("Application shutting down...")
    logger.info("=" * 60)
    try:
        await _stop_portal_poller(app)
    except Exception as e:
        logger.exception(f"Error stopping portal poller: {str(e)}")
    try:
        await _stop_aa_worker(app)
    except Exception as e:
        logger.exception(f"Error stopping AA worker: {str(e)}")
    try:
        stop_scheduler()
        logger.success("Scheduler stopped successfully")
    except Exception as e:
        logger.exception(f"Error during shutdown: {str(e)}")
    await engine.dispose()
    logger.info("Shutdown complete")


app = FastAPI(
    title=settings.APP_NAME,
    description=(
        "Background ops worker: calendar crons (FY, quarter, birthday), "
        "Account Aggregator fetch, and on-demand Income Tax portal Playwright poller."
    ),
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
    openapi_url="/openapi.json" if settings.DEBUG else None,
)


@app.get("/")
async def root():
    return {
        "message": settings.APP_NAME,
        "status": "running",
        "description": (
            "Calendar crons plus on-demand portal automation poller "
            "and Account Aggregator worker"
        ),
        "cron_jobs": [
            {
                "name": "Financial Year Creation Job",
                "schedule": "Daily at 12:00 AM Asia/Kolkata"
                + (
                    " (also on startup)"
                    if settings.FINANCIAL_YEAR_RUN_ON_STARTUP
                    else ""
                ),
                "description": "Creates current financial year with 4 quarters for clients",
            },
            {
                "name": "Quarter Transition Job",
                "schedule": "Daily at 12:05 AM Asia/Kolkata",
                "description": "Unlocks current quarter (active), marks previous quarter as completed",
            },
            {
                "name": "RM Birthday Email Job",
                "schedule": "Daily at 06:00 Asia/Kolkata",
                "description": "Emails each RM with today's client/family birthdays via Graph sendMail",
            },
        ],
        "portal_worker": {
            "enabled": settings.PORTAL_WORKER_ENABLED,
            "concurrency": settings.WORKER_CONCURRENCY,
            "note": "Queue poller, not a CronTrigger",
        },
        "aa_worker": {
            "enabled": settings.AA_WORKER_ENABLED,
            "sweeper_enabled": settings.AA_SWEEPER_ENABLED,
        },
    }


@app.get("/health")
async def health_check(request: Request):
    from app.jobs.financial_year_job import scheduler

    jobs_info = []
    for job in scheduler.get_jobs():
        jobs_info.append(
            {
                "id": job.id,
                "name": job.name,
                "next_run": str(job.next_run_time) if job.next_run_time else None,
            }
        )

    db_healthy = await check_db_connection()
    poller = poller_status(getattr(request.app.state, "poller_task", None))
    sweeper = poller_status(getattr(request.app.state, "sweeper_task", None))
    portal_expected = settings.PORTAL_WORKER_ENABLED and bool(
        (settings.DOCUMENT_PASSWORD_ENCRYPTION_KEY or "").strip()
    )
    sweeper_expected = settings.PORTAL_WORKER_ENABLED
    portal_ok = ((not portal_expected) or poller == "running") and (
        (not sweeper_expected) or sweeper == "running"
    )
    status = "healthy" if db_healthy and portal_ok else "degraded"
    return {
        "status": status,
        "app": settings.APP_NAME,
        "environment": settings.APP_ENV,
        "database": "connected" if db_healthy else "disconnected",
        "scheduler_running": scheduler.running,
        "scheduled_jobs": jobs_info,
        "poller": poller,
        "sweeper": sweeper,
        "portal_worker_enabled": settings.PORTAL_WORKER_ENABLED,
        "aa_worker_enabled": settings.AA_WORKER_ENABLED,
        "aa_sweeper_enabled": settings.AA_SWEEPER_ENABLED,
        "worker_id": WORKER_ID,
        "concurrency": settings.WORKER_CONCURRENCY,
    }


@app.get("/health/ping")
async def ping():
    return {"message": "pong"}
