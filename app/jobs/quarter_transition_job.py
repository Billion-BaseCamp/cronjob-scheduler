"""
Quarter Transition Cron Job
Runs on a schedule to unlock the current quarter and mark the previous quarter as completed.
"""
from apscheduler.triggers.cron import CronTrigger

from app.db.database import AsyncSessionLocal
from app.service.quarter_transition import transition_quarters_to_current_state
from app.core.logger import logger, log_job_start, log_job_end

# Reuse the same scheduler as financial year job
from app.jobs.financial_year_job import scheduler


async def quarter_transition_job():
    """
    Cron job: unlock current quarter (is_locked=False, status=active),
    set previous quarter status to completed. Idempotent and transactional.
    """
    job_name = "Quarter Transition Job"
    log_job_start(job_name)

    try:
        async with AsyncSessionLocal() as db:
            result = await transition_quarters_to_current_state(db)

        if result["status"] == "success":
            logger.info(f"Job completed: {result['message']}")
            logger.info(
                f"Current quarter FY {result['current_fy_q'][0]} Q{result['current_fy_q'][1]}: "
                f"{result['current_quarters_updated']} quarter(s) set to active/unlocked"
            )
            logger.info(
                f"Previous quarter FY {result['previous_fy_q'][0]} Q{result['previous_fy_q'][1]}: "
                f"{result['previous_quarters_updated']} quarter(s) set to completed"
            )
            log_job_end(job_name, success=True)
        else:
            logger.error(f"Job failed: {result['message']}")
            log_job_end(job_name, success=False)

    except Exception as e:
        logger.exception(f"Error in {job_name}: {str(e)}")
        log_job_end(job_name, success=False)
        raise


async def setup_quarter_transition_job():
    """Register the quarter transition cron job with the scheduler."""
    logger.info("Setting up Quarter Transition cron job...")

    scheduler.add_job(
        quarter_transition_job,
        trigger=CronTrigger( minute='*/1'),  # Daily at 00:05 (after financial year job)
        id="quarter_transition_job",
        name="Quarter Transition Job",
        replace_existing=True,
        max_instances=1,
    )

    logger.success("Scheduled: Quarter Transition Job (Daily at 00:05)")
