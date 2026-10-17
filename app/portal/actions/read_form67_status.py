"""Read Form 67 filing for one assessment year from View Filed Forms.

Path after login:
  e-File → Income Tax Forms → View Filed Forms
  tab Forms as per Income Tax Act 1961
  Form 67 card → View All
  Filter by Assessment Year (close the dropdown, then Filter)
  View Details on the latest Filing Date

``form67_filed`` is true only when that page is Form 67 for the job year
and Status is Successfully e-Verified. No card for the year is false.
Any other status leaves the checkbox unchanged (``form67_filed`` is null).
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

_PAGE_TIMEOUT_MS = 20_000
_FILTER_TIMEOUT_MS = 15_000
_E_VERIFIED = "successfully e-verified"
_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_AY_RE = re.compile(r"A\.Y\.?\s*(\d{4}-\d{2})", re.IGNORECASE)
_FORM67_RE = re.compile(r"\bForm\s+67\b", re.IGNORECASE)
_FILING_DATE_RE = re.compile(
    r"Filing Date\s*:?\s*(\d{1,2}-[A-Za-z]{3}-\d{4})",
    re.IGNORECASE,
)
_DMY_RE = re.compile(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})")
_ACTIVITY_RE = re.compile(
    r"Filing Date\s*:?\s*(\d{1,2}-[A-Za-z]{3}-\d{4})"
    r".*?Filing Type\s*:?\s*([A-Za-z]+)"
    r".*?Status\s*:?\s*(.+?)"
    r"\s+Acknowledgement No\.?\s*:?\s*(\d+)"
    r".*?Filed By\s*:?\s*(\S+)",
    re.IGNORECASE | re.DOTALL,
)
_RESULT_COUNT_RE = re.compile(r"(\d+)\s+result\(s\)\s+found", re.IGNORECASE)

# The year list's Filter control is this button, not .filterStyle.
# .filterStyle exists only in the page stylesheet.
FILTER_OPEN_SELECTOR = "button.filterIcon"
FILTER_APPLY_SELECTOR = 'button[name="searchValue"]'
YEAR_SELECT_SELECTOR = 'mat-select[formcontrolname="year"]'
YEAR_OPTION_SELECTOR = '.mat-mdc-select-panel[aria-multiselectable="true"] mat-option'
ACTIVITY_SELECTOR = "app-filed-form-activity-status"
YEAR_LIST_SELECTOR = "app-filed-form-ass-year-det"
FORMS_PAGE_SELECTOR = "app-view-filed-forms"
TAB_1961 = "Forms as per Income Tax Act 1961"


@dataclass(frozen=True)
class Form67Activity:
    is_form67: bool
    assessment_year: Optional[str]
    status: Optional[str]
    acknowledgement_no: Optional[str]
    filing_date: Optional[str]
    filing_type: Optional[str]
    filed_by: Optional[str]


def _flat(text: str) -> str:
    return " ".join((text or "").split())


def is_successfully_e_verified(status: str) -> bool:
    return _E_VERIFIED in _flat(status).lower()


def parse_portal_dmy(text: str) -> Optional[datetime]:
    match = _DMY_RE.search(text or "")
    if match is None:
        return None
    month = _MONTHS.get(match.group(2).lower())
    if month is None:
        return None
    return datetime(int(match.group(3)), month, int(match.group(1)))


def filing_date_in_text(text: str) -> Optional[datetime]:
    match = _FILING_DATE_RE.search(_flat(text))
    if match is None:
        return None
    return parse_portal_dmy(match.group(1))


def parse_activity_text(text: str) -> Form67Activity:
    """Read the View Details page. Labels and values may be on separate lines."""
    flat = _flat(text)
    ay_match = _AY_RE.search(flat)
    fields = _ACTIVITY_RE.search(flat)
    if fields is None:
        status_match = re.search(
            r"Status\s*:?\s*(.+?)(?:\s+Acknowledgement|\s+Filed By|\s+Activity|$)",
            flat,
            re.IGNORECASE,
        )
        return Form67Activity(
            is_form67=bool(_FORM67_RE.search(flat)),
            assessment_year=ay_match.group(1) if ay_match else None,
            status=_flat(status_match.group(1)) if status_match else None,
            acknowledgement_no=None,
            filing_date=None,
            filing_type=None,
            filed_by=None,
        )
    return Form67Activity(
        is_form67=bool(_FORM67_RE.search(flat)),
        assessment_year=ay_match.group(1) if ay_match else None,
        status=_flat(fields.group(3)),
        acknowledgement_no=fields.group(4),
        filing_date=fields.group(1),
        filing_type=fields.group(2),
        filed_by=fields.group(5),
    )


def activity_verdict(text: str, requested_ay: str) -> str:
    """``filed``, ``not_e_verified``, or ``mismatch``."""
    parsed = parse_activity_text(text)
    if not parsed.is_form67 or parsed.assessment_year != requested_ay.strip():
        return "mismatch"
    if parsed.status and is_successfully_e_verified(parsed.status):
        return "filed"
    if parsed.status:
        return "not_e_verified"
    return "mismatch"


def result_count(text: str) -> Optional[int]:
    match = _RESULT_COUNT_RE.search(_flat(text))
    if match is None:
        return None
    return int(match.group(1))


def _empty_result(assessment_year: str, **extra: object) -> dict[str, object]:
    return {
        "assessment_year": assessment_year,
        "form67_filed": None,
        "form67_status": None,
        "acknowledgement_no": None,
        "filing_date": None,
        "filing_type": None,
        "filed_by": None,
        **extra,
    }


def not_filed_result(assessment_year: str) -> dict[str, object]:
    return _empty_result(assessment_year, form67_filed=False)


def activity_result(text: str, requested_ay: str) -> dict[str, object]:
    parsed = parse_activity_text(text)
    verdict = activity_verdict(text, requested_ay)
    filed: Optional[bool]
    if verdict == "filed":
        filed = True
    elif verdict == "not_e_verified":
        filed = None
    else:
        filed = None
    return {
        "assessment_year": requested_ay.strip(),
        "form67_filed": filed,
        "form67_status": parsed.status,
        "acknowledgement_no": parsed.acknowledgement_no,
        "filing_date": parsed.filing_date,
        "filing_type": parsed.filing_type,
        "filed_by": parsed.filed_by,
        "activity_verdict": verdict,
    }


async def _option_checked(option: Locator) -> bool:
    """Multi-select rows use aria-selected and a pseudo checkbox, not an input."""
    if (await option.get_attribute("aria-selected")) == "true":
        return True
    box = option.locator("input.mdc-checkbox__native-control")
    if await box.count():
        return await box.first.is_checked()
    return False


async def _dismiss_year_overlay(page: Page) -> None:
    """Close the year list. The Filter button stays blocked while it is open."""
    label = page.locator("mat-label.filterLabel").filter(has_text="Assessment Year")
    try:
        if await label.count():
            await label.first.click(timeout=3_000, force=True)
    except PlaywrightError:
        logger.info("Assessment Year label click did not close the list")
    try:
        await page.keyboard.press("Escape")
    except PlaywrightError:
        pass
    panel = page.locator(YEAR_OPTION_SELECTOR)
    try:
        if await panel.count():
            await panel.first.wait_for(state="hidden", timeout=5_000)
    except PlaywrightTimeoutError:
        logger.info("Assessment year list stayed open")


async def _select_only_year(page: Page, ay: str) -> None:
    select = page.locator(YEAR_SELECT_SELECTOR).filter(visible=True).first
    await select.wait_for(state="visible", timeout=_FILTER_TIMEOUT_MS)
    await select.click(force=True)
    options = page.locator(YEAR_OPTION_SELECTOR)
    await options.first.wait_for(state="visible", timeout=_FILTER_TIMEOUT_MS)
    count = await options.count()
    found = False
    target_checked = False
    for index in range(count):
        options = page.locator(YEAR_OPTION_SELECTOR)
        if await options.count() <= index:
            break
        option = options.nth(index)
        label = _flat(await option.inner_text())
        wanted = label == ay
        checked = await _option_checked(option)
        if wanted:
            found = True
        if wanted == checked:
            if wanted:
                target_checked = True
            continue
        await option.click(force=True)
        if wanted:
            target_checked = True
        if await page.locator(YEAR_OPTION_SELECTOR).count() == 0:
            break
    if not found or not target_checked:
        raise PlaywrightTimeoutError(f"Assessment year {ay} is not in the filter")
    if await page.locator(YEAR_OPTION_SELECTOR).count():
        await _dismiss_year_overlay(page)


async def _apply_ay_filter(page: Page, ay: str) -> bool:
    try:
        logger.info("Opening Form 67 filter for A.Y. %s", ay)
        open_btn = (
            page.locator(YEAR_LIST_SELECTOR)
            .locator(FILTER_OPEN_SELECTOR)
            .filter(visible=True)
        )
        await open_btn.first.click(force=True, timeout=_FILTER_TIMEOUT_MS)
        await page.locator(YEAR_SELECT_SELECTOR).filter(visible=True).first.wait_for(
            state="visible",
            timeout=_FILTER_TIMEOUT_MS,
        )
        await _select_only_year(page, ay)
        logger.info("Applying Form 67 filter")
        apply_btn = page.locator(FILTER_APPLY_SELECTOR).filter(visible=True)
        await apply_btn.first.click(timeout=_FILTER_TIMEOUT_MS)
        await page.locator(YEAR_SELECT_SELECTOR).first.wait_for(
            state="hidden",
            timeout=_FILTER_TIMEOUT_MS,
        )
        return True
    except (PlaywrightTimeoutError, PlaywrightError):
        logger.warning("Form 67 assessment-year filter did not complete", exc_info=True)
        return False


async def _cards_for_year(page: Page, ay: str) -> Locator:
    pattern = re.compile(rf"A\.Y\.?\s*{re.escape(ay)}")
    return page.locator(f"{YEAR_LIST_SELECTOR} mat-card").filter(has_text=pattern)


async def _latest_card(cards: Locator) -> Optional[Locator]:
    count = await cards.count()
    if count == 0:
        return None
    best: Optional[Locator] = None
    best_date: Optional[datetime] = None
    for index in range(count):
        card = cards.nth(index)
        parsed = filing_date_in_text(await card.inner_text())
        if parsed is None:
            logger.warning("Form 67 card %s has no Filing Date", index)
            continue
        if best_date is None or parsed > best_date:
            best_date = parsed
            best = card
    if best is None and count == 1:
        return cards.first
    return best


async def _open_view_details(card: Locator, page: Page) -> bool:
    link = card.locator("span.hyperLink").filter(has_text="View Details")
    try:
        await link.first.click()
        await page.locator(ACTIVITY_SELECTOR).wait_for(
            state="visible",
            timeout=_PAGE_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError:
        logger.warning("Form 67 activity page did not appear")
        return False
    return True


async def read_form67_on_year_list(
    page: Page,
    assessment_year: str,
) -> dict[str, object]:
    """Filter the open Form 67 year list and read View Details when a card exists."""
    ay = assessment_year.strip()
    if not await _apply_ay_filter(page, ay):
        return _empty_result(ay, filter_failed=True)

    list_text = await page.locator(YEAR_LIST_SELECTOR).inner_text()
    count = result_count(list_text)
    cards = await _cards_for_year(page, ay)
    card_count = await cards.count()
    if count is not None and count > 0 and card_count == 0:
        logger.warning(
            "Form 67 filter reported %s result(s) but no A.Y. %s card",
            count,
            ay,
        )
        return _empty_result(ay, filter_failed=True)
    if card_count == 0:
        logger.info("No Form 67 filing for A.Y. %s", ay)
        return not_filed_result(ay)

    card = await _latest_card(cards)
    if card is None:
        logger.info("Form 67 cards for A.Y. %s have no Filing Date", ay)
        return _empty_result(ay, details_failed=True)
    if not await _open_view_details(card, page):
        return _empty_result(ay, details_failed=True)

    activity_text = await page.locator(ACTIVITY_SELECTOR).inner_text()
    result = activity_result(activity_text, ay)
    logger.info(
        "Form 67 A.Y. %s verdict=%s status=%s ack=%s",
        ay,
        result.get("activity_verdict"),
        result.get("form67_status"),
        result.get("acknowledgement_no"),
    )
    return result


async def read_form67_status(
    page: Page | None,
    assessment_year: str,
) -> dict[str, object]:
    ay = assessment_year.strip()
    if settings.PORTAL_AUTOMATION_DRY_RUN:
        return _empty_result(ay, form67_status="dry-run")
    if page is None:
        return _empty_result(ay, form67_status="no page")
    return await read_form67_on_year_list(page, ay)
