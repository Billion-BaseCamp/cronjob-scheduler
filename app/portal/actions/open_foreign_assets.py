"""Open AIS from e-filing, then Reports → Foreign Assets Information.

``a#AIS`` sometimes opens disclaimer ``#annualInformation``; Proceed
(``#okBtnNav3``) then opens ``ais.insight.gov.in``. A session that already
accepted the disclaimer opens the compliance portal directly and never
shows that modal. The e-filing tab stays open. Sign out of this
compliance tab with ``run_ais_logout`` before logging out of e-filing.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)

AIS_LINK_SELECTOR = "a#AIS"
AIS_DISCLAIMER_SELECTOR = "#annualInformation"
AIS_PROCEED_SELECTOR = "#okBtnNav3"
AIS_READY_SELECTOR = "app-ais-instructions, .header-profile .name small"
LOGIN_FIELD_SELECTOR = "#panAdhaarUserId"
_PAGE_TIMEOUT_MS = 20_000
_POPUP_TIMEOUT_S = 30.0
_POLL_MS = 250
_SESSION_TIMEOUT_MS = 45_000
# The Reports anchor also contains the help-tip image alt "Active bulb".
_REPORTS = re.compile(r"Reports")
_FOREIGN_ASSETS_HREF = "/complianceportal/reports/foreign-assets-information"
# Compliance header only. E-filing uses ``button.profileMenubtn``.
_AIS_LOGOUT_TOGGLE = ".header-profile button#dropdownMenu2"
_AIS_LOGOUT_ITEM = re.compile(r"^\s*Logout\s*$")
_AIS_LOGOUT_CONFIRM = re.compile(r"Are you sure you want to Log Out\?")
_AIS_LOGGED_OUT = re.compile(
    r"You have been logged out from Compliance Portal!"
)
_AIS_LOGOUT_OK = re.compile(r"^\s*Ok\s*$")


@dataclass
class AisPages:
    page: Page | None = None
    error_code: str | None = None
    message: str | None = None


async def _visible(page: Page, selector: str) -> bool:
    try:
        return await page.locator(selector).first.is_visible(timeout=1_500)
    except PlaywrightTimeout:
        return False


async def _is_compliance(page: Page) -> bool:
    url = page.url or ""
    if "ais.insight.gov.in" in url or "/complianceportal/" in url:
        return True
    try:
        return await page.locator("app-ais-instructions").first.is_visible()
    except PlaywrightTimeout:
        return False


async def _take_opened_page(page: Page, opened: list[Page]) -> Page | None:
    """A compliance tab that is already open, before or without Proceed."""
    if opened:
        ais = opened[-1]
        try:
            await ais.wait_for_load_state("domcontentloaded")
        except PlaywrightTimeout:
            logger.warning("AIS tab did not finish loading url=%s", ais.url)
        logger.info("AIS opened in a new tab url=%s", ais.url)
        return ais
    if await _is_compliance(page):
        logger.info("AIS opened in the current tab url=%s", page.url)
        return page
    return None


async def _proceed(page: Page, opened: list[Page]) -> Page | None:
    proceed = page.locator(AIS_PROCEED_SELECTOR)
    await proceed.wait_for(state="visible", timeout=_PAGE_TIMEOUT_MS)
    logger.info("AIS disclaimer — clicking Proceed")
    await proceed.click()
    deadline = time.monotonic() + _POPUP_TIMEOUT_S
    while time.monotonic() < deadline:
        ais = await _take_opened_page(page, opened)
        if ais is not None:
            return ais
        await page.wait_for_timeout(_POLL_MS)
    logger.warning("AIS Proceed did not open the compliance portal url=%s", page.url)
    return None


async def _open_ais_tab(page: Page) -> Page | None:
    logger.info("Clicking AIS")
    link = page.locator(AIS_LINK_SELECTOR)
    await link.wait_for(state="visible", timeout=_PAGE_TIMEOUT_MS)
    opened: list[Page] = []

    def _on_page(new_page: Page) -> None:
        opened.append(new_page)

    page.context.on("page", _on_page)
    try:
        await link.click()
        deadline = time.monotonic() + _POPUP_TIMEOUT_S
        while time.monotonic() < deadline:
            ais = await _take_opened_page(page, opened)
            if ais is not None:
                return ais
            if await page.locator(AIS_DISCLAIMER_SELECTOR).first.is_visible():
                return await _proceed(page, opened)
            await page.wait_for_timeout(_POLL_MS)
        logger.warning("AIS click did not open the compliance portal url=%s", page.url)
        return None
    finally:
        page.context.remove_listener("page", _on_page)


async def _session_state(ais: Page, pan: str) -> str:
    """``ok``, ``login_required``, ``mismatch``, or ``drift``."""
    ready = ais.locator(AIS_READY_SELECTOR).first
    try:
        await ready.wait_for(state="visible", timeout=_SESSION_TIMEOUT_MS)
    except PlaywrightTimeout:
        if await _visible(ais, LOGIN_FIELD_SELECTOR):
            return "login_required"
        return "drift"
    if await _visible(ais, LOGIN_FIELD_SELECTOR):
        return "login_required"
    header = ais.locator(".header-profile .name small")
    if await header.count() == 0:
        return "ok"
    text = (await header.first.inner_text()).strip().upper()
    if pan and text and pan not in text:
        logger.warning("AIS header PAN does not match the profile PAN")
        return "mismatch"
    return "ok"


async def _click_ais_dialog_ok(page: Page, title: re.Pattern[str]) -> None:
    """Ok on one compliance ``app-logout-alert``, matched by its title."""
    dialog = page.locator("mat-dialog-container app-logout-alert").filter(
        has_text=title
    )
    ok = dialog.locator("button.btn-primary").filter(has_text=_AIS_LOGOUT_OK)
    await ok.first.click(timeout=10_000)


async def run_ais_logout(page: Page | None) -> bool:
    """Sign out of the AIS compliance portal.

    Click the name in ``.header-profile``, then Logout, then Ok on
    "Are you sure you want to Log Out?", then Ok on
    "You have been logged out from Compliance Portal!". The header can
    stay on screen after that. These dialogs are separate from the
    foreign-assets download prompt. A missed click does not raise.
    The caller still logs out of e-filing.
    """
    if page is None:
        return False
    try:
        await page.bring_to_front()
        toggle = page.locator(_AIS_LOGOUT_TOGGLE)
        if await toggle.count() == 0 or not await toggle.first.is_visible():
            logger.info("AIS profile menu is not on this page url=%s", page.url)
            return False
        logger.info("Opening AIS profile menu")
        await toggle.first.click(timeout=10_000)
        item = page.locator(
            '.header-profile [aria-labelledby="dropdownMenu2"] button.dropdown-item'
        ).filter(has_text=_AIS_LOGOUT_ITEM)
        logger.info("Clicking AIS Logout")
        await item.first.click(timeout=10_000)
        logger.info("Confirming AIS logout")
        await _click_ais_dialog_ok(page, _AIS_LOGOUT_CONFIRM)
        logger.info("Dismissing AIS logged-out message")
        await _click_ais_dialog_ok(page, _AIS_LOGGED_OUT)
        logger.info("Logged out of AIS url=%s", page.url)
        return True
    except PlaywrightTimeout:
        logger.warning("AIS logout timed out url=%s", getattr(page, "url", ""))
        return False
    except Exception:
        logger.exception("AIS logout failed url=%s", getattr(page, "url", ""))
        return False


async def open_foreign_assets(page: Page, pan: str) -> AisPages:
    """Return the compliance-portal page sitting on Foreign Assets Information."""
    try:
        ais = await _open_ais_tab(page)
    except PlaywrightTimeout:
        logger.warning("AIS menu or disclaimer did not appear url=%s", page.url)
        return AisPages(
            error_code="UI_DRIFT",
            message="Could not open AIS from the e-filing navbar.",
        )
    if ais is None:
        return AisPages(
            error_code="UI_DRIFT",
            message="AIS click did not open the compliance portal.",
        )

    state = await _session_state(ais, pan)
    if state == "login_required":
        return AisPages(
            page=ais,
            error_code="AIS_LOGIN_REQUIRED",
            message=(
                "AIS opened a login page. The e-filing session did not "
                "carry over."
            ),
        )
    if state == "mismatch":
        return AisPages(
            page=ais,
            error_code="AIS_SESSION_MISMATCH",
            message="AIS is signed in as a different PAN than My Profile.",
        )
    if state != "ok":
        return AisPages(
            page=ais,
            error_code="UI_DRIFT",
            message="AIS did not show the instructions page.",
        )

    try:
        logger.info("Opening Reports")
        reports = ais.locator("nav.ctm-navbar a").filter(has_text=_REPORTS)
        await reports.first.wait_for(state="visible", timeout=_PAGE_TIMEOUT_MS)
        await reports.first.click()
        link = ais.locator(f'a[href="{_FOREIGN_ASSETS_HREF}"]')
        await link.first.wait_for(state="visible", timeout=_PAGE_TIMEOUT_MS)
        logger.info("Opening Foreign Assets Information")
        await link.first.click()
        await ais.locator("app-foreign-asset-report").wait_for(
            state="visible", timeout=_PAGE_TIMEOUT_MS
        )
    except PlaywrightTimeout:
        logger.warning("Foreign Assets page did not open url=%s", ais.url)
        return AisPages(
            page=ais,
            error_code="UI_DRIFT",
            message=(
                "Could not open Reports → Foreign Assets Information."
            ),
        )
    return AisPages(page=ais)
