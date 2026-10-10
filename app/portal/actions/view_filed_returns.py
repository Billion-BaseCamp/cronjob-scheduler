"""Open View Filed Returns from the post-login e-File menu.

1. Click e-File (a#e-File)
2. Click Income Tax Returns (opens the submenu)
3. Click View Filed Returns

Selectors come from live portal HTML. Do not invent extras.
"""

from __future__ import annotations

import logging

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from app.portal.actions.read_filed_return_status import wait_for_filed_returns_page
from app.core.config import settings

logger = logging.getLogger(__name__)

E_FILE_SELECTOR = "a#e-File"
DASHBOARD_SELECTOR = "a#Dashboard"
# Still on screen when the session header loads but the outlet stays Login.
LOGIN_CARD_SELECTOR = "#loginPasswordField"
INCOME_TAX_RETURNS_SELECTOR = (
    'button.mat-mdc-menu-item:has-text("Income Tax Returns")'
)
VIEW_FILED_RETURNS_SELECTOR = (
    'button.mat-mdc-menu-item:has-text("View Filed Returns")'
)
_MENU_TIMEOUT_MS = 20_000
_LOGIN_CARD_TIMEOUT_MS = 8_000


async def _clear_stuck_login_card(page: Page) -> None:
    """Open Dashboard when the header is logged in and the login card remains."""
    card = page.locator(LOGIN_CARD_SELECTOR)
    try:
        await card.wait_for(state="hidden", timeout=_LOGIN_CARD_TIMEOUT_MS)
        return
    except PlaywrightTimeout:
        logger.info(
            "Login card still visible under the session header; opening Dashboard"
        )
    await page.locator(DASHBOARD_SELECTOR).click()
    await card.wait_for(state="hidden", timeout=_MENU_TIMEOUT_MS)


async def _click_filed_returns_menu(page: Page) -> None:
    logger.info("Clicking e-File")
    await page.locator(E_FILE_SELECTOR).click()

    logger.info("Clicking Income Tax Returns")
    itr_item = page.locator(INCOME_TAX_RETURNS_SELECTOR)
    await itr_item.wait_for(state="visible", timeout=_MENU_TIMEOUT_MS)
    await itr_item.hover()
    await itr_item.click()

    logger.info("Clicking View Filed Returns")
    filed_item = page.locator(VIEW_FILED_RETURNS_SELECTOR)
    await filed_item.wait_for(state="visible", timeout=_MENU_TIMEOUT_MS)
    await filed_item.click()


async def run_open_filed_returns(page: Page | None) -> bool:
    """Return True if the View Filed Returns page loaded."""
    if settings.PORTAL_AUTOMATION_DRY_RUN:
        logger.info("Dry-run: skip View Filed Returns")
        return True
    if page is None:
        logger.warning("No browser page; skip View Filed Returns")
        return False
    try:
        await _clear_stuck_login_card(page)
        await _click_filed_returns_menu(page)
        opened = await wait_for_filed_returns_page(page)
        if opened:
            logger.info("Opened View Filed Returns url=%s", page.url)
        return opened
    except PlaywrightTimeout:
        logger.warning("View Filed Returns timed out url=%s", page.url)
        return False
