"""Read e-verification from View Filed Returns via Filter + View Details.

Portal Filter is assessment year (A.Y. ``2026-27``). The job stores that
portal AY in ``assessment_year`` — use it as-is. Do not FY+1 (that would
open the next year on the portal).

List page (``#/dashboard/itrStatus``, ``app-itr-status``):
  1. Open Filter (``button#filterbtn1``)
  2. Select AY, click Assessment Year label to close the overlay, apply
     (``button#okButton``)
  3. Among remaining cards, open View Details on the latest Filing Date

Lifecycle page (``app-itr-status-life-cycle``): scrape every
``div.matStepStatus``. e-verified if any label contains
``Successfully e-verified``. ``itr_status`` is the first (latest) step.

JSON XHR is deferred; this scrape is the source of truth for now.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.core.config import settings

logger = logging.getLogger(__name__)

E_VERIFIED_LABEL = "Successfully e-verified"
FILTER_OPEN_SELECTOR = "button#filterbtn1"
FILTER_APPLY_SELECTOR = "button#okButton"
FILTER_PANEL_SELECTOR = "div.filterBox"
AY_SELECT_SELECTOR = 'div.filterBox mat-select[formcontrolname="ay"]'
AY_FILTER_LABEL_SELECTOR = "mat-label.filterLabel"
OVERLAY_BACKDROP_SELECTOR = (
    "div.cdk-overlay-backdrop.cdk-overlay-backdrop-showing"
)
VIEW_DETAILS_SELECTOR = "span.hyperLink"
LIFECYCLE_SELECTOR = "app-itr-status-life-cycle"
CARD_SELECTOR = "mat-card.contextBox"
STEP_SELECTOR = "div.matStepStatus"
# Live portal: " Filing Date : " inside div.valueBox. Match on the box
# text — do not filter(has=card-scoped locator); that misses the date.
_FILING_DATE_BOX_RE = re.compile(r"Filing Date\s*:")

_PAGE_TIMEOUT_MS = 20_000
_CARD_TIMEOUT_MS = 15_000
_FILTER_TIMEOUT_MS = 10_000

_MONTHS = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}
_FILING_DATE_RE = re.compile(
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"\s+(\d{1,2}),\s+(\d{4})"
)


def portal_assessment_year(job_year: str) -> str:
    """Job ``assessment_year`` is portal AY (``2026-27``). Do not FY+1."""
    return job_year.strip()


@dataclass(frozen=True)
class FiledReturnStatus:
    assessment_year: str
    found: bool
    e_verified: bool
    portal_status: Optional[str]
    step_labels: tuple[str, ...]


def _ay_heading_pattern(assessment_year: str) -> re.Pattern[str]:
    ay = assessment_year.strip()
    return re.compile(rf"A\.Y\.\s+{re.escape(ay)}")


def parse_filing_date(text: str) -> Optional[datetime]:
    match = _FILING_DATE_RE.search(text)
    if match is None:
        return None
    return datetime(
        int(match.group(3)),
        _MONTHS[match.group(1)],
        int(match.group(2)),
    )


async def wait_for_filed_returns_page(page: Page) -> bool:
    try:
        await page.locator("app-itr-status").wait_for(
            state="visible",
            timeout=_PAGE_TIMEOUT_MS,
        )
        await page.locator("mat-label.ServiceHeading").filter(
            has_text="View Filed Returns"
        ).wait_for(state="visible", timeout=10_000)
    except PlaywrightTimeoutError:
        logger.warning("View Filed Returns page did not appear")
        return False
    return True


def _cards_for_ay(page: Page, assessment_year: str) -> Locator:
    heading = page.locator("mat-label.contentHeadingText").filter(
        has_text=_ay_heading_pattern(assessment_year)
    )
    return page.locator(CARD_SELECTOR).filter(has=heading)


async def _step_labels(root: Locator) -> tuple[str, ...]:
    nodes = root.locator(STEP_SELECTOR)
    count = await nodes.count()
    labels: list[str] = []
    for index in range(count):
        text = (await nodes.nth(index).inner_text()).strip()
        if text:
            labels.append(text)
    return tuple(labels)


async def _filing_date_of(card: Locator) -> Optional[datetime]:
    box = card.locator("div.valueBox").filter(has_text=_FILING_DATE_BOX_RE)
    if await box.count() == 0:
        return None
    text = (await box.first.locator("mat-label.fieldVal").inner_text()).strip()
    return parse_filing_date(text)


async def _latest_filing_card(
    cards: Locator,
) -> Optional[Locator]:
    count = await cards.count()
    if count == 0:
        return None
    best: Optional[Locator] = None
    best_date: Optional[datetime] = None
    for index in range(count):
        card = cards.nth(index)
        parsed = await _filing_date_of(card)
        if parsed is None:
            logger.warning("Card %s has no Filing Date", index)
            continue
        logger.info("Card %s Filing Date %s", index, parsed.date())
        if best_date is None or parsed > best_date:
            best_date = parsed
            best = card
    if best is None and count == 1:
        return cards.first
    return best


async def _dismiss_year_overlay(page: Page) -> None:
    label = page.locator(AY_FILTER_LABEL_SELECTOR).filter(
        has_text="Assessment Year"
    )
    try:
        await label.click(timeout=5_000)
    except PlaywrightError:
        logger.info("Assessment Year label click did not land")
    backdrop = page.locator(OVERLAY_BACKDROP_SELECTOR)
    if await backdrop.count() == 0:
        return
    if await backdrop.last.is_visible():
        await backdrop.last.click()
        await backdrop.last.wait_for(state="hidden", timeout=5_000)


async def _select_assessment_year(page: Page, ay: str) -> None:
    select = page.locator(AY_SELECT_SELECTOR)
    await select.wait_for(state="visible", timeout=_FILTER_TIMEOUT_MS)
    await select.click()
    option = page.locator("mat-option").filter(
        has_text=re.compile(rf"^\s*{re.escape(ay)}\s*$")
    )
    await option.first.wait_for(state="visible", timeout=_FILTER_TIMEOUT_MS)
    await option.first.click()
    await _dismiss_year_overlay(page)


async def _apply_ay_filter(page: Page, assessment_year: str) -> bool:
    ay = assessment_year.strip()
    try:
        logger.info("Opening Filter for A.Y. %s", ay)
        await page.locator(FILTER_OPEN_SELECTOR).click()
        await page.locator(FILTER_APPLY_SELECTOR).wait_for(
            state="visible",
            timeout=_FILTER_TIMEOUT_MS,
        )
        await page.locator(FILTER_PANEL_SELECTOR).wait_for(
            state="visible",
            timeout=_FILTER_TIMEOUT_MS,
        )
        await _select_assessment_year(page, ay)
        logger.info("Applying Filter")
        await page.locator(FILTER_APPLY_SELECTOR).click(
            timeout=_FILTER_TIMEOUT_MS
        )
        await page.locator(FILTER_APPLY_SELECTOR).wait_for(
            state="hidden",
            timeout=_FILTER_TIMEOUT_MS,
        )
        return True
    except (PlaywrightTimeoutError, PlaywrightError):
        logger.warning("Assessment Year filter did not complete", exc_info=True)
        try:
            apply_btn = page.locator(FILTER_APPLY_SELECTOR)
            if await apply_btn.count() and await apply_btn.first.is_visible():
                await page.locator(FILTER_OPEN_SELECTOR).click()
        except PlaywrightError:
            pass
        return False


async def _open_view_details(card: Locator, page: Page) -> bool:
    link = card.locator(VIEW_DETAILS_SELECTOR).filter(
        has_text="View Details"
    )
    try:
        await link.first.click()
        await page.locator(LIFECYCLE_SELECTOR).wait_for(
            state="visible",
            timeout=_PAGE_TIMEOUT_MS,
        )
        await page.locator(LIFECYCLE_SELECTOR).locator(
            STEP_SELECTOR
        ).first.wait_for(state="visible", timeout=_CARD_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        logger.warning("Lifecycle page did not appear after View Details")
        return False
    logger.info("Opened View Details url=%s", page.url)
    return True


def _not_found(ay: str) -> FiledReturnStatus:
    return FiledReturnStatus(ay, False, False, None, ())


async def read_status_for_assessment_year(
    page: Page,
    assessment_year: str,
) -> FiledReturnStatus:
    ay = portal_assessment_year(assessment_year)
    logger.info("Portal A.Y. %s", ay)
    try:
        await page.locator(CARD_SELECTOR).first.wait_for(
            state="visible",
            timeout=_CARD_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError:
        logger.warning("No filed-return cards on the page")
        return _not_found(ay)

    await _apply_ay_filter(page, ay)

    cards = _cards_for_ay(page, ay)
    try:
        await cards.first.wait_for(
            state="visible",
            timeout=_CARD_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError:
        logger.info("No card for A.Y. %s after Filter", ay)
        return _not_found(ay)

    card = await _latest_filing_card(cards)
    if card is None:
        logger.info("No Filing Date on cards for A.Y. %s", ay)
        return _not_found(ay)

    if not await _open_view_details(card, page):
        return FiledReturnStatus(ay, True, False, None, ())

    lifecycle = page.locator(LIFECYCLE_SELECTOR)
    labels = await _step_labels(lifecycle)
    e_verified = any(
        E_VERIFIED_LABEL.lower() in label.lower() for label in labels
    )
    portal_status = labels[0] if labels else None
    logger.info(
        "A.Y. %s e_verified=%s portal_status=%s steps=%s",
        ay,
        e_verified,
        portal_status,
        labels,
    )
    return FiledReturnStatus(
        assessment_year=ay,
        found=True,
        e_verified=e_verified,
        portal_status=portal_status,
        step_labels=labels,
    )


def status_as_result(status: FiledReturnStatus) -> dict[str, object]:
    """Shape stored on job.result. Does not write filing_status."""
    if not status.found:
        return {
            "assessment_year": status.assessment_year,
            "e_verified": None,
            "itr_status": (
                f"No filing found for AY {status.assessment_year}"
            ),
        }
    return {
        "assessment_year": status.assessment_year,
        "e_verified": status.e_verified,
        "itr_status": status.portal_status or "Unknown",
    }


async def read_itr_status(
    page: Page | None, assessment_year: str
) -> dict[str, object]:
    if settings.PORTAL_AUTOMATION_DRY_RUN:
        return {
            "assessment_year": assessment_year,
            "e_verified": None,
            "itr_status": "dry-run",
        }
    if page is None:
        return {
            "assessment_year": assessment_year,
            "e_verified": None,
            "itr_status": "no page",
        }
    status = await read_status_for_assessment_year(page, assessment_year)
    return status_as_result(status)
