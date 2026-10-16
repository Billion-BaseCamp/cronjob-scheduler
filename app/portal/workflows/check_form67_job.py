"""CHECK_FORM67: login, read Form 67 for the job year, log out, set the flag."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.core.config import settings
from app.portal.actions.login import run_login
from app.portal.actions.login_outcomes import LoginOutcome
from app.portal.actions.logout import run_logout
from app.portal.actions.read_form67_status import (
    ACTIVITY_SELECTOR,
    read_form67_status,
)
from app.portal.actions.view_filed_forms import run_open_form67_year_list
from app.portal.evidence import upload_screenshot
from app.portal.evidence_step import (
    EVIDENCE_FILED_FORMS,
    EVIDENCE_FORM67,
    EVIDENCE_LOGIN,
)
from app.portal.form67_filed import maybe_apply_form67_filed
from app.portal.store import get_client
from app.portal.workflows.filed_return_status_job import _apply_login_outcome

logger = logging.getLogger(__name__)

STEPS = ("LOGIN", "VIEW_FILED_FORMS", "LOGOUT")
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


async def _activity_visible(page: Any) -> bool:
    if page is None:
        return False
    try:
        return await page.locator(ACTIVITY_SELECTOR).first.is_visible()
    except Exception:
        return False


async def run_check_form67_job(db, job, *, workflow: str) -> None:
    client = await get_client(db, job.client_id)
    if client is None:
        job.current_step = STEPS[0]
        _fail(job, error_code="UNKNOWN", message="Client not found")
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

            job.current_step = STEPS[1]
            opened = await run_open_form67_year_list(page)
            if opened == "drift":
                await _attach_evidence(job, page, EVIDENCE_FILED_FORMS)
                await run_logout(page)
                _fail(
                    job,
                    error_code="UI_DRIFT",
                    message=(
                        "Could not open e-File → Income Tax Forms → "
                        "View Filed Forms → Form 67."
                    ),
                )
                return

            if opened == "not_filed":
                status = {
                    "assessment_year": (job.assessment_year or "").strip(),
                    "form67_filed": False,
                    "form67_status": None,
                    "acknowledgement_no": None,
                    "filing_date": None,
                    "filing_type": None,
                    "filed_by": None,
                }
            else:
                status = await read_form67_status(page, job.assessment_year)

            if status.get("filter_failed") or status.get("details_failed"):
                await _attach_evidence(job, page, EVIDENCE_FILED_FORMS)
                await run_logout(page)
                _fail(
                    job,
                    error_code="UI_DRIFT",
                    message=(
                        "Form 67 year list opened, but the assessment-year "
                        "filter or View Details did not complete."
                    ),
                )
                return

            if status.get("activity_verdict") == "mismatch":
                await _attach_evidence(job, page, EVIDENCE_FORM67)
                await run_logout(page)
                _fail(
                    job,
                    error_code="UI_DRIFT",
                    message=(
                        "View Details did not show Form 67 for this "
                        "assessment year."
                    ),
                )
                return

            on_activity = await _activity_visible(page)
            await _attach_evidence(
                job,
                page,
                EVIDENCE_FORM67 if on_activity else EVIDENCE_FILED_FORMS,
            )

            job.current_step = STEPS[2]
            logged_out = await run_logout(page)

            job.status = "completed"
            job.error_code = None
            job.error_message = None
            job.worker_id = None
            job.completed_at = _now()
            _merge_result(
                job,
                {
                    "workflow": workflow,
                    "login_ok": True,
                    "opened_filed_forms": opened == "opened",
                    "logged_out": logged_out,
                    **status,
                },
            )
            await maybe_apply_form67_filed(db, job)
        except Exception:
            logger.exception("%s crashed for job %s", workflow, job.id)
            await _attach_evidence(job, page, EVIDENCE_LOGIN)
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
