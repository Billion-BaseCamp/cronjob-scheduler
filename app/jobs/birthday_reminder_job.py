"""
RM birthday reminder emails at 11:45 IST.
"""
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.logger import logger, log_job_start, log_job_end
from app.db.database import AsyncSessionLocal
from app.jobs.financial_year_job import scheduler
from app.service.birthday import process_birthday_emails_for_day


async def birthday_reminder_job():
    job_name = "RM Birthday Email Job"
    if not settings.BIRTHDAY_EMAIL_JOB_ENABLED:
        logger.info("Skipping RM Birthday Email Job: BIRTHDAY_EMAIL_JOB_ENABLED is false")
        return

    log_job_start(job_name)
    try:
        async with AsyncSessionLocal() as db:
            await process_birthday_emails_for_day(db)
        log_job_end(job_name, success=True)
    except Exception as e:
        logger.exception(f"Error in {job_name}: {str(e)}")
        log_job_end(job_name, success=False)
        raise


async def setup_birthday_reminder_job():
    logger.info("Setting up RM Birthday Email cron job...")

    if not settings.BIRTHDAY_EMAIL_JOB_ENABLED:
        logger.info("RM Birthday Email Job disabled: BIRTHDAY_EMAIL_JOB_ENABLED is false")
        return

    scheduler.add_job(
        birthday_reminder_job,
        trigger=CronTrigger(hour=11, minute=45, timezone="Asia/Kolkata"),
        id="birthday_reminder_ist_1145",
        name="RM Birthday Email Job",
        replace_existing=True,
        max_instances=1,
    )

    logger.success("Scheduled: RM Birthday Email Job (Daily at 11:45 Asia/Kolkata)")
