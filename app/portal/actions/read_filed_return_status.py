"""Read e-verification from View Filed Returns via Filter + View Details.

Portal Filter is assessment year (A.Y. ``2026-27``). The job stores that
portal AY in ``assessment_year`` — use it as-is. Do not FY+1 (that would
open the next year on the portal).

List page (``#/dashboard/itrStatus``, ``app-itr-status``):
  1. Set Items per page to the largest value. Then click the footer
     View More (the button beside Back, not the one inside a card) and
     the next-page arrow until neither is available. Page 1 has no
     View More; it appears on the last page of the short list, and that
     click grows the list so the year filter contains every year.
  2. Open Filter (``button#filterbtn1``)
  3. Select AY, click Assessment Year label to close the overlay, apply
     (``button#okButton``)
  4. Among remaining cards, open View Details on the latest Filing Date

Lifecycle page (``app-itr-status-life-cycle``): read Filing Type from
``div.ftype mat-label.leftSideVal`` next to the ``Filing Type`` label, and
scrape every ``div.matStepStatus``. e-verified when Filing Type is Defective
or Rectification, or a step contains ``Successfully e-verified`` or
``ITR-V received`` (hyphen optional). ``itr_status`` is the first (latest)
step.

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
# Defective / Rectification exist only after the return was already verified.
_VERIFIED_FILING_TYPES = frozenset({"defective", "rectification"})
_ITRV_RECEIVED = "itrv received"
_FILING_TYPE_LABEL_RE = re.compile(r"Filing Type")
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


def _step_counts_as_verified(label: str) -> bool:
    folded = label.casefold()
    if E_VERIFIED_LABEL.casefold() in folded:
        return True
    return _ITRV_RECEIVED in folded.replace("-", "")


def is_e_verified(filing_type: str, labels: tuple[str, ...]) -> bool:
    """True when the details page shows a verified filing or timeline step."""
    if filing_type.strip().casefold() in _VERIFIED_FILING_TYPES:
        return True
    return any(_step_counts_as_verified(label) for label in labels)


async def _filing_type(root: Locator) -> str:
    """Filing Type value on the details page (``Original``, ``Rectification``)."""
    block = root.locator("div.brdrRight").filter(has_text=_FILING_TYPE_LABEL_RE)
    value = block.locator("div.ftype mat-label.leftSideVal")
    if await value.count() == 0:
        return ""
    return (await value.first.inner_text()).strip()


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


_SELECT_PANEL = (
    ".cdk-overlay-pane .mat-mdc-select-panel, "
    ".cdk-overlay-pane .mat-select-panel"
)
FILTER_APPLIED = "applied"
FILTER_NOT_LISTED = "not_listed"
FILTER_FAILED = "failed"


def _year_option(page: Page, ay: str) -> Locator:
    return page.locator("mat-option").filter(
        has_text=re.compile(rf"^\s*{re.escape(ay)}\s*$")
    )


async def _option_shown(option: Locator) -> bool:
    try:
        return await option.count() > 0 and await option.first.is_visible()
    except PlaywrightError:
        return False


async def _scroll_year_panel(page: Page) -> bool:
    panel = page.locator(_SELECT_PANEL)
    if await panel.count() == 0:
        return False
    return bool(
        await panel.first.evaluate(
            """el => {
                const before = el.scrollTop;
                const step = Math.max(el.clientHeight - 8, 40);
                el.scrollTop = Math.min(el.scrollTop + step, el.scrollHeight);
                return el.scrollTop !== before;
            }"""
        )
    )


async def _close_filter_dialog(page: Page) -> None:
    try:
        await page.keyboard.press("Escape")
    except PlaywrightError:
        pass
    try:
        apply_btn = page.locator(FILTER_APPLY_SELECTOR)
        if await apply_btn.count() and await apply_btn.first.is_visible():
            await page.locator(FILTER_OPEN_SELECTOR).click()
    except PlaywrightError:
        pass


async def _select_assessment_year(page: Page, ay: str) -> bool:
    """Select the year. False means the open list does not contain it."""
    select = page.locator(AY_SELECT_SELECTOR)
    await select.wait_for(state="visible", timeout=_FILTER_TIMEOUT_MS)
    await select.click()
    option = _year_option(page, ay)
    for _ in range(40):
        if await _option_shown(option):
            await option.first.click()
            await _dismiss_year_overlay(page)
            return True
        if not await _scroll_year_panel(page):
            break
        await page.wait_for_timeout(100)
    seen: list[str] = []
    try:
        nodes = page.locator("mat-option")
        count = min(await nodes.count(), 40)
        for index in range(count):
            text = " ".join((await nodes.nth(index).inner_text()).split())
            if text:
                seen.append(text)
    except PlaywrightError:
        pass
    logger.info("A.Y. %s is not in the filter. Options seen: %s", ay, seen)
    await _dismiss_year_overlay(page)
    return False


_VIEW_MORE_RE = re.compile(r"^\s*View\s+More\s*$", re.I)
_RANGE_OF_RE = re.compile(r"(\d+)\s*[–-]\s*(\d+)\s+of\s+(\d+)", re.I)
_PAGES_RE = re.compile(r"(\d+)\s+of\s+(\d+)\s+pages?", re.I)
_LIST_PAGINATOR = "app-itr-status mat-paginator"
_PAGE_SIZE_SELECT = (
    ".mat-mdc-paginator-page-size mat-select, .mat-paginator-page-size mat-select"
)
_PAGE_SIZE_TOUCH = ".mat-mdc-paginator-page-size .mat-mdc-paginator-touch-target"
_PAGE_SIZE_VALUE = ".mat-mdc-select-value-text, .mat-select-value-text"
_PAGE_SIZE_OPTION = ".cdk-overlay-pane mat-option"
_NEXT_PAGE = (
    "button.mat-mdc-paginator-navigation-next, "
    "button.mat-paginator-navigation-next"
)
_RANGE_LABEL = ".mat-mdc-paginator-range-label, .mat-paginator-range-label"
_VIEW_MORE_CONTAINER = "app-itr-status .viewMoreBtnContainer button"
_LOAD_STEPS = 30


def _page_size_number(text: str) -> int | None:
    digits = re.sub(r"\D", "", text or "")
    if not digits:
        return None
    return int(digits)


def _range_has_more(text: str) -> bool:
    """True when the pager label still has a later page or a later item."""
    match = _RANGE_OF_RE.search(text or "")
    if match is not None and int(match.group(2)) < int(match.group(3)):
        return True
    pages = _PAGES_RE.search(text or "")
    return pages is not None and int(pages.group(1)) < int(pages.group(2))


def _list_paginator(page: Page) -> Locator:
    return page.locator(_LIST_PAGINATOR)


async def _scroll_list_footer(page: Page) -> None:
    try:
        await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
    except PlaywrightError:
        pass
    paginator = _list_paginator(page)
    if await paginator.count() == 0:
        return
    try:
        await paginator.last.scroll_into_view_if_needed(timeout=2_000)
    except PlaywrightError:
        pass


async def _dom_disabled(node: Locator) -> bool:
    """The pager button can sit in the DOM without a box Playwright calls visible."""
    try:
        return bool(
            await node.evaluate(
                """el => !!(
                    el.disabled
                    || el.getAttribute('aria-disabled') === 'true'
                    || el.classList.contains('mat-mdc-button-disabled')
                    || el.classList.contains('mat-button-disabled')
                )"""
            )
        )
    except PlaywrightError:
        return True


async def _click_control(node: Locator) -> None:
    try:
        await node.scroll_into_view_if_needed(timeout=2_000)
    except PlaywrightError:
        pass
    try:
        if await node.is_visible():
            await node.click(timeout=3_000)
            return
    except PlaywrightTimeoutError:
        pass
    await node.evaluate("el => el.click()")


async def _range_text(page: Page) -> str:
    label = _list_paginator(page).locator(_RANGE_LABEL)
    if await label.count() == 0:
        return ""
    try:
        return " ".join((await label.first.inner_text()).split())
    except PlaywrightError:
        return ""


async def _wait_range_change(page: Page, before: str) -> None:
    for _ in range(32):
        await page.wait_for_timeout(250)
        if await _range_text(page) != before:
            return


async def _close_page_size_menu(page: Page) -> None:
    backdrop = page.locator(OVERLAY_BACKDROP_SELECTOR)
    try:
        if await backdrop.count() == 0:
            return
        await page.keyboard.press("Escape")
        await backdrop.first.wait_for(state="hidden", timeout=5_000)
    except (PlaywrightTimeoutError, PlaywrightError):
        logger.warning("Items per page menu is still open", exc_info=True)


async def _shown_page_size(page: Page) -> int | None:
    value = _list_paginator(page).locator(_PAGE_SIZE_SELECT).locator(_PAGE_SIZE_VALUE)
    if await value.count() == 0:
        return None
    try:
        text = " ".join((await value.first.inner_text()).split())
    except PlaywrightError:
        return None
    return _page_size_number(text)


async def _open_page_size_menu(paginator: Locator) -> None:
    """The touch target is aria-hidden on View Filed Returns. Click the select."""
    select = paginator.locator(_PAGE_SIZE_SELECT)
    touch = paginator.locator(_PAGE_SIZE_TOUCH)
    if await touch.count():
        try:
            if await touch.first.is_visible():
                await touch.first.click(timeout=2_000)
                return
        except PlaywrightError:
            pass
    if await select.count() == 0:
        return
    try:
        await select.first.click(timeout=2_000)
    except PlaywrightTimeoutError:
        await select.first.evaluate("el => el.click()")


async def _set_largest_page_size(page: Page) -> None:
    """The short list is 6 per page. The largest option loads more before paging."""
    await _scroll_list_footer(page)
    paginator = _list_paginator(page)
    select = paginator.locator(_PAGE_SIZE_SELECT)
    try:
        if await select.count() == 0:
            return
        await _open_page_size_menu(paginator)
        options = page.locator(_PAGE_SIZE_OPTION)
        await options.first.wait_for(state="visible", timeout=_FILTER_TIMEOUT_MS)
        sizes: list[tuple[int, int]] = []
        count = await options.count()
        for index in range(count):
            text = " ".join((await options.nth(index).inner_text()).split())
            size = _page_size_number(text)
            if size is not None:
                sizes.append((size, index))
        if not sizes:
            return
        size, index = max(sizes)
        before = await _range_text(page)
        await options.nth(index).evaluate("el => el.click()")
        await page.locator(OVERLAY_BACKDROP_SELECTOR).first.wait_for(
            state="hidden", timeout=5_000
        )
        for _ in range(20):
            if await _shown_page_size(page) == size:
                break
            await page.wait_for_timeout(200)
        if await _range_text(page) == before:
            await page.wait_for_timeout(300)
        logger.info("Items per page set to %s", size)
    except (PlaywrightTimeoutError, PlaywrightError):
        logger.warning("Could not raise Items per page", exc_info=True)
    finally:
        await _close_page_size_menu(page)


async def _visible_view_more(nodes: Locator) -> Locator | None:
    count = await nodes.count()
    for index in range(count):
        node = nodes.nth(index)
        try:
            in_card = await node.evaluate("el => !!el.closest('mat-card')")
            if in_card or not await node.is_visible() or not await node.is_enabled():
                continue
            text = " ".join((await node.inner_text()).split())
        except PlaywrightError:
            continue
        if text.lower() != "view more":
            continue
        return node
    return None


async def _footer_view_more(page: Page) -> Locator | None:
    """The button beside Back. The View More inside a card only expands that card."""
    found = await _visible_view_more(page.locator(_VIEW_MORE_CONTAINER))
    if found is not None:
        return found
    nodes = page.locator("app-itr-status button").filter(has_text=_VIEW_MORE_RE)
    count = await nodes.count()
    for index in range(count):
        node = nodes.nth(index)
        try:
            beside_back = await node.evaluate(
                """el => {
                    if (el.closest('mat-card')) return false;
                    let parent = el.parentElement;
                    for (let depth = 0; depth < 4 && parent; depth += 1) {
                        const back = [...parent.querySelectorAll('button')].find(button =>
                            /^\\s*<?\\s*Back\\s*$/i.test(button.innerText || '')
                        );
                        if (back && back !== el && !back.closest('mat-card')) return true;
                        parent = parent.parentElement;
                    }
                    return false;
                }"""
            )
            if not beside_back or not await node.is_visible() or not await node.is_enabled():
                continue
        except PlaywrightError:
            continue
        return node
    return None


async def _enabled_next(page: Page) -> Locator | None:
    await _scroll_list_footer(page)
    buttons = _list_paginator(page).locator(_NEXT_PAGE)
    count = await buttons.count()
    fallback: Locator | None = None
    for index in range(count):
        node = buttons.nth(index)
        if await _dom_disabled(node):
            continue
        try:
            if await node.is_visible():
                return node
        except PlaywrightError:
            pass
        if fallback is None:
            fallback = node
    return fallback


async def _click_view_more(page: Page) -> None:
    """Load every filing before Filter. View More is absent until the last short page."""
    try:
        await _set_largest_page_size(page)
        seen: set[tuple[str, str]] = set()
        for _ in range(_LOAD_STEPS):
            await _scroll_list_footer(page)
            view_more = await _footer_view_more(page)
            nxt = await _enabled_next(page)
            before = await _range_text(page)
            if view_more is None and nxt is None:
                if _range_has_more(before):
                    logger.warning(
                        "Filed-return range %s still has another page, but Next is not usable",
                        before,
                    )
                else:
                    logger.info(
                        "Filed-return list is loaded (%s cards, %s)",
                        await page.locator(CARD_SELECTOR).count(),
                        before or "no range",
                    )
                return
            action = "View More" if view_more is not None else "next page"
            if (action, before) in seen:
                logger.info("Filed-return list stopped advancing at %s", before or "the start")
                return
            seen.add((action, before))
            target = view_more if view_more is not None else nxt
            if target is None:
                return
            logger.info("Loading filed returns via %s from %s", action, before or "the first page")
            await _click_control(target)
            await _wait_range_change(page, before)
        logger.warning("Stopped before every filed return was loaded")
    except (PlaywrightTimeoutError, PlaywrightError):
        logger.warning("Could not load every filed return before Filter", exc_info=True)


async def _apply_ay_filter(page: Page, assessment_year: str) -> str:
    """``applied``, ``not_listed`` when the year is absent, or ``failed``."""
    ay = assessment_year.strip()
    try:
        await _click_view_more(page)
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
        if not await _select_assessment_year(page, ay):
            await _close_filter_dialog(page)
            return FILTER_NOT_LISTED
        logger.info("Applying Filter")
        await page.locator(FILTER_APPLY_SELECTOR).click(
            timeout=_FILTER_TIMEOUT_MS
        )
        await page.locator(FILTER_APPLY_SELECTOR).wait_for(
            state="hidden",
            timeout=_FILTER_TIMEOUT_MS,
        )
        return FILTER_APPLIED
    except (PlaywrightTimeoutError, PlaywrightError):
        logger.warning("Assessment Year filter did not complete", exc_info=True)
        await _close_filter_dialog(page)
        return FILTER_FAILED


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

    if await _apply_ay_filter(page, ay) == FILTER_NOT_LISTED:
        logger.info("A.Y. %s is not in the filter list", ay)
        return _not_found(ay)

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
    filing_type = await _filing_type(lifecycle)
    e_verified = is_e_verified(filing_type, labels)
    portal_status = labels[0] if labels else None
    logger.info(
        "A.Y. %s e_verified=%s filing_type=%s portal_status=%s steps=%s",
        ay,
        e_verified,
        filing_type,
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


ITR_NOT_FILED = "ITR_NOT_FILED"
_NO_FILING_PREFIX = "No filing found for AY "


def no_filing_message(assessment_year: str) -> str:
    return f"{_NO_FILING_PREFIX}{assessment_year}"


def not_filed_outcome(result: dict[str, object]) -> tuple[str, str] | None:
    """Error to store when the portal has no return for this assessment year.

    Dry-run and missing-page results are left unset.
    """
    if result.get("e_verified") is not None:
        return None
    message = result.get("itr_status")
    if not isinstance(message, str) or not message.startswith(_NO_FILING_PREFIX):
        return None
    return (ITR_NOT_FILED, message)


def status_as_result(status: FiledReturnStatus) -> dict[str, object]:
    """Shape stored on job.result. Does not write filing_status."""
    if not status.found:
        return {
            "assessment_year": status.assessment_year,
            "e_verified": None,
            "itr_status": no_filing_message(status.assessment_year),
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
