"""CHECK_PENDING_ACTIONS_NOTICES: read-only notice harvest (Phase 1: e-Proceedings)."""

from __future__ import annotations

from app.portal.workflows.pending_actions_notices_job import (
    run_pending_actions_notices_job,
)

WORKFLOW = "CHECK_PENDING_ACTIONS_NOTICES"
# Phase 1 — A only. Add outstanding_demand / compliance_portal in later phases.
PHASE1_SOURCES = ("e_proceedings",)


async def run_check_pending_actions_notices(db, job) -> None:
    await run_pending_actions_notices_job(
        db,
        job,
        workflow=WORKFLOW,
        sources=PHASE1_SOURCES,
    )
