"""Open Pending Actions menu / return to the e-Proceedings list.

Selectors from live portal HTML. Used by notice harvest actions.
"""

from __future__ import annotations

import logging
import re

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from app.core.config import settings

logger = logging.getLogger(__name__)

PENDING_ACTIONS_SELECTOR = "a#Pending\\ Actions, a[id='Pending Actions']"
PENDING_ACTIONS_FALLBACK = (
    'a.mat-mdc-menu-trigger.menuText:has-text("Pending Actions")'
)
E_PROCEEDINGS_MENU_SELECTOR = (
    'button.mat-mdc-menu-item:has-text("e-Proceedings")'
)
SUO_MOTO_SELECTOR = "#Suo_Moto"
FOR_YOUR_ACTION_TAB = re.compile(r"For your Action", re.I)
_NAV_TIMEOUT_MS = 20_000


async def open_pending_actions_menu(page: Page) -> None:
    trigger = page.locator(PENDING_ACTIONS_SELECTOR)
    if await trigger.count() == 0:
        trigger = page.locator(PENDING_ACTIONS_FALLBACK)
    await trigger.first.click(timeout=_NAV_TIMEOUT_MS)


async def open_e_proceedings_for_your_action(page: Page | None) -> bool:
    """Pending Actions → e-Proceedings → For your Action. Return True if ready."""
    if settings.PORTAL_AUTOMATION_DRY_RUN:
        logger.info("Dry-run: skip e-Proceedings nav")
        return True
    if page is None:
        logger.warning("No browser page; skip e-Proceedings nav")
        return False
    try:
        logger.info("Opening Pending Actions → e-Proceedings")
        await open_pending_actions_menu(page)

        item = page.locator(E_PROCEEDINGS_MENU_SELECTOR)
        await item.first.wait_for(state="visible", timeout=_NAV_TIMEOUT_MS)
        await item.first.click()

        suo = page.locator(SUO_MOTO_SELECTOR)
        if await suo.count() and await suo.first.is_visible():
            # Ensure Self / Suo Moto path is selected when present.
            try:
                await suo.first.click(timeout=5_000)
            except PlaywrightTimeout:
                pass

        tab = page.get_by_role("tab", name=FOR_YOUR_ACTION_TAB)
        if await tab.count() == 0:
            tab = page.locator("[role='tab']").filter(has_text=FOR_YOUR_ACTION_TAB)
        await tab.first.wait_for(state="visible", timeout=_NAV_TIMEOUT_MS)
        await tab.first.click()

        await page.wait_for_timeout(500)
        logger.info("Opened e-Proceedings For your Action url=%s", page.url)
        return True
    except PlaywrightTimeout:
        logger.warning("e-Proceedings nav timed out url=%s", getattr(page, "url", ""))
        return False


async def back_to_e_proceedings_list(page: Page) -> bool:
    """Leave viewNotices and return to the proceeding list."""
    try:
        back = page.locator("button.previousIcon, button.large-button-secondary").filter(
            has_text=re.compile(r"^\s*Back\s*$", re.I)
        )
        if await back.count() and await back.first.is_visible():
            await back.first.click(timeout=10_000)
        else:
            crumb = page.locator("a, button, span").filter(
                has_text=re.compile(r"^\s*e-Proceedings\s*$", re.I)
            )
            if await crumb.count():
                await crumb.first.click(timeout=10_000)
            else:
                await page.go_back(timeout=_NAV_TIMEOUT_MS)

        tab = page.get_by_role("tab", name=FOR_YOUR_ACTION_TAB)
        if await tab.count() == 0:
            tab = page.locator("[role='tab']").filter(has_text=FOR_YOUR_ACTION_TAB)
        await tab.first.wait_for(state="visible", timeout=_NAV_TIMEOUT_MS)
        return True
    except PlaywrightTimeout:
        logger.warning("Could not return to e-Proceedings list url=%s", page.url)
        return False
