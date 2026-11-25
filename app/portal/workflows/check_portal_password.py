"""CHECK_PORTAL_PASSWORD: log in, then log out.

A completed job means the portal accepted the password. It does not open
filed returns, notices, or downloads, and it does not wait for a password,
OTP, or CAPTCHA. Only a portal "invalid password" clears the saved password.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.core.config import settings
from app.portal.actions.login import run_login
from app.portal.actions.login_outcomes import LoginOutcome
from app.portal.actions.logout import run_logout
from app.portal.evidence import upload_screenshot
from app.portal.evidence_step import EVIDENCE_LOGIN
from app.portal.store import get_client
from app.portal.workflows.filed_return_status_job import _apply_login_outcome

logger = logging.getLogger(__name__)

WORKFLOW = "CHECK_PORTAL_PASSWORD"
STEPS = ("LOGIN", "LOGOUT")
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
    job.waiting_since = None
    job.completed_at = _now()


def _apply_password_check_outcome(job, client, outcome: LoginOutcome) -> bool:
    """Finish this check. True means the portal accepted the password.

    Other workflows may pause for a person. This one records the result and
    stops. The saved password changes only when the portal rejects it.
    """
    if outcome == LoginOutcome.SUCCESS:
        return True

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

    if outcome == LoginOutcome.MISSING_PASSWORD:
        _fail(
            job,
            error_code="MISSING_PASSWORD",
            message="No Income Tax portal password saved.",
        )
        return False

    if outcome == LoginOutcome.CAPTCHA_REQUIRED:
        _fail(
            job,
            error_code="CAPTCHA_REQUIRED",
            message=(
                "The portal showed a CAPTCHA, so this check could not "
                "confirm the password. The saved password was not changed."
            ),
        )
        return False

    if outcome == LoginOutcome.OTP_REQUIRED:
        _fail(
            job,
            error_code="OTP_REQUIRED",
            message=(
                "The portal asked for an OTP, so this check could not "
                "confirm the password. The saved password was not changed."
            ),
        )
        return False

    return _apply_login_outcome(job, client, outcome)


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


async def run_check_portal_password(db, job) -> None:
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
            if not _apply_password_check_outcome(job, client, outcome):
                return

            job.current_step = STEPS[1]
            logged_out = await run_logout(page)

            job.status = "completed"
            job.error_code = None
            job.error_message = None
            job.worker_id = None
            job.completed_at = _now()
            payload: dict[str, Any] = {
                "workflow": WORKFLOW,
                "login_ok": True,
                "password_valid": True,
                "logged_out": logged_out,
            }
            if settings.PORTAL_AUTOMATION_DRY_RUN:
                payload["dry_run"] = True
            _merge_result(job, payload)
        except Exception:
            logger.exception("%s crashed for job %s", WORKFLOW, job.id)
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
