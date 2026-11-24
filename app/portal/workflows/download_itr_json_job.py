"""DOWNLOAD_ITR_JSON: login once, download each selected ITR file, log out.

The year list is ``result.assessment_years``. A job with only
``assessment_year`` downloads that one year. AY 2021-22 onward is
``portal-automation/json/{client_id}/{assessment_year}/itr.json``.
AY 2020-21 and earlier is ``portal-automation/xml/{client_id}/{assessment_year}/itr.xml``.
A later run for the same client and year overwrites that object. Filing status is not changed.
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
from app.portal.actions.read_filed_return_status import (
    FILTER_APPLIED,
    ITR_NOT_FILED,
    _apply_ay_filter,
)
from app.portal.actions.view_filed_returns import run_open_filed_returns
from app.portal.evidence import JsonUploadError, upload_itr_file, upload_screenshot
from app.portal.evidence_step import EVIDENCE_FILED_RETURNS, EVIDENCE_LOGIN
from app.portal.itr_json import (
    FiledReturnCard,
    counts_by_filing_type,
    downloaded_filing_date,
    assessment_years_from_job,
    itr_file_format,
    latest_filing,
)
from app.portal.itr_json_status import remember_itr_json_year
from app.portal.store import get_client
from app.portal.workflows.filed_return_status_job import _apply_login_outcome

logger = logging.getLogger(__name__)

STEPS = ("LOGIN", "VIEW_FILED_RETURNS", "DOWNLOAD_FILE", "LOGOUT")
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
    cards: tuple[FiledReturnCard, ...] | list[FiledReturnCard] = (),
    chosen: FiledReturnCard | None = None,
    s3_key: str | None = None,
    file_format: str = "json",
    dry_run: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "workflow": workflow,
        "assessment_year": assessment_year,
        "file_format": file_format,
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
        "itr_json_s3_key": s3_key if file_format != "xml" else None,
        "itr_xml_s3_key": s3_key if file_format == "xml" else None,
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
        s3_key=payload.get("itr_xml_s3_key") or payload.get("itr_json_s3_key"),
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


def _bundle(
    workflow: str,
    *,
    years: list[str],
    entries: list[dict[str, Any]],
    logged_out: bool,
    dry_run: bool = False,
) -> dict[str, Any]:
    """One year keeps the existing result fields. Several years are listed on ``years``."""
    head = entries[0] if len(entries) == 1 else {
        "workflow": workflow,
        "assessment_year": years[0] if years else None,
        "login_ok": True,
        "opened_filed_returns": True,
        "logged_out": logged_out,
    }
    payload = {**head, "assessment_years": years, "years": entries, "logged_out": logged_out}
    if dry_run:
        payload["dry_run"] = True
    return payload


async def _store_year(db, job, scrape):
    s3_key = None
    if scrape.payload is not None and scrape.error_code is None:
        try:
            s3_key = await upload_itr_file(
                job.client_id,
                scrape.assessment_year,
                scrape.payload,
                scrape.file_format,
            )
        except JsonUploadError as exc:
            scrape = scrape.__class__(
                assessment_year=scrape.assessment_year,
                cards=scrape.cards,
                chosen=scrape.chosen,
                payload=None,
                file_format=scrape.file_format,
                error_code="S3_UPLOAD_FAILED",
                error_message=exc.message,
            )
    return scrape, s3_key


async def run_download_itr_json_job(db, job, *, workflow: str) -> None:
    client = await get_client(db, job.client_id)
    if client is None:
        job.current_step = STEPS[0]
        _fail(job, error_code="UNKNOWN", message="Client not found")
        return

    years = assessment_years_from_job(job)
    if not years:
        job.current_step = STEPS[0]
        _fail(job, error_code="UNKNOWN", message="Assessment year is required.")
        return
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
                    _bundle(
                        workflow,
                        years=years,
                        entries=[
                            _result(
                                workflow,
                                assessment_year=year,
                                logged_out=logged_out,
                                file_format=itr_file_format(year),
                                dry_run=True,
                            )
                            for year in years
                        ],
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
            entries: list[dict[str, Any]] = []
            failure: tuple[str, str] | None = None
            for year in years:
                scrape = await download_itr_json(page, year)
                scrape, s3_key = await _store_year(db, job, scrape)
                entry = _result(
                    workflow,
                    assessment_year=scrape.assessment_year,
                    logged_out=False,
                    cards=scrape.cards,
                    chosen=scrape.chosen,
                    s3_key=s3_key,
                    file_format=scrape.file_format,
                )
                if scrape.error_code is None:
                    entry["year_status"] = "downloaded"
                    await _remember(db, job, entry, status="downloaded")
                elif scrape.error_code == ITR_NOT_FILED:
                    entry["year_status"] = "no_filing"
                    entry["error_message"] = scrape.error_message
                    await _remember(db, job, entry, status="no_filing")
                else:
                    entry["year_status"] = "failed"
                    entry["error_code"] = scrape.error_code
                    entry["error_message"] = scrape.error_message
                    if failure is None:
                        failure = (
                            scrape.error_code or "DOWNLOAD_FAILED",
                            scrape.error_message or "ITR file download failed.",
                        )
                entries.append(entry)
                if scrape.error_code == "UI_DRIFT":
                    break

            evidence_year = str(entries[-1].get("assessment_year") or "") if entries else ""
            if evidence_year:
                applied = await _apply_ay_filter(page, evidence_year)
                if applied != FILTER_APPLIED:
                    logger.warning(
                        "Assessment year %s did not re-apply for the evidence shot",
                        evidence_year,
                    )
            await _attach_evidence(job, page, EVIDENCE_FILED_RETURNS)
            job.current_step = STEPS[3]
            logged_out = await run_logout(page)
            payload = _bundle(
                workflow,
                years=years,
                entries=entries,
                logged_out=logged_out,
            )
            if failure is not None:
                _fail(job, error_code=failure[0], message=failure[1])
            elif entries and all(item.get("year_status") == "no_filing" for item in entries):
                _complete(
                    job,
                    error_code=ITR_NOT_FILED,
                    message=entries[0].get("error_message") or "No filing found.",
                )
            else:
                _complete(job)
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
