"""FETCH_FOREIGN_ASSETS: login, read the portal date, download C.Y. PDFs, log out.

The year list is the queued job's ``result.calendar_years``, for example
``{"calendar_years": [2024, 2023]}``. Insert that row in
``portal_automation_jobs`` to test. Nothing here calls tax-engine or a
frontend.

The PDF password is the My Profile date. clients.date_of_birth is not read
and is not updated. A year with no file is completed as no_data. A year
missing from the dropdown is year_not_in_dropdown. An unknown dialog, a
missing file, or an S3 failure fails the job; PDFs already stored stay in S3.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.core.config import settings
from app.portal.actions.download_foreign_assets import download_foreign_assets_year
from app.portal.actions.login import run_login
from app.portal.actions.login_outcomes import LoginOutcome
from app.portal.actions.logout import run_logout
from app.portal.actions.open_foreign_assets import open_foreign_assets
from app.portal.actions.read_my_profile import read_my_profile
from app.portal.evidence import PdfUploadError, upload_pdf, upload_screenshot
from app.portal.evidence_step import (
    EVIDENCE_FOREIGN_ASSETS,
    EVIDENCE_LOGIN,
    EVIDENCE_OPEN_AIS,
    EVIDENCE_READ_PROFILE,
)
from app.portal.foreign_assets import (
    calendar_years_from_job,
    foreign_assets_password,
    normalize_pan,
)
from app.portal.store import get_client
from app.portal.workflows.filed_return_status_job import _apply_login_outcome

logger = logging.getLogger(__name__)

STEPS = ("LOGIN", "READ_PROFILE", "OPEN_AIS", "FOREIGN_ASSETS", "LOGOUT")
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


def _result(
    workflow: str,
    years: list[dict[str, Any]],
    *,
    logged_out: bool,
    born_on: Any = None,
    password: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "workflow": workflow,
        "logged_out": logged_out,
        "years": years,
    }
    if dry_run:
        payload["dry_run"] = True
    if born_on is not None:
        payload["portal_date_of_birth"] = born_on.isoformat()
    if password:
        payload["pdf_password"] = password
    return payload


async def _fail_after_login(
    job,
    efiling: Any,
    shot: Any,
    step_name: str,
    *,
    error_code: str,
    message: str,
    payload: dict[str, Any],
) -> None:
    await _attach_evidence(job, shot, step_name)
    logged_out = await run_logout(efiling)
    payload = {**payload, "logged_out": logged_out}
    _fail(job, error_code=error_code, message=message)
    _merge_result(job, payload)


async def _store_pdf(job, year: int, pdf: bytes) -> dict[str, Any]:
    try:
        key = await upload_pdf(job.id, year, pdf)
    except PdfUploadError as exc:
        return {
            "calendar_year": year,
            "status": "failed",
            "error_code": "S3_UPLOAD_FAILED",
            "error": exc.message,
        }
    return {"calendar_year": year, "status": "downloaded", "s3_key": key}


async def run_fetch_foreign_assets_job(db, job, *, workflow: str) -> None:
    years = calendar_years_from_job(job)
    if not years:
        job.current_step = STEPS[0]
        _fail(
            job,
            error_code="MISSING_CALENDAR_YEARS",
            message="No calendar years were passed for this job.",
        )
        return

    client = await get_client(db, job.client_id)
    if client is None:
        job.current_step = STEPS[0]
        _fail(job, error_code="UNKNOWN", message="Client not found")
        return

    page = None
    browser = None
    playwright = None
    ais_page = None
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
                job.current_step = STEPS[4]
                job.status = "completed"
                job.error_code = None
                job.error_message = None
                job.worker_id = None
                job.completed_at = _now()
                _merge_result(
                    job,
                    _result(
                        workflow,
                        [
                            {"calendar_year": year, "status": "dry_run"}
                            for year in years
                        ],
                        logged_out=True,
                        dry_run=True,
                    ),
                )
                return

            job.current_step = STEPS[1]
            profile = await read_my_profile(page)
            if profile.error_code or profile.born_on is None:
                await _fail_after_login(
                    job,
                    page,
                    page,
                    EVIDENCE_READ_PROFILE,
                    error_code=profile.error_code or "PROFILE_DATE_UNREADABLE",
                    message=profile.message
                    or "Could not read the date from My Profile.",
                    payload=_result(workflow, [], logged_out=False),
                )
                return

            pan = profile.pan or normalize_pan(getattr(client, "pan_number", None))
            if not pan:
                await _fail_after_login(
                    job,
                    page,
                    page,
                    EVIDENCE_READ_PROFILE,
                    error_code="UI_DRIFT",
                    message="Could not read PAN from My Profile.",
                    payload=_result(
                        workflow, [], logged_out=False, born_on=profile.born_on
                    ),
                )
                return
            password = foreign_assets_password(pan, profile.born_on)

            job.current_step = STEPS[2]
            opened = await open_foreign_assets(page, pan)
            ais_page = opened.page
            if opened.error_code or ais_page is None:
                await _fail_after_login(
                    job,
                    page,
                    ais_page or page,
                    EVIDENCE_OPEN_AIS,
                    error_code=opened.error_code or "UI_DRIFT",
                    message=opened.message or "Could not open Foreign Assets Information.",
                    payload=_result(
                        workflow,
                        [],
                        logged_out=False,
                        born_on=profile.born_on,
                        password=password,
                    ),
                )
                return

            job.current_step = STEPS[3]
            saved: list[dict[str, Any]] = []
            base = dict(
                born_on=profile.born_on,
                password=password,
            )
            for year in years:
                downloaded = await download_foreign_assets_year(ais_page, year)
                pdf = downloaded.pop("pdf", None)
                if downloaded.get("status") == "downloaded":
                    downloaded = await _store_pdf(job, year, pdf or b"")
                if downloaded.get("status") == "page_drift":
                    await _fail_after_login(
                        job,
                        page,
                        ais_page,
                        EVIDENCE_FOREIGN_ASSETS,
                        error_code=downloaded.get("error_code") or "UI_DRIFT",
                        message=downloaded.get("error")
                        or "Foreign Assets Information changed.",
                        payload=_result(
                            workflow,
                            saved,
                            logged_out=False,
                            **base,
                        ),
                    )
                    return
                saved.append(downloaded)
                if downloaded.get("status") == "failed":
                    await _fail_after_login(
                        job,
                        page,
                        ais_page,
                        EVIDENCE_FOREIGN_ASSETS,
                        error_code=downloaded.get("error_code") or "DOWNLOAD_FAILED",
                        message=downloaded.get("error")
                        or f"Foreign assets PDF failed for C.Y. {year}.",
                        payload=_result(
                            workflow,
                            saved,
                            logged_out=False,
                            **base,
                        ),
                    )
                    return

            await _attach_evidence(job, ais_page, EVIDENCE_FOREIGN_ASSETS)
            job.current_step = STEPS[4]
            logged_out = await run_logout(page)
            job.status = "completed"
            job.error_code = None
            job.error_message = None
            job.worker_id = None
            job.completed_at = _now()
            _merge_result(
                job,
                _result(
                    workflow,
                    saved,
                    logged_out=logged_out,
                    **base,
                ),
            )
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            logger.exception("%s crashed for job %s: %s", workflow, job.id, detail)
            await _attach_evidence(job, ais_page or page, EVIDENCE_LOGIN)
            if page is not None:
                try:
                    await run_logout(page)
                except Exception:
                    logger.exception("Logout after crash failed")
            if page is not None and not settings.PLAYWRIGHT_HEADLESS:
                logger.error(
                    "Paused on the portal page. Press Resume in the "
                    "Playwright bar to close Chrome. %s",
                    detail,
                )
                try:
                    await page.pause()
                except Exception:
                    logger.exception("Playwright inspector did not open")
            _fail(
                job,
                error_code="UNKNOWN",
                message=(
                    f"Worker crashed at {job.current_step or 'LOGIN'}: {detail}"
                )[:4000],
            )
    finally:
        if browser is not None:
            await browser.close()
        if playwright is not None:
            await playwright.stop()
