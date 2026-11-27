"""DOWNLOAD_26AS_TXT: Form 168 / ATS TXT download stored in advance_tax_26as_uploads."""

from __future__ import annotations

from app.portal.workflows.download_26as_txt_job import run_download_26as_txt_job

WORKFLOW = "DOWNLOAD_26AS_TXT"


async def run_download_26as_txt(db, job) -> None:
    await run_download_26as_txt_job(db, job, workflow=WORKFLOW)
