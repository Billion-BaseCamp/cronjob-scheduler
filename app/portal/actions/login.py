"""Log in to the Income Tax e-filing portal.

Flow (each Continue hits POST /iec/loginapi/login):
  1. Open the login page
  2. Type user id (PAN) → Continue
  3. Wait for the password screen
  4. Type password, tick the confirm box → Continue
  5. If Dual Login Detected, click Login Here once (take over the session)
  6. Confirm the logged-in page is visible

CAPTCHA / OTP are never solved here; those pause the job for a human.
Dual login is not CAPTCHA/OTP — take over automatically.
Dry-run skips the browser.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from app.portal.actions.login_outcomes import (
    LoginOutcome,
    classify_login_page,
    is_assured_invalid_password,
)
from app.core.config import settings
from app.portal.crypto import decrypt_portal_secret
from app.portal.network import is_login_api, login_api_error

logger = logging.getLogger(__name__)

# Selectors from live portal HTML. Do not invent extras.
USER_ID_SELECTOR = "#panAdhaarUserId"
CONTINUE_AFTER_PAN_SELECTOR = (
    "button.large-button-primary.width.marTop16"
)
PASSWORD_SELECTOR = "#loginPasswordField"
PASSWORD_CHECKBOX_SELECTOR = "#passwordCheckBox-input"
CONTINUE_AFTER_PASSWORD_SELECTOR = (
    "button.large-button-primary.width.marTop26"
)
PASSWORD_MAT_ERROR_SELECTOR = "mat-error"
LOGGED_IN_SELECTOR = "#postLoginMenuBar, app-dashboard, a#Dashboard"
# Dual-login modal on #/login/password (live portal HTML).
LOGIN_HERE_BUTTON_SELECTOR = "button.defaultButton.primaryButton"
_LOGIN_HERE_RE = re.compile(r"Login\s+Here", re.I)

CAPTCHA_SELECTOR = (
    "img[src*='captcha'], img[alt*='captcha' i], img.captcha, "
    "input[name*='captcha' i], input[id*='captcha' i]"
)
OTP_SELECTOR = (
    "input[name*='otp' i], input[id*='otp' i], "
    "input[autocomplete='one-time-code']"
)

# Submitting password immediately after PAN Continue returns EF500023.
# A 3s pause failed; the successful run waited ~10s after PAN OK.
PAUSE_AFTER_PAN_MS = 10_000


def portal_user_id(client: Any) -> Optional[str]:
    """Prefer the saved portal user id; otherwise use the client's PAN."""
    saved = decrypt_portal_secret(
        getattr(client, "it_portal_username", None)
    )
    if saved and saved.strip():
        return saved.strip()
    pan = getattr(client, "pan_number", None)
    if pan and str(pan).strip():
        return str(pan).strip().upper()
    return None


def dry_run_login(password: Optional[str]) -> LoginOutcome:
    if not password or not str(password).strip():
        return LoginOutcome.MISSING_PASSWORD
    if settings.PORTAL_AUTOMATION_DRY_RUN_INVALID_PASSWORD:
        return LoginOutcome.INVALID_PASSWORD
    return LoginOutcome.SUCCESS


def resolve_login_outcome_without_browser(client: Any) -> LoginOutcome:
    password = decrypt_portal_secret(
        getattr(client, "it_portal_pass", None)
    )
    if settings.PORTAL_AUTOMATION_DRY_RUN:
        return dry_run_login(password)
    if not password or not str(password).strip():
        return LoginOutcome.MISSING_PASSWORD
    return LoginOutcome.UNKNOWN


async def _is_visible(page: Page, selector: str) -> bool:
    try:
        return await page.locator(selector).first.is_visible(timeout=1500)
    except PlaywrightTimeout:
        return False


async def _captcha_or_otp(page: Page) -> Optional[LoginOutcome]:
    if await _is_visible(page, CAPTCHA_SELECTOR):
        return LoginOutcome.CAPTCHA_REQUIRED
    if await _is_visible(page, OTP_SELECTOR):
        return LoginOutcome.OTP_REQUIRED
    return None


async def _fill(page: Page, selector: str, value: str) -> None:
    field = page.locator(selector)
    await field.wait_for(state="visible")
    await field.fill(value)


async def _click_login_button(
    page: Page, selector: str
) -> Optional[str]:
    """Click Continue and read the login API JSON for an error message."""
    async with page.expect_response(is_login_api, timeout=20_000) as pending:
        await page.locator(selector).click()
    return await login_api_error(await pending.value)


async def _wait_for_password_screen(page: Page) -> None:
    await page.wait_for_function(
        "() => location.hash.includes('/login/password')",
        timeout=20_000,
    )
    await page.locator(PASSWORD_SELECTOR).wait_for(state="visible")
    await page.locator(CONTINUE_AFTER_PASSWORD_SELECTOR).wait_for(
        state="visible"
    )
    logger.info("Waiting %sms for portal session", PAUSE_AFTER_PAN_MS)
    await page.wait_for_timeout(PAUSE_AFTER_PAN_MS)


async def _mat_error_text(page: Page) -> str:
    loc = page.locator(PASSWORD_MAT_ERROR_SELECTOR).first
    try:
        if await loc.is_visible(timeout=3_000):
            return (await loc.inner_text()).strip()
    except PlaywrightTimeout:
        return ""
    return ""


