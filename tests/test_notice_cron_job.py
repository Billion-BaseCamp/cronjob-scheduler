"""Scheduler wiring for notice runs. Cron jobs must stay off by default."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("apscheduler")
pytest.importorskip("nucleus.models.portal_automation.watchlist")

from app.core.config import settings
from app.jobs import notice_cron_job
from app.jobs.financial_year_job import scheduler


@pytest.fixture(autouse=True)
def _clean_scheduler():
    for job in scheduler.get_jobs():
        job.remove()
    yield
    for job in scheduler.get_jobs():
        job.remove()


def _ids() -> set[str]:
    return {job.id for job in scheduler.get_jobs()}


def test_crons_are_off_by_default_and_only_the_finalizer_runs(monkeypatch) -> None:
    monkeypatch.setattr(settings, "E_PROCEEDINGS_MONTHLY_CRON_ENABLED", False)
    monkeypatch.setattr(settings, "E_PROCEEDINGS_WEEKLY_CRON_ENABLED", False)
    monkeypatch.setattr(settings, "OUTSTANDING_DEMAND_CRON_ENABLED", False)
    monkeypatch.setattr(settings, "NOTICE_BATCH_FINALIZER_ENABLED", True)
    asyncio.run(notice_cron_job.setup_notice_cron_jobs())
    assert _ids() == {"notice_batch_finalizer"}


def test_schedules_match_the_agreed_days(monkeypatch) -> None:
    monkeypatch.setattr(settings, "E_PROCEEDINGS_MONTHLY_CRON_ENABLED", True)
    monkeypatch.setattr(settings, "E_PROCEEDINGS_WEEKLY_CRON_ENABLED", True)
    monkeypatch.setattr(settings, "OUTSTANDING_DEMAND_CRON_ENABLED", True)
    monkeypatch.setattr(settings, "NOTICE_BATCH_FINALIZER_ENABLED", True)
    asyncio.run(notice_cron_job.setup_notice_cron_jobs())

    jobs = {job.id: job for job in scheduler.get_jobs()}
    fields = lambda job_id: {f.name: str(f) for f in jobs[job_id].trigger.fields}  # noqa: E731

    monthly = fields("notice_cron_e_proceedings_monthly")
    assert (monthly["day"], monthly["hour"], monthly["minute"]) == ("28", "23", "0")
    weekly = fields("notice_cron_e_proceedings_weekly")
    assert (weekly["day_of_week"], weekly["hour"]) == ("mon", "23")
    demand = fields("notice_cron_outstanding_demand_monthly")
    assert (demand["day"], demand["hour"]) == ("27", "23")
    assert str(jobs["notice_cron_e_proceedings_monthly"].trigger.timezone) == "Asia/Kolkata"
