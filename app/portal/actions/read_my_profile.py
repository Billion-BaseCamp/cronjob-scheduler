"""Read the PDF password date from e-filing My Profile.

Profile menu → My Profile → Personal Details. The value next to Date of
Birth (or Date of Incorporation / Formation for a non-individual) is the
only date used. clients.date_of_birth is not read and is not updated.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from app.portal.actions.logout import PROFILE_MENU_SELECTOR
from app.portal.foreign_assets import (
    PROFILE_DATE_LABELS,
    normalize_pan,
    parse_portal_date,
)

logger = logging.getLogger(__name__)

_MENU_TIMEOUT_MS = 20_000
_PAGE_TIMEOUT_MS = 20_000
_MY_PROFILE = re.compile(r"^\s*My Profile\s*$")


@dataclass
class ProfileDate:
    pan: str | None = None
    born_on: date | None = None
    error_code: str | None = None
    message: str | None = None


def _label_pattern(label: str) -> re.Pattern[str]:
    return re.compile(rf"^\s*{re.escape(label)}\s*$")


async def _sibling_value(page: Page, label: str) -> str | None:
    block = page.locator("div.body-2-text").filter(has_text=_label_pattern(label))
    if await block.count() == 0:
        return None
    value = block.first.locator(
        "xpath=following-sibling::div[contains(@class,'subtitle-2-text')]"
    )
    try:
        await value.first.wait_for(state="visible", timeout=5_000)
    except PlaywrightTimeout:
        return None
    return (await value.first.inner_text()).strip()


async def read_my_profile(page: Page) -> ProfileDate:
    """Open My Profile and return the portal PAN and profile date."""
    try:
        logger.info("Opening profile menu for My Profile")
        await page.locator(PROFILE_MENU_SELECTOR).click()
        item = page.locator("button.mat-mdc-menu-item").filter(has_text=_MY_PROFILE)
        await item.first.wait_for(state="visible", timeout=_MENU_TIMEOUT_MS)
        await item.first.click()
        await page.locator("div.body-2-text").filter(
            has_text=re.compile(
                r"Date of Birth|Date of Incorporation|Date of Formation"
            )
        ).first.wait_for(state="visible", timeout=_PAGE_TIMEOUT_MS)
    except PlaywrightTimeout:
        logger.warning("My Profile did not open url=%s", page.url)
        return ProfileDate(
            error_code="UI_DRIFT",
            message="Could not open My Profile from the profile menu.",
        )

    born_on = None
    for label in PROFILE_DATE_LABELS:
        raw = await _sibling_value(page, label)
        if raw is None:
            continue
        born_on = parse_portal_date(raw)
        if born_on is not None:
            logger.info("Read portal profile date label=%s", label)
            break
        logger.warning("Profile label %s is not a full date: %s", label, raw)
        return ProfileDate(
            error_code="PROFILE_DATE_UNREADABLE",
            message=(
                "My Profile showed a date that is not a full day, month, "
                "and year. The portal date is required for the PDF password."
            ),
        )

    if born_on is None:
        return ProfileDate(
            error_code="PROFILE_DATE_UNREADABLE",
            message=(
                "My Profile did not show a date of birth or date of "
                "incorporation. The portal date is required for the PDF password."
            ),
        )

    pan = normalize_pan(await _sibling_value(page, "PAN"))
    return ProfileDate(pan=pan, born_on=born_on)
