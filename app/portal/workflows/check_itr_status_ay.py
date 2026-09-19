"""CHECK_ITR_STATUS_AY: filed-return scrape only — never changes filing_status."""

from __future__ import annotations

from app.portal.workflows.filed_return_status_job import run_filed_return_status_job

WORKFLOW = "CHECK_ITR_STATUS_AY"


async def run_check_itr_status_ay(db, job) -> None:
    await run_filed_return_status_job(
        db,
        job,
        workflow=WORKFLOW,
        promote_e_verified=False,
    )
