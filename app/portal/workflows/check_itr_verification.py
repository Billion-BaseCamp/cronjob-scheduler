"""CHECK_ITR_VERIFICATION: login, read filed-return status, log out.

Does not write itr_returns.filing_status.

Primary screenshot (one S3 object per job):
  fail/pause → the page where it broke
  success → Lifecycle (``app-itr-status-life-cycle``)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.portal.actions.login import run_login
from app.portal.actions.login_outcomes import LoginOutcome
from app.portal.actions.logout import run_logout
from app.portal.actions.read_filed_return_status import LIFECYCLE_SELECTOR, read_itr_status
from app.portal.actions.view_filed_returns import run_open_filed_returns
from app.core.config import settings
from app.portal.evidence import upload_screenshot
from app.portal.evidence_step import (
    EVIDENCE_FILED_RETURNS,
    EVIDENCE_LIFECYCLE,
    EVIDENCE_LOGIN,
    primary_evidence_step,
)
from app.portal.store import get_client

logger = logging.getLogger(__name__)

STEPS = ("LOGIN", "VIEW_FILED_RETURNS", "LOGOUT")
_EVIDENCE_RESULT_KEYS = ("evidence_s3_key", "evidence_step", "login_evidence_s3_key")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _merge_result(job, payload: dict[str, Any]) -> None:
    prior = job.result if isinstance(job.result, dict) else {}
    kept = {key: prior[key] for key in _EVIDENCE_RESULT_KEYS if key in prior}
    job.result = {**payload, **kept}


async def _maybe_screenshot(page: Any, job_id, step_name: str) -> str | None:
    if page is None:
        return None
    try:
        png = await page.screenshot(full_page=True)
    except Exception:
        logger.exception("Screenshot failed for %s", step_name)
        return None
    try:
        return upload_screenshot(job_id, step_name, png)
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


async def _lifecycle_visible(page: Any) -> bool:
    if page is None:
        return False
    try:
        return await page.locator(LIFECYCLE_SELECTOR).first.is_visible()
    except Exception:
        return False


def _pause(job, *, status: str, error_code: str, message: str) -> None:
    job.status = status
    job.error_code = error_code
    job.error_message = message
    job.worker_id = None
    job.waiting_since = _now()
    job.completed_at = None


def _fail(job, *, error_code: str, message: str) -> None:
    job.status = "failed"
    job.error_code = error_code
    job.error_message = message
    job.worker_id = None
    job.completed_at = _now()


def _requeue_or_fail(job, *, error_code: str, message: str) -> None:
    if (job.attempt or 0) < settings.MAX_ATTEMPTS:
        job.status = "queued"
        job.error_code = error_code
        job.error_message = message
        job.worker_id = None
        job.waiting_since = None
        job.current_step = STEPS[0]
        return
    _fail(job, error_code=error_code, message=message)


def _apply_login_outcome(job, client, outcome: LoginOutcome) -> bool:
    """Update the job for a failed login. True means login succeeded."""
    if outcome == LoginOutcome.SUCCESS:
        return True

    if outcome == LoginOutcome.MISSING_PASSWORD:
        _pause(
            job,
            status="waiting_for_password",
            error_code="MISSING_PASSWORD",
            message="No Income Tax portal password saved. Enter it to continue.",
        )
        return False

    if outcome == LoginOutcome.INVALID_PASSWORD:
        client.it_portal_pass = None
        _fail(
            job,
            error_code="INVALID_PASSWORD",
            message=(
                "Portal rejected the password. Enter a new one and run again."
            ),
        )
        return False

    if outcome == LoginOutcome.INVALID_USER_ID:
        _fail(
            job,
            error_code="INVALID_USER_ID",
            message=(
                "Portal rejected the user id (PAN). "
                "The saved password was not changed."
            ),
        )
        return False

    if outcome == LoginOutcome.CAPTCHA_REQUIRED:
        _pause(
            job,
            status="waiting_for_human",
            error_code="CAPTCHA_REQUIRED",
            message="CAPTCHA required; a human must complete login.",
        )
        return False

    if outcome == LoginOutcome.OTP_REQUIRED:
        _pause(
            job,
            status="waiting_for_otp",
            error_code="OTP_REQUIRED",
            message="OTP required; wait for the taxpayer to enter it.",
        )
        return False

    if outcome == LoginOutcome.PORTAL_BLOCKED:
        _fail(
            job,
            error_code="PORTAL_BLOCKED",
            message=(
                "Income Tax portal returned Permission Denied. "
                "The login form never loaded."
            ),
        )
        return False

    if outcome == LoginOutcome.UI_DRIFT:
        _fail(
            job,
            error_code="UI_DRIFT",
            message=(
                "Login page controls did not match. Inspect the live "
                "form and fix the selectors."
            ),
        )
        return False

    if outcome == LoginOutcome.NOT_AUTHENTICATED:
        _requeue_or_fail(
            job,
            error_code="EF500023",
            message=(
                "Portal rejected password login (session not ready). "
                "Retrying."
            ),
        )
        return False

    if outcome == LoginOutcome.DUAL_LOGIN:
        _fail(
            job,
            error_code="DUAL_LOGIN",
            message=(
                "Portal showed Dual Login Detected. Login Here did not "
                "complete the takeover."
            ),
        )
        return False

    _fail(
        job,
        error_code="UNKNOWN",
        message="Login could not be classified (unknown).",
    )
    return False


async def run_check_itr_verification(db, job) -> None:
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

            status = await read_itr_status(page, job.assessment_year)
            lifecycle = await _lifecycle_visible(page)
            await _attach_evidence(
                job,
                page,
                primary_evidence_step(
                    login_ok=True,
                    lifecycle_visible=lifecycle,
                ),
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
                    "workflow": "CHECK_ITR_VERIFICATION",
                    "login_ok": True,
                    "opened_filed_returns": True,
                    "logged_out": logged_out,
                    **status,
                },
            )
        except Exception:
            logger.exception("CHECK_ITR_VERIFICATION crashed for job %s", job.id)
            step = job.current_step or EVIDENCE_LOGIN
            if step == STEPS[2]:
                step = EVIDENCE_LIFECYCLE
            elif step not in {
                EVIDENCE_LOGIN,
                EVIDENCE_FILED_RETURNS,
                EVIDENCE_LIFECYCLE,
            }:
                step = EVIDENCE_LOGIN
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
