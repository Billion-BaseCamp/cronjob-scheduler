from fastapi import FastAPI
from contextlib import asynccontextmanager

from app.core.logger import logger
from app.jobs.financial_year_job import setup_financial_year_job, start_scheduler, stop_scheduler


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
        start_scheduler()
        
        logger.success("All cron jobs started successfully")
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
                "schedule": "On startup + Every 1 hour",
                "description": "Creates current financial year with 4 quarters for clients"
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