"""Launch a desktop Chrome context for the Income Tax e-filing portal.

Playwright's bundled Chromium is often served "Permission Denied!!" by the
portal. Prefer the installed Google Chrome channel when present.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.config import settings
from app.portal.network import attach_network_log

logger = logging.getLogger(__name__)

_CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)


async def launch_portal_page(playwright: Any) -> tuple[Any, Any]:
    """Return (browser, page). Caller must close the browser."""
    launch_kwargs: dict[str, Any] = {
        "headless": settings.PLAYWRIGHT_HEADLESS,
        "args": ["--disable-blink-features=AutomationControlled"],
        "ignore_default_args": ["--enable-automation"],
    }
    try:
        browser = await playwright.chromium.launch(channel="chrome", **launch_kwargs)
        logger.info("Launched installed Google Chrome")
    except Exception:
        logger.warning(
            "Google Chrome not available; using Playwright Chromium (portal may block it)"
        )
        browser = await playwright.chromium.launch(**launch_kwargs)

    context = await browser.new_context(
        user_agent=_CHROME_UA,
        locale="en-IN",
        timezone_id="Asia/Kolkata",
        viewport={"width": 1440, "height": 900},
    )
    await context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    )
    page = await context.new_page()
    attach_network_log(page)
    return browser, page
