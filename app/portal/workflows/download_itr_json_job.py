"""DOWNLOAD_ITR_JSON: login, download one ITR JSON, store it in S3, log out.

The object key is ``portal-automation/json/{client_id}/{assessment_year}/itr.json``.
A later run for the same client and year overwrites it. Filing status is not changed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.core.config import settings
from app.portal.actions.download_itr_json import download_itr_json
from app.portal.actions.login import run_login
from app.portal.actions.login_outcomes import LoginOutcome
from app.portal.actions.logout import run_logout
from app.portal.actions.read_filed_return_status import ITR_NOT_FILED
from app.portal.actions.view_filed_returns import run_open_filed_returns
from app.portal.evidence import JsonUploadError, upload_itr_json, upload_screenshot
from app.portal.evidence_step import EVIDENCE_FILED_RETURNS, EVIDENCE_LOGIN
from app.portal.itr_json import (
    FiledReturnCard,
    counts_by_filing_type,
    downloaded_filing_date,
    latest_filing,
)
from app.portal.itr_json_status import remember_itr_json_year
from app.portal.store import get_client
from app.portal.workflows.filed_return_status_job import _apply_login_outcome

logger = logging.getLogger(__name__)

STEPS = ("LOGIN", "VIEW_FILED_RETURNS", "DOWNLOAD_JSON", "LOGOUT")
_EVIDENCE_RESULT_KEYS = ("evidence_s3_key", "evidence_step", "login_evidence_s3_key")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _merge_result(job, payload: dict[str, Any]) -> None:
    prior = job.result if isinstance(job.result, dict) else {}
    kept = {key: prior[key] for key in _EVIDENCE_RESULT_KEYS if key in prior}
    job.result = {**payload, **kept}


def _fail(job, *, error_code: str, message: str) -> None:
    job.status = "failed"
    job.error_code = error_code
    job.error_message = message
    job.worker_id = None
    job.completed_at = _now()


def _complete(job, *, error_code: str | None = None, message: str | None = None) -> None:
    job.status = "completed"
    job.error_code = error_code
    job.error_message = message
    job.worker_id = None
    job.completed_at = _now()


async def _maybe_screenshot(page: Any, job_id, step_name: str) -> str | None:
    if page is None:
        return None
    try:
        png = await page.screenshot(full_page=True)
    except Exception:
        logger.exception("Screenshot failed for %s", step_name)
        return None
    try:
        return await upload_screenshot(job_id, step_name, png)
    except Exception:
        logger.exception("Evidence upload failed for %s", step_name)
        return None


async def _attach_evidence(job, page: Any, step_name: str) -> None:
    key = await _maybe_screenshot(page, job.id, step_name)
    if not key:
        return
    job.result = {
        **(job.result or {}),
        "evidence_s3_key": key,
        "evidence_step": step_name,
    }


def _blank(value: str) -> str | None:
    text = (value or "").strip()
    return text or None


def _summary(cards: tuple[FiledReturnCard, ...] | list[FiledReturnCard]) -> dict[str, Any]:
    filing_type, filing_date = latest_filing(cards)
    return {
        "latest_filing_type": filing_type,
        "latest_filing_date": filing_date,
        "filings_count": len(cards),
        "counts_by_filing_type": counts_by_filing_type(cards),
    }


def _result(
    workflow: str,
    *,
    assessment_year: str,
    logged_out: bool,
    cards: tuple[FiledReturnCard, ...] = (),
    chosen: FiledReturnCard | None = None,
    s3_key: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "workflow": workflow,
        "assessment_year": assessment_year,
        "login_ok": True,
        "opened_filed_returns": True,
        "logged_out": logged_out,
        "downloaded_filing_type": (
            chosen.filing_type.strip() if chosen is not None and chosen.filing_type.strip() else None
        ),
        "downloaded_filing_date": downloaded_filing_date(chosen),
        "itr_form": _blank(chosen.itr_form) if chosen is not None else None,
        "acknowledgement_no": _blank(chosen.acknowledgement_no) if chosen is not None else None,
        "filing_section": _blank(chosen.filing_section) if chosen is not None else None,
        "e_verified_on": _blank(chosen.e_verified_on) if chosen is not None else None,
        "itr_json_s3_key": s3_key,
        **_summary(cards),
    }
    if dry_run:
        payload["dry_run"] = True
    return payload


async def _remember(db, job, payload: dict[str, Any], *, status: str) -> None:
    await remember_itr_json_year(
        db,
        client_id=job.client_id,
        assessment_year=str(payload.get("assessment_year") or ""),
        status=status,
        job_id=job.id,
        s3_key=payload.get("itr_json_s3_key"),
        itr_form=payload.get("itr_form"),
        acknowledgement_no=payload.get("acknowledgement_no"),
        filing_section=payload.get("filing_section"),
        filing_date=payload.get("downloaded_filing_date"),
        e_verified_on=payload.get("e_verified_on"),
        latest_filing_type=payload.get("latest_filing_type"),
        latest_filing_date=payload.get("latest_filing_date"),
        filings_count=int(payload.get("filings_count") or 0),
        counts_by_filing_type=payload.get("counts_by_filing_type") or {},
    )


async def run_download_itr_json_job(db, job, *, workflow: str) -> None:
    client = await get_client(db, job.client_id)
    if client is None:
        job.current_step = STEPS[0]
        _fail(job, error_code="UNKNOWN", message="Client not found")
        return

    ay = (job.assessment_year or "").strip()
    page = None
    browser = None
    playwright = None
    try:
        try:
            if not settings.PORTAL_AUTOMATION_DRY_RUN:
                from playwright.async_api import async_playwright

                from app.portal.browser import launch_portal_page

                playwright = await async_playwright().start()
                browser, page = await launch_portal_page(playwright)

            outcome = await run_login(client, page)
            job.current_step = STEPS[0]
            if outcome != LoginOutcome.SUCCESS:
                if outcome != LoginOutcome.MISSING_PASSWORD:
                    await _attach_evidence(job, page, EVIDENCE_LOGIN)
            if not _apply_login_outcome(job, client, outcome):
                return

            if settings.PORTAL_AUTOMATION_DRY_RUN:
                job.current_step = STEPS[3]
                logged_out = await run_logout(page)
                _complete(job)
                _merge_result(
                    job,
                    _result(
                        workflow,
                        assessment_year=ay,
                        logged_out=logged_out,
                        dry_run=True,
                    ),
                )
                return

            job.current_step = STEPS[1]
            opened = await run_open_filed_returns(page)
            if not opened:
                await _attach_evidence(job, page, EVIDENCE_FILED_RETURNS)
                await run_logout(page)
                _fail(
                    job,
                    error_code="UI_DRIFT",
                    message=(
                        "Could not open e-File → Income Tax Returns → "
                        "View Filed Returns."
                    ),
                )
                return

            job.current_step = STEPS[2]
            scrape = await download_itr_json(page, ay)
            await _attach_evidence(job, page, EVIDENCE_FILED_RETURNS)

            s3_key = None
            if scrape.payload is not None and scrape.error_code is None:
                try:
                    s3_key = await upload_itr_json(
                        job.client_id, scrape.assessment_year, scrape.payload
                    )
                except JsonUploadError as exc:
                    scrape = scrape.__class__(
                        assessment_year=scrape.assessment_year,
                        cards=scrape.cards,
                        chosen=scrape.chosen,
                        payload=None,
                        error_code="S3_UPLOAD_FAILED",
                        error_message=exc.message,
                    )

            job.current_step = STEPS[3]
            logged_out = await run_logout(page)
            payload = _result(
                workflow,
                assessment_year=scrape.assessment_year,
                logged_out=logged_out,
                cards=scrape.cards,
                chosen=scrape.chosen,
                s3_key=s3_key,
            )
            if scrape.error_code is None:
                _complete(job)
                await _remember(db, job, payload, status="downloaded")
            elif scrape.error_code == ITR_NOT_FILED:
                _complete(job, error_code=scrape.error_code, message=scrape.error_message)
                await _remember(db, job, payload, status="no_filing")
            else:
                _fail(
                    job,
                    error_code=scrape.error_code,
                    message=scrape.error_message or "ITR JSON download failed.",
                )
            _merge_result(job, payload)
        except Exception:
            logger.exception("%s crashed for job %s", workflow, job.id)
            step = job.current_step or EVIDENCE_LOGIN
            if step not in {EVIDENCE_LOGIN, EVIDENCE_FILED_RETURNS}:
                step = EVIDENCE_FILED_RETURNS if step != STEPS[0] else EVIDENCE_LOGIN
            await _attach_evidence(job, page, step)
            _fail(
                job,
                error_code="UNKNOWN",
                message="Worker crashed while processing this job.",
            )
    finally:
        if browser is not None:
            await browser.close()
        if playwright is not None:
            await playwright.stop()
