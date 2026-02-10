"""
Financial Year Creation Cron Job
Runs daily at midnight to create financial years for clients
"""
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.db.database import AsyncSessionLocal
from app.service.financial_year import create_financial_years_for_all_clients
from app.core.logger import logger, log_job_start, log_job_end


async def financial_year_creation_job():
    """
    Cron job to create financial years for all clients without current FY
    Runs daily at midnight
    """
    job_name = "Financial Year Creation Job"
    log_job_start(job_name)
    
    try:
        # Create database session
        async with AsyncSessionLocal() as db:
            # Run the financial year creation
            result = await create_financial_years_for_all_clients(db)
            
            logger.info(f"Job completed: {result['message']}")
            logger.info(f"Clients processed: {result['clients_processed']}")
            logger.info(f"Financial years created: {result['financial_years_created']}")
            logger.info(f"Quarters created: {result['quarters_created']}")
            
            if result.get('failed_count', 0) > 0:
                logger.warning(f"Failed clients: {result.get('failed_clients')}")
                log_job_end(job_name, success=False)
            else:
                log_job_end(job_name, success=True)
                
    except Exception as e:
        logger.exception(f"Error in {job_name}: {str(e)}")
        log_job_end(job_name, success=False)
        raise


# Initialize AsyncIO scheduler (supports async jobs directly)
scheduler = AsyncIOScheduler()


async def setup_financial_year_job():
    """Setup the financial year creation cron job"""
    logger.info("Setting up Financial Year Creation cron job...")
    

    logger.info("Running initial Financial Year creation job...")
    await financial_year_creation_job()
    
    scheduler.add_job(
        financial_year_creation_job,  # Async function directly!
        trigger=CronTrigger(minute='*/1'),  # Every 1 minut
        id="financial_year_creation_job",
        name="Financial Year Creation Job",
        replace_existing=True,
        max_instances=1  
    )

    logger.success("Scheduled: Financial Year Creation Job (Daily at midnight)")


def start_scheduler():
    """Start the scheduler"""
    if not scheduler.running:
        scheduler.start()
        logger.success("Scheduler started successfully")
    else:
        logger.info("Scheduler already running")


def stop_scheduler():
    """Stop the scheduler"""
    if scheduler.running:
        scheduler.shutdown()
        logger.info("Scheduler stopped")

