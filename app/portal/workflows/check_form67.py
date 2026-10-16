"""CHECK_FORM67: Form 67 scrape. Sets form67_filed true or false."""

from __future__ import annotations

from app.portal.workflows.check_form67_job import run_check_form67_job

WORKFLOW = "CHECK_FORM67"


async def run_check_form67(db, job) -> None:
    await run_check_form67_job(db, job, workflow=WORKFLOW)
