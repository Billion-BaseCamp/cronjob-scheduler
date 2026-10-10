"""CHECK_E_PROCEEDINGS_NOTICES: read-only harvest of Pending Actions → e-Proceedings."""

from __future__ import annotations

from app.portal.workflows.pending_actions_notices_job import (
    run_pending_actions_notices_job,
)

WORKFLOW = "CHECK_E_PROCEEDINGS_NOTICES"
SOURCE = "e_proceedings"


async def run_check_e_proceedings_notices(db, job) -> None:
    await run_pending_actions_notices_job(db, job, workflow=WORKFLOW, source=SOURCE)
