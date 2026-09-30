"""CHECK_PENDING_ACTIONS_NOTICES: read-only harvest of e-Proceedings + outstanding demand."""

from __future__ import annotations

from app.portal.workflows.pending_actions_notices_job import (
    run_pending_actions_notices_job,
)

WORKFLOW = "CHECK_PENDING_ACTIONS_NOTICES"
# compliance_portal joins in Phase 3.
NOTICE_SOURCES = ("e_proceedings", "outstanding_demand")


async def run_check_pending_actions_notices(db, job) -> None:
    await run_pending_actions_notices_job(
        db,
        job,
        workflow=WORKFLOW,
        sources=NOTICE_SOURCES,
    )