def _classify_pan_error(error: str, page: Page) -> LoginOutcome:
    outcome = classify_login_page(
        page_text=error,
        url=page.url or "",
        still_on_login=True,
        login_step="pan",
    )
    if outcome in (LoginOutcome.INVALID_PASSWORD, LoginOutcome.SUCCESS):
        return LoginOutcome.INVALID_USER_ID
    return outcome


def _login_here_button(page: Page):
    return page.locator(LOGIN_HERE_BUTTON_SELECTOR).filter(has_text=_LOGIN_HERE_RE)


async def _confirm_logged_in(page: Page) -> LoginOutcome:
    """Wait for dashboard, or take over a Dual Login modal first."""
    logged_in = page.locator(LOGGED_IN_SELECTOR).first
    login_here = _login_here_button(page)
    dual_copy = page.get_by_text(
        re.compile(
            r"Dual Login Detected|currently active in another window",
            re.I,
        )
    )
    try:
        await logged_in.or_(login_here).or_(dual_copy).wait_for(
            state="visible", timeout=30_000
        )
    except PlaywrightTimeout:
        logger.warning("Logged-in page missing url=%s", page.url)
        password_outcome = await _classify_password_error(page, None)
        if password_outcome == LoginOutcome.INVALID_PASSWORD:
            return password_outcome
        return classify_login_page(
            page_text=await page.inner_text("body"),
            url=page.url or "",
            still_on_login="login" in (page.url or "").lower(),
            login_step="password",
        )

    if await logged_in.is_visible():
        return LoginOutcome.SUCCESS

    logger.info("Dual login modal — clicking Login Here")
    try:
        await login_here.first.click(timeout=5_000)
    except Exception:
        logger.warning("Login Here click did not land url=%s", page.url)
        return LoginOutcome.DUAL_LOGIN

    try:
        await logged_in.wait_for(state="visible", timeout=30_000)
    except PlaywrightTimeout:
        logger.warning(
            "Logged-in page missing after Login Here url=%s", page.url
        )
        return LoginOutcome.DUAL_LOGIN
    return LoginOutcome.SUCCESS


async def _classify_password_error(
    page: Page, login_error: str | None
) -> LoginOutcome:
    url = page.url or ""
    mat_text = await _mat_error_text(page)
    combined = " ".join(part for part in (login_error, mat_text) if part)
    if is_assured_invalid_password(page_text=mat_text, url=url) or (
        is_assured_invalid_password(page_text=combined, url=url)
    ):
        return LoginOutcome.INVALID_PASSWORD
    if login_error:
        return classify_login_page(
            page_text=login_error,
            url=url,
            still_on_login=True,
            login_step="password",
        )
    return LoginOutcome.UNKNOWN


async def playwright_login(
    page: Page,
    *,
    user_id: str,
    password: str,
    login_url: Optional[str] = None,
) -> LoginOutcome:
    url = login_url or settings.PORTAL_LOGIN_URL
    try:
        logger.info("Opening login %s", url)
        await page.goto(url, wait_until="domcontentloaded")
        await page.locator(USER_ID_SELECTOR).wait_for(
            state="visible", timeout=45_000
        )

        paused = await _captcha_or_otp(page)
        if paused is not None:
            return paused

        logger.info("Step 1: user id")
        await _fill(page, USER_ID_SELECTOR, user_id)
        pan_error = await _click_login_button(
            page, CONTINUE_AFTER_PAN_SELECTOR
        )
        if pan_error:
            logger.warning("PAN Continue failed: %s", pan_error)
            return _classify_pan_error(pan_error, page)

        await _wait_for_password_screen(page)
        paused = await _captcha_or_otp(page)
        if paused is not None:
            return paused

        logger.info("Step 2: password")
        await _fill(page, PASSWORD_SELECTOR, password)
        await page.locator(PASSWORD_CHECKBOX_SELECTOR).check(force=True)
        login_error = await _click_login_button(
            page, CONTINUE_AFTER_PASSWORD_SELECTOR
        )
        if login_error:
            logger.warning("Password Continue failed: %s", login_error)
            return await _classify_password_error(page, login_error)

        paused = await _captcha_or_otp(page)
        if paused is not None:
            return paused

        logger.info("Step 3: confirm logged-in page")
        outcome = await _confirm_logged_in(page)
        if outcome == LoginOutcome.SUCCESS:
            logger.info("Login succeeded url=%s", page.url)
        return outcome
    except PlaywrightTimeout:
        logger.warning("Login control timed out url=%s", page.url)
        return LoginOutcome.UI_DRIFT


async def run_login(client: Any, page: Optional[Page] = None) -> LoginOutcome:
    """Decrypt credentials, then log in (or dry-run)."""
    password = decrypt_portal_secret(
        getattr(client, "it_portal_pass", None)
    )
    if settings.PORTAL_AUTOMATION_DRY_RUN:
        return dry_run_login(password)
    if not password or not str(password).strip():
        return LoginOutcome.MISSING_PASSWORD
    user_id = portal_user_id(client)
    if not user_id:
        return LoginOutcome.UNKNOWN
    if page is None:
        raise RuntimeError(
            "Playwright page is required when dry-run is disabled"
        )
    return await playwright_login(
        page, user_id=user_id, password=password
    )
