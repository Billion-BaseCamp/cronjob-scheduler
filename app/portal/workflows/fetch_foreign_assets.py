"""FETCH_FOREIGN_ASSETS: download Foreign Assets Information PDFs."""

from __future__ import annotations

from app.portal.workflows.fetch_foreign_assets_job import (
    run_fetch_foreign_assets_job,
)

WORKFLOW = "FETCH_FOREIGN_ASSETS"


async def run_fetch_foreign_assets(db, job) -> None:
    await run_fetch_foreign_assets_job(db, job, workflow=WORKFLOW)
