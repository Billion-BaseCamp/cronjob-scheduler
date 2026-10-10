"""CHECK_OUTSTANDING_DEMAND: read-only harvest of Response to Outstanding Demand."""

from __future__ import annotations

from app.portal.workflows.pending_actions_notices_job import (
    run_pending_actions_notices_job,
)

WORKFLOW = "CHECK_OUTSTANDING_DEMAND"
SOURCE = "outstanding_demand"


async def run_check_outstanding_demand(db, job) -> None:
    await run_pending_actions_notices_job(db, job, workflow=WORKFLOW, source=SOURCE)
