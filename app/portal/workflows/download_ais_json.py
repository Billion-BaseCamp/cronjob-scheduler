"""DOWNLOAD_AIS_JSON: download AIS Utility JSON via Gemini captcha solving and store in S3."""

from __future__ import annotations

from app.portal.workflows.download_ais_json_job import run_download_ais_json_job

WORKFLOW = "DOWNLOAD_AIS_JSON"


async def run_download_ais_json(db, job) -> None:
    await run_download_ais_json_job(db, job, workflow=WORKFLOW)
