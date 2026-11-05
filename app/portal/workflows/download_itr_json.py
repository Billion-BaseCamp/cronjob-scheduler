"""DOWNLOAD_ITR_JSON: filed-return JSON download stored in S3."""

from __future__ import annotations

from app.portal.workflows.download_itr_json_job import run_download_itr_json_job

WORKFLOW = "DOWNLOAD_ITR_JSON"


async def run_download_itr_json(db, job) -> None:
    await run_download_itr_json_job(db, job, workflow=WORKFLOW)
