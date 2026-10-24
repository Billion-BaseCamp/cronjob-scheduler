"""Open View Filed Forms and the Form 67 year list.

1. Click e-File (a#e-File)
2. Click Income Tax Forms
3. Click View Filed Forms
4. Open the tab Forms as per Income Tax Act 1961
5. On the Form 67 card, click View All

A page that says nothing has been filed, or a list with no Form 67 card,
means not filed. A missing 1961 tab is drift. View All is taken from the
Form 67 card itself, not from a wrapper that also holds other forms.
"""

from __future__ import annotations

import logging
import re

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from app.core.config import settings
from app.portal.actions.read_form67_status import (
    FORMS_PAGE_SELECTOR,
    TAB_1961,
    YEAR_LIST_SELECTOR,
)
from app.portal.actions.view_filed_returns import (
    E_FILE_SELECTOR,
    _clear_stuck_login_card,
)

logger = logging.getLogger(__name__)

_MENU_TIMEOUT_MS = 20_000
_PAGE_TIMEOUT_MS = 20_000
_FORM67_NAME = re.compile(r"^\s*Form\s+67\s*$")
_NO_FORMS_RE = re.compile(r"no form filed till date", re.IGNORECASE)


def is_no_forms_filed_page(text: str) -> bool:
    """View Filed Forms with no filings at all. There is no 1961 tab."""
    return _NO_FORMS_RE.search(text or "") is not None


def _visible_menuitem(page: Page, name: str):
    """Exact visible row. has-text also matches File Income Tax Forms."""
    return (
        page.locator(".cdk-overlay-container")
        .get_by_role("menuitem", name=name, exact=True)
        .filter(visible=True)
        .last
    )


async def _click_view_filed_forms(page: Page) -> None:
    logger.info("Clicking e-File")
    await page.locator(E_FILE_SELECTOR).click()

    logger.info("Opening Income Tax Forms")
    forms_item = _visible_menuitem(page, "Income Tax Forms")
    await forms_item.wait_for(state="visible", timeout=_MENU_TIMEOUT_MS)
    await forms_item.hover()
    await forms_item.click()

    logger.info("Clicking View Filed Forms")
    filed_item = _visible_menuitem(page, "View Filed Forms")
    await filed_item.wait_for(state="visible", timeout=_MENU_TIMEOUT_MS)
    await filed_item.click()


async def _forms_page_kind(page: Page) -> str:
    """``empty`` when nothing is filed, ``list`` when the 1961 tab is up, else ``drift``."""
    root = page.locator(FORMS_PAGE_SELECTOR)
    empty = root.get_by_text("No form filed till date")
    tab = page.get_by_role("tab", name=TAB_1961)
    try:
        await empty.or_(tab).first.wait_for(state="visible", timeout=_PAGE_TIMEOUT_MS)
    except PlaywrightTimeout:
        logger.warning("Income Tax Act 1961 tab did not load url=%s", page.url)
        return "drift"
    if await empty.count() and await empty.first.is_visible():
        text = await root.first.inner_text()
        if is_no_forms_filed_page(text):
            logger.info("View Filed Forms has no forms filed url=%s", page.url)
            return "empty"
    return "list"


async def _open_1961_tab(page: Page) -> bool:
    tab = page.get_by_role("tab", name=TAB_1961)
    try:
        await tab.first.wait_for(state="visible", timeout=_PAGE_TIMEOUT_MS)
        await tab.first.click()
        await page.locator(".headFormNameStyle, .nothingToShow").first.wait_for(
            state="visible",
            timeout=_PAGE_TIMEOUT_MS,
        )
    except PlaywrightTimeout:
        logger.warning("Income Tax Act 1961 tab did not load url=%s", page.url)
        return False
    return True


def _form67_card(page: Page):
    """The card whose own heading is Form 67.

    An outer mat-card wraps every form. Matching any card that contains
    Form 67 then clicks that wrapper's first View All, which is another
    form (Form 10E on a client who filed that first).
    """
    heading = (
        page.locator(FORMS_PAGE_SELECTOR)
        .locator(".headFormNameStyle")
        .filter(has_text=_FORM67_NAME)
    )
    return heading.locator("xpath=ancestor::mat-card[1]")


async def _open_form67_view_all(page: Page) -> str:
    """Return ``opened``, ``not_filed``, or ``drift``."""
    card = _form67_card(page)
    if await card.count() == 0:
        logger.info("No Form 67 card on View Filed Forms")
        return "not_filed"
    link = card.first.locator("span.hyperLink").filter(has_text="View All")
    try:
        logger.info("Opening Form 67 View All")
        await link.first.click()
        await page.locator(YEAR_LIST_SELECTOR).wait_for(
            state="visible",
            timeout=_PAGE_TIMEOUT_MS,
        )
    except PlaywrightTimeout:
        logger.warning("Form 67 year list did not appear url=%s", page.url)
        return "drift"
    return "opened"


async def run_open_form67_year_list(page: Page | None) -> str:
    """``opened``, ``not_filed``, or ``drift``. Dry-run returns ``opened``."""
    if settings.PORTAL_AUTOMATION_DRY_RUN:
        logger.info("Dry-run: skip View Filed Forms")
        return "opened"
    if page is None:
        logger.warning("No browser page; skip View Filed Forms")
        return "drift"
    try:
        await _clear_stuck_login_card(page)
        await _click_view_filed_forms(page)
        await page.locator(FORMS_PAGE_SELECTOR).wait_for(
            state="visible",
            timeout=_PAGE_TIMEOUT_MS,
        )
        kind = await _forms_page_kind(page)
        if kind == "empty":
            return "not_filed"
        if kind != "list":
            return "drift"
        if not await _open_1961_tab(page):
            return "drift"
        opened = await _open_form67_view_all(page)
        if opened == "opened":
            logger.info("Opened Form 67 year list url=%s", page.url)
        return opened
    except PlaywrightTimeout:
        logger.warning("View Filed Forms timed out url=%s", page.url)
        return "drift"
