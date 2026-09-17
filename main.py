from fastapi import FastAPI
from contextlib import asynccontextmanager
import asyncio

import nucleus.models  # noqa: F401  register Client, FY, Quarter, and related mappers

from app.core.config import settings
from app.core.logger import logger
from app.jobs.birthday_reminder_job import setup_birthday_reminder_job
from app.jobs.financial_year_job import setup_financial_year_job, start_scheduler, stop_scheduler
from app.jobs.quarter_transition_job import setup_quarter_transition_job


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan - startup and shutdown events"""
    # Startup
    logger.info("="*60)
    logger.info("Application starting up...")
    logger.info("="*60)
    
    try:
        # Setup and start cron jobs
        await setup_financial_year_job()
        await setup_quarter_transition_job()
        await setup_birthday_reminder_job()
        start_scheduler()
        
        logger.success("All cron jobs started successfully")

        # Account Aggregator background loops. Imported lazily and only when
        # enabled: app.aa imports nucleus.models.account_aggregator, which does
        # not exist in older pinned nucleus versions. A module-level import
        # would raise ImportError at startup and take down every cron job in
        # this service — financial year, quarter transition, birthday emails —
        # on any deployment whose nucleus pin predates the AA models.
        app.state.aa_tasks = []
        if settings.AA_WORKER_ENABLED or settings.AA_SWEEPER_ENABLED:
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
            else:
                if settings.AA_WORKER_ENABLED:
                    app.state.aa_tasks.append(
                        asyncio.create_task(run_aa_worker(), name="aa-worker")
                    )
                if settings.AA_SWEEPER_ENABLED:
                    # Internally guarded by a Postgres advisory lock: safe to
                    # start on every replica, but only one will actually sweep.
                    app.state.aa_tasks.append(
                        asyncio.create_task(run_aa_sweeper(), name="aa-sweeper")
                    )
        if app.state.aa_tasks:
            logger.info(f"AA background tasks started: {len(app.state.aa_tasks)}")

        logger.info("Application ready!")
        
    except Exception as e:
        logger.exception(f"Error during startup: {str(e)}")
        raise
    
    yield
    
    # Shutdown
    logger.info("="*60)
    logger.info("Application shutting down...")
    logger.info("="*60)
    
    try:
        stop_scheduler()
        logger.success("Scheduler stopped successfully")
    except Exception as e:
        logger.exception(f"Error during shutdown: {str(e)}")

    # Cancel the AA loops and wait for them, so an in-flight job finishes its
    # bookkeeping rather than being orphaned in `running` for the sweeper to
    # rescue later.
    for task in getattr(app.state, "aa_tasks", []):
        task.cancel()
    if getattr(app.state, "aa_tasks", []):
        await asyncio.gather(*app.state.aa_tasks, return_exceptions=True)
        logger.info("AA background tasks stopped")


app = FastAPI(
    title="BBC Advance Tax Cron Job",
    description="Financial Year and Quarter Management System - Cron Job Only",
    version="1.0.0",
    lifespan=lifespan
)


@app.get("/")
async def root():
    """Root endpoint"""
    logger.info("Health check endpoint called")
    return {
        "message": "BBC Advance Tax Cron Job System",
        "status": "running",
        "description": "Automated financial year and quarter creation",
        "cron_jobs": [
            {
                "name": "Financial Year Creation Job",
                "schedule": "On startup + Daily at 12:00 AM Asia/Kolkata",
                "description": "Creates current financial year with 4 quarters for clients"
            },
            {
                "name": "Quarter Transition Job",
                "schedule": "Daily at 12:05 AM Asia/Kolkata",
                "description": "Unlocks current quarter (active), marks previous quarter as completed"
            },
            {
                "name": "RM Birthday Email Job",
                "schedule": "Daily at 06:00 Asia/Kolkata",
                "description": "Emails each RM with today's client/family birthdays via Graph sendMail"
            }
        ]
    }


@app.get("/health")
async def health_check():
    """Detailed health check"""
    from app.jobs.financial_year_job import scheduler
    
    jobs_info = []
    for job in scheduler.get_jobs():
        jobs_info.append({
            "id": job.id,
            "name": job.name,
            "next_run": str(job.next_run_time) if job.next_run_time else None
        })
    
    return {
        "status": "healthy",
        "scheduler_running": scheduler.running,
        "scheduled_jobs": jobs_info
    }