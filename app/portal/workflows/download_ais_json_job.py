"""DOWNLOAD_AIS_JSON: login, open AIS, solve captcha via Gemini, store AIS JSON in S3."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.core.config import settings
from app.portal.actions.download_ais_json import download_ais_json
from app.portal.actions.login import run_login
from app.portal.actions.login_outcomes import LoginOutcome
from app.portal.actions.logout import run_logout
from app.portal.actions.open_foreign_assets import (
    open_ais_instructions,
    run_ais_logout,
)
from app.portal.actions.read_my_profile import read_my_profile
from app.portal.ais_json import (
    ais_json_filename,
    normalize_financial_year,
)
from app.portal.ais_json_archive import remember_ais_json_archive
from app.portal.evidence import (
    AisJsonUploadError,
    upload_ais_json,
    upload_screenshot,
)
from app.portal.evidence_step import (
    EVIDENCE_AIS_DOWNLOAD,
    EVIDENCE_LOGIN,
    EVIDENCE_OPEN_AIS,
    EVIDENCE_READ_PROFILE,
)
from app.portal.foreign_assets import normalize_pan
from app.portal.store import get_client
from app.portal.workflows.filed_return_status_job import _apply_login_outcome

logger = logging.getLogger(__name__)

STEPS = ("LOGIN", "READ_PROFILE", "OPEN_AIS", "DOWNLOAD_AIS_JSON", "LOGOUT")
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


def _complete(
    job, *, error_code: str | None = None, message: str | None = None
) -> None:
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


def _result(
    workflow: str,
    *,
    financial_year: str,
    pan: str | None,
    status: str,
    logged_out: bool,
    s3_key: str | None = None,
    file_size_bytes: int | None = None,
    captcha_attempts: int = 0,
    fetched_at: datetime | None = None,
    born_on: Any = None,
    dry_run: bool = False,
    ais_logged_out: bool | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "workflow": workflow,
        "financial_year": financial_year,
        "pan": pan,
        "status": status,
        "ais_json_s3_key": s3_key,
        "file_size_bytes": file_size_bytes,
        "captcha_attempts": captcha_attempts,
        "logged_out": logged_out,
    }
    if fetched_at is not None:
        payload["fetched_at"] = fetched_at.isoformat()
    if ais_logged_out is not None:
        payload["ais_logged_out"] = ais_logged_out
    if born_on is not None:
        payload["portal_date_of_birth"] = born_on.isoformat()
    if dry_run:
        payload["dry_run"] = True
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
    ais: Any = None,
) -> None:
    await _attach_evidence(job, shot, step_name)
    if ais is not None:
        payload = {**payload, "ais_logged_out": await run_ais_logout(ais)}
    logged_out = await run_logout(
        None if ais is not None and ais is efiling else efiling
    )
    payload = {**payload, "logged_out": logged_out}
    _fail(job, error_code=error_code, message=message)
    _merge_result(job, payload)


async def run_download_ais_json_job(db, job, *, workflow: str) -> None:
    try:
        fy = normalize_financial_year(getattr(job, "assessment_year", None))
    except ValueError:
        job.current_step = STEPS[0]
        _fail(
            job,
            error_code="MISSING_FINANCIAL_YEAR",
            message="A valid financial year (e.g. 2025-26) is required.",
        )
        return

    client = await get_client(db, job.client_id)
    if client is None:
        job.current_step = STEPS[0]
        _fail(job, error_code="UNKNOWN", message="Client not found")
        return

    pan_fallback = normalize_pan(getattr(client, "pan_number", None))

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
                logged_out = await run_logout(page)
                _complete(job)
                _merge_result(
                    job,
                    _result(
                        workflow,
                        financial_year=fy,
                        pan=pan_fallback,
                        status="dry_run",
                        logged_out=logged_out,
                        dry_run=True,
                    ),
                )
                return

            job.current_step = STEPS[1]
            profile = await read_my_profile(page)
            pan = profile.pan or pan_fallback
            born_on = profile.born_on
            if not pan:
                await _fail_after_login(
                    job,
                    page,
                    page,
                    EVIDENCE_READ_PROFILE,
                    error_code="UI_DRIFT",
                    message="Could not determine PAN from My Profile or client record.",
                    payload=_result(
                        workflow,
                        financial_year=fy,
                        pan=None,
                        status="failed",
                        logged_out=False,
                        born_on=born_on,
                    ),
                )
                return

            job.current_step = STEPS[2]
            opened = await open_ais_instructions(page, pan)
            ais_page = opened.page
            if opened.error_code or ais_page is None:
                await _fail_after_login(
                    job,
                    page,
                    ais_page or page,
                    EVIDENCE_OPEN_AIS,
                    error_code=opened.error_code or "UI_DRIFT",
                    message=opened.message or "Could not open AIS portal.",
                    payload=_result(
                        workflow,
                        financial_year=fy,
                        pan=pan,
                        status="failed",
                        logged_out=False,
                        born_on=born_on,
                    ),
                    ais=ais_page,
                )
                return

            job.current_step = STEPS[3]
            scrape = await download_ais_json(ais_page, fy)
            await _attach_evidence(job, ais_page, EVIDENCE_AIS_DOWNLOAD)

            s3_key: str | None = None
            file_size: int | None = None
            fetched_at: datetime | None = None
            if scrape.status == "downloaded" and scrape.payload is not None:
                try:
                    s3_key = await upload_ais_json(
                        job.client_id, pan, fy, scrape.payload
                    )
                    file_size = len(scrape.payload)
                    fetched_at = _now()
                    await remember_ais_json_archive(
                        db,
                        client_id=job.client_id,
                        itr_return_id=getattr(job, "itr_return_id", None),
                        s3_key=s3_key,
                        original_filename=ais_json_filename(pan, fy),
                        file_size_bytes=file_size,
                    )
                except AisJsonUploadError as exc:
                    scrape = scrape.__class__(
                        status="failed",
                        financial_year=fy,
                        error_code="S3_UPLOAD_FAILED",
                        error_message=exc.message,
                        captcha_attempts=scrape.captcha_attempts,
                    )

            job.current_step = STEPS[4]
            ais_logged_out = await run_ais_logout(ais_page)
            logged_out = await run_logout(None if ais_page is page else page)

            payload = _result(
                workflow,
                financial_year=fy,
                pan=pan,
                status=scrape.status,
                logged_out=logged_out,
                ais_logged_out=ais_logged_out,
                s3_key=s3_key,
                file_size_bytes=file_size,
                captcha_attempts=scrape.captcha_attempts,
                fetched_at=fetched_at,
                born_on=born_on,
            )

            if scrape.status in {"downloaded", "no_data", "year_not_in_dropdown"}:
                _complete(
                    job,
                    error_code=scrape.error_code,
                    message=scrape.error_message,
                )
            else:
                _fail(
                    job,
                    error_code=scrape.error_code or "DOWNLOAD_FAILED",
                    message=scrape.error_message or "AIS JSON download failed.",
                )
            _merge_result(job, payload)
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            logger.exception("%s crashed for job %s: %s", workflow, job.id, detail)
            await _attach_evidence(job, ais_page or page, EVIDENCE_LOGIN)
            if ais_page is not None:
                await run_ais_logout(ais_page)
            if page is not None and page is not ais_page:
                try:
                    await run_logout(page)
                except Exception:
                    logger.exception("Logout after crash failed")
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
