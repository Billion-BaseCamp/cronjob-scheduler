"""Scheduled notice runs and the batch finalizer.

- e-Proceedings monthly: 28th at 23:00 IST, every client; builds the weekly list.
- e-Proceedings weekly: Mondays at 23:00 IST, the latest completed list only.
- Outstanding demand monthly: 27th at 23:00 IST, every client.
- Finalizer: every few minutes, completes or fails running batches.

Start one run by hand (ignores the cron flags, still refuses to overlap):

    python -m app.jobs.notice_cron_job e_proceedings_monthly [--no-email]
    python -m app.jobs.notice_cron_job finalize
"""

from __future__ import annotations

import argparse
import asyncio

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.core.config import settings
from app.core.logger import logger
from app.jobs.financial_year_job import scheduler
from app.portal.notice_schedule import (
    CRON_HOUR,
    E_PROCEEDINGS_MONTHLY_DAY,
    OUTSTANDING_DEMAND_MONTHLY_DAY,
    RUN_E_PROCEEDINGS_MONTHLY,
    RUN_E_PROCEEDINGS_WEEKLY,
    RUN_OUTSTANDING_DEMAND_MONTHLY,
    RUN_WORKFLOW,
)

_TZ = "Asia/Kolkata"


async def run_scheduled(run_type: str) -> None:
    from app.portal.notice_cron import start_scheduled_run

    try:
        result = await start_scheduled_run(run_type)
    except Exception:
        logger.exception(f"Scheduled notice run {run_type} crashed")
        return
    if result.started:
        logger.info(
            f"Scheduled notice run {run_type}: batch {result.batch_id}, "
            f"{result.enqueued} job(s) queued"
        )
    else:
        logger.info(f"Scheduled notice run {run_type} not started: {result.reason}")


async def finalize_batches() -> None:
    from app.portal.notice_cron import finalize_running_batches

    try:
        await finalize_running_batches()
    except Exception:
        logger.exception("Notice batch finalizer failed")


def _add_cron(run_type: str, trigger: CronTrigger, label: str) -> None:
    scheduler.add_job(
        run_scheduled,
        trigger=trigger,
        args=[run_type],
        id=f"notice_cron_{run_type}",
        name=label,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    logger.success(f"Scheduled: {label}")


async def setup_notice_cron_jobs() -> None:
    wanted = (
        settings.E_PROCEEDINGS_MONTHLY_CRON_ENABLED,
        settings.E_PROCEEDINGS_WEEKLY_CRON_ENABLED,
        settings.OUTSTANDING_DEMAND_CRON_ENABLED,
        settings.NOTICE_BATCH_FINALIZER_ENABLED,
    )
    if not any(wanted):
        logger.info("Notice crons and finalizer are off")
        return

    from app.portal.notice_cron import models_available

    if not models_available():
        logger.error(
            "Notice crons not scheduled: the installed nucleus has no watchlist "
            "or batch run columns. Bump the nucleus pin."
        )
        return

    if settings.E_PROCEEDINGS_MONTHLY_CRON_ENABLED:
        _add_cron(
            RUN_E_PROCEEDINGS_MONTHLY,
            CronTrigger(day=E_PROCEEDINGS_MONTHLY_DAY, hour=CRON_HOUR, minute=0, timezone=_TZ),
            f"e-Proceedings monthly ({E_PROCEEDINGS_MONTHLY_DAY}th at {CRON_HOUR}:00 IST)",
        )
    if settings.E_PROCEEDINGS_WEEKLY_CRON_ENABLED:
        _add_cron(
            RUN_E_PROCEEDINGS_WEEKLY,
            CronTrigger(day_of_week="mon", hour=CRON_HOUR, minute=0, timezone=_TZ),
            f"e-Proceedings weekly (Mondays at {CRON_HOUR}:00 IST)",
        )
    if settings.OUTSTANDING_DEMAND_CRON_ENABLED:
        _add_cron(
            RUN_OUTSTANDING_DEMAND_MONTHLY,
            CronTrigger(
                day=OUTSTANDING_DEMAND_MONTHLY_DAY, hour=CRON_HOUR, minute=0, timezone=_TZ
            ),
            f"Outstanding demand monthly ({OUTSTANDING_DEMAND_MONTHLY_DAY}th at {CRON_HOUR}:00 IST)",
        )
    if settings.NOTICE_BATCH_FINALIZER_ENABLED:
        scheduler.add_job(
            finalize_batches,
            trigger=IntervalTrigger(minutes=settings.NOTICE_BATCH_FINALIZER_MINUTES),
            id="notice_batch_finalizer",
            name="Notice batch finalizer",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger.success(
            f"Scheduled: Notice batch finalizer (every {settings.NOTICE_BATCH_FINALIZER_MINUTES} min)"
        )


async def _cli(command: str, send_email: bool) -> int:
    import nucleus.models  # noqa: F401  register mappers

    from app.db.database import engine
    from app.portal.notice_cron import finalize_running_batches, start_scheduled_run

    try:
        if command == "finalize":
            for message in await finalize_running_batches():
                print(message)
            return 0
        result = await start_scheduled_run(command, manual=True, send_email=send_email)
        if result.started:
            print(f"Started batch {result.batch_id} with {result.enqueued} job(s).")
            return 0
        print(f"Not started: {result.reason}")
        return 1
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start one scheduled notice run now.")
    parser.add_argument("command", choices=sorted(RUN_WORKFLOW) + ["finalize"])
    parser.add_argument(
        "--no-email",
        action="store_true",
        help="Queue the jobs without advisor emails (for a first, checked run).",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_cli(args.command, send_email=not args.no_email))


if __name__ == "__main__":
    raise SystemExit(main())
