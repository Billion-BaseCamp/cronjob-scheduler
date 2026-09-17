"""Log out of the Income Tax e-filing portal.

1. Click the profile menu (top-right name)
2. Click Log Out
3. Wait until the portal leaves the logged-in session

After Log Out the portal goes to #/feedback/logout (not the PAN form).
That URL is a successful logout.
"""

from __future__ import annotations

import logging

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from app.core.config import settings

logger = logging.getLogger(__name__)

PROFILE_MENU_SELECTOR = "button.profileMenubtn"
LOGOUT_MENU_ITEM_SELECTOR = (
    'button.mat-mdc-menu-item:has-text("Log Out")'
)
# Live portal after Log Out (operator inspect).
LOGGED_OUT_HASH = "/feedback/logout"


async def run_logout(page: Page | None) -> bool:
    """Return True if the portal left the logged-in session."""
    if settings.PORTAL_AUTOMATION_DRY_RUN:
        logger.info("Dry-run: skip logout")
        return True
    if page is None:
        logger.warning("No browser page; skip logout")
        return False
    try:
        logger.info("Opening profile menu")
        await page.locator(PROFILE_MENU_SELECTOR).click()

        logger.info("Clicking Log Out")
        menu_item = page.locator(LOGOUT_MENU_ITEM_SELECTOR)
        await menu_item.wait_for(state="visible")
        await menu_item.click()

        await page.wait_for_function(
            """(needle) => (location.hash || '').includes(needle)""",
            arg=LOGGED_OUT_HASH,
            timeout=20_000,
        )
        logger.info("Logged out url=%s", page.url)
        return True
    except PlaywrightTimeout:
        logger.warning("Logout timed out url=%s", page.url)
        return False
