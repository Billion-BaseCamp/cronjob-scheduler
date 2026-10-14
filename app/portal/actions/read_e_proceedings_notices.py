"""Harvest e-Proceedings → For your Action notices (UI first, API fallback).

Per proceeding: open View Notices/Orders, keep **latest by issuedOn only**,
classify Submit Response vs View Response.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.core.config import settings
from app.portal.actions.pending_actions_hub import (
    FOR_YOUR_ACTION_TAB,
    back_to_e_proceedings_list,
    open_e_proceedings_for_your_action,
)
from app.portal.notice_diagnosis import attach_summaries

logger = logging.getLogger(__name__)

SOURCE = "e_proceedings"

PROCEEDING_CARD_SELECTOR = (
    ".card-container.matCardRow, .matCardRow, div.card-container.matCardRow"
)
VIEW_NOTICES_BUTTON = re.compile(r"View Notices\s*/\s*Orders", re.I)
NOTICE_CARD_SELECTOR = ".card-container.matCard, div.card-container.matCard"
SUBMIT_RESPONSE_RE = re.compile(r"Submit Response", re.I)
VIEW_RESPONSE_RE = re.compile(r"View Response", re.I)
DIN_RE = re.compile(r"(?:Reference ID|DIN)\s*[:\-]?\s*(\d{8,})", re.I)
# Notice u/s token: 142(1), 250, 271AAC(1), 143(1)(a), 148A, 226(3), 271(1)(c)
_SECTION_CORE = r"\d{1,3}[A-Z]{0,4}(?:\([0-9A-Za-z]+\))*"
SECTION_RE = re.compile(rf"^\s*({_SECTION_CORE})\s*$", re.I)
SECTION_IN_TEXT_RE = re.compile(
    rf"(?:u/?s\.?|under\s+section)\s*({_SECTION_CORE})",
    re.I,
)
SECTION_IN_DOC_REF_RE = re.compile(rf"/F/({_SECTION_CORE})/", re.I)
DOC_REF_RE = re.compile(
    r"(ITBA/[A-Za-z0-9_./()-]+)",
)
_DOC_REF_LABEL_RE = re.compile(r"Document\s+reference\s+ID", re.I)
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
_UI_DATE_RE = re.compile(
    r"(\d{1,2})[-/\s](Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[-/\s,]?(\d{4})",
    re.I,
)

# Pager under the For your Action list: Items per Page (10 / 25 / 50) and
# "1 of 1 pages". The tab label carries the proceeding total, e.g. "(8)".
PAGINATOR_SELECTOR = "mat-paginator#paginator, mat-paginator.mat-mdc-paginator"
PAGE_SIZE_SELECT = ".mat-mdc-paginator-page-size mat-select"
# Transparent layer over the select that opens it; a click on the select itself
# is intercepted by it ("intercepts pointer events").
PAGE_SIZE_TOUCH_TARGET = ".mat-mdc-paginator-page-size .mat-mdc-paginator-touch-target"
PAGE_SIZE_VALUE = ".mat-mdc-select-value-text"
PAGE_SIZE_OPTION = ".mat-mdc-select-panel mat-option"
# While a select is open this transparent layer covers the whole page.
OVERLAY_BACKDROP_SELECTOR = ".cdk-overlay-backdrop.cdk-overlay-backdrop-showing"
_OVERLAY_TIMEOUT_MS = 5_000
RANGE_LABEL_SELECTOR = ".mat-mdc-paginator-range-label"
_RANGE_RE = re.compile(r"(\d+)\s+of\s+(\d+)\s+pages?", re.I)
_TAB_COUNT_RE = re.compile(r"For your Action\s*\(\s*(\d+)\s*\)", re.I)

_PAGE_TIMEOUT_MS = 20_000
_CARD_TIMEOUT_MS = 12_000


@dataclass
class NoticeHarvest:
    source: str
    ok: bool
    notices: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    scrape_path: str | None = None


def parse_ui_date(text: str) -> Optional[date]:
    match = _UI_DATE_RE.search(text or "")
    if match is None:
        return None
    day = int(match.group(1))
    month = _MONTHS[match.group(2).title()[:3]]
    year = int(match.group(3))
    return date(year, month, day)


def coerce_notice_section(*candidates: Any) -> Optional[str]:
    """Best-effort Notice u/s from heading, label, description, or ITBA doc-ref."""
    for raw in candidates:
        if raw is None:
            continue
        text = str(raw).strip()
        if not text:
            continue
        compact = re.sub(r"\s+", "", text)
        direct = SECTION_RE.match(compact)
        if direct:
            return direct.group(1)
        from_us = SECTION_IN_TEXT_RE.search(text)
        if from_us:
            return re.sub(r"\s+", "", from_us.group(1))
        from_doc = SECTION_IN_DOC_REF_RE.search(text)
        if from_doc:
            return re.sub(r"\s+", "", from_doc.group(1))
    return None


def epoch_ms_to_date(value: Any) -> Optional[date]:
    if value is None or value == "":
        return None
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return parse_ui_date(str(value))
    if ms < 10_000_000_000:  # seconds
        ms *= 1000
    # Portal "Issued On" is Indian civil date.
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.fromtimestamp(ms / 1000.0, tz=ist).date()


def ay_display(ay: Any) -> Optional[str]:
    """Portal ``ay`` year start (e.g. 2025) → ``2025-26``."""
    if ay is None or ay == "":
        return None
    text = str(ay).strip()
    if re.match(r"^\d{4}-\d{2}$", text):
        return text
    try:
        start = int(text)
    except ValueError:
        return text or None
    return f"{start}-{(start + 1) % 100:02d}"


def notice_sort_key(notice: dict[str, Any]) -> tuple:
    issued = notice.get("issued_on")
    if isinstance(issued, date):
        return (issued.toordinal(), notice.get("din") or "")
    parsed = parse_ui_date(str(issued or "")) or epoch_ms_to_date(issued)
    return (parsed.toordinal() if parsed else 0, notice.get("din") or "")


def pick_latest_notice(notices: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """One notice per proceeding — highest issued_on."""
    if not notices:
        return None
    return max(notices, key=notice_sort_key)


def map_api_notice(raw: dict[str, Any], *, proceeding: dict[str, Any] | None = None) -> dict[str, Any]:
    proceeding = proceeding or {}
    issued = epoch_ms_to_date(raw.get("issuedOn"))
    due = epoch_ms_to_date(raw.get("responseDueDate"))
    is_submitted = str(raw.get("isSubmitted") or "").strip().upper()
    has_submit = is_submitted == "N"
    ay = ay_display(raw.get("ay") or proceeding.get("assessmentYear"))
    return {
        "source": SOURCE,
        "din": str(raw.get("documentIdentificationNumber") or "").strip() or None,
        "notice_section": coerce_notice_section(
            raw.get("noticeSection"),
            raw.get("description"),
            raw.get("documentReferenceId"),
        ),
        "document_reference_id": (raw.get("documentReferenceId") or "").strip() or None,
        "description": (raw.get("description") or "").strip() or None,
        "issued_on": issued.isoformat() if issued else None,
        "response_due_date": due.isoformat() if due else None,
        "assessment_year": ay,
        "proceeding_name": (
            (raw.get("proceedingName") or proceeding.get("proceedingName") or "")
            .strip()
            or None
        ),
        "proceeding_req_id": str(
            raw.get("proceedingReqId") or proceeding.get("proceedingReqId") or ""
        ).strip()
        or None,
        "has_submit_response": has_submit,
    }


def notices_from_api_payload(payload: Any) -> list[dict[str, Any]]:
    """Extract notice dicts from a portal XHR JSON body."""
    if not isinstance(payload, dict):
        return []
    candidates: list[Any] = []
    for key in (
        "eProceedingNotices",
        "notices",
        "noticeList",
        "items",
        "data",
        "responseData",
    ):
        value = payload.get(key)
        if isinstance(value, list):
            candidates = value
            break
    if not candidates:
        # Flat list-shaped wrapper sometimes nests under messages-less root.
        for value in payload.values():
            if (
                isinstance(value, list)
                and value
                and isinstance(value[0], dict)
                and "documentIdentificationNumber" in value[0]
            ):
                candidates = value
                break
    out: list[dict[str, Any]] = []
    for item in candidates:
        if isinstance(item, dict) and item.get("documentIdentificationNumber"):
            out.append(map_api_notice(item))
    return out


def parse_total_pages(text: str) -> Optional[int]:
    """``1 of 3 pages`` → 3."""
    match = _RANGE_RE.search(text or "")
    return int(match.group(2)) if match else None


def parse_for_your_action_count(text: str) -> Optional[int]:
    """``For your Action (8)`` → 8."""
    match = _TAB_COUNT_RE.search(text or "")
    return int(match.group(1)) if match else None


def proceedings_gap(
    *,
    found: int,
    expected: Optional[int],
    total_pages: Optional[int],
) -> Optional[str]:
    """Why the listed proceedings may be incomplete, or None when all are shown.

    A partial list must not pass as a completed check: notices on unread
    proceedings would look like "nothing to do".
    """
    if expected is None and total_pages is None:
        return "could not confirm how many proceedings there are"
    if total_pages is not None and total_pages > 1:
        return f"proceedings span {total_pages} pages"
    if expected is not None and found != expected:
        return f"read {found} of {expected} proceedings"
    return None


def _date_iso(d: Optional[date]) -> Optional[str]:
    return d.isoformat() if d else None


async def _inner_text(locator: Locator) -> str:
    try:
        return (await locator.inner_text()).strip()
    except PlaywrightError:
        return ""


def parse_labeled_field_from_text(card_text: str, label: str) -> Optional[str]:
    """Pull value after ``Label :`` from flat card text (unit-testable fallback)."""
    next_labels = (
        r"Description|Issued On|Response Due Date|"
        r"Last Response(?:\s+submitted\s+On)?"
    )
    pattern = re.compile(
        rf"{re.escape(label)}\s*:\s*(.+?)(?=(?:{next_labels})\s*:|\Z)",
        re.I | re.S,
    )
    match = pattern.search(card_text or "")
    if match is None:
        return None
    value = re.sub(r"\s+", " ", match.group(1)).strip(" -\t\n\r")
    return value or None


async def _labeled_subtitle(card: Locator, label: str) -> Optional[str]:
    """Read ``span.dataHeading`` + sibling ``span.subtitle2`` (portal notice cards)."""
    heading = card.locator("span.dataHeading, .dataHeading").filter(
        has_text=re.compile(rf"{re.escape(label)}\s*:", re.I)
    )
    if await heading.count() == 0:
        heading = card.locator("*").filter(
            has_text=re.compile(rf"^\s*{re.escape(label)}\s*:\s*$", re.I)
        )
    if await heading.count() == 0:
        return None
    parent = heading.first.locator("xpath=..")
    value_node = parent.locator(
        "span.subtitle2, .subtitle2, mat-label.subtitle2, .fieldVal"
    ).first
    if await value_node.count():
        value = await _inner_text(value_node)
        if value:
            return value
    # Sibling after the heading
    sibling = heading.first.locator(
        "xpath=following-sibling::span[contains(@class,'subtitle2')][1]"
    )
    if await sibling.count():
        value = await _inner_text(sibling.first)
        if value:
            return value
    return None


def parse_document_reference_id(card_text: str) -> Optional[str]:
    """Extract ITBA/... document reference from flat card text."""
    match = DOC_REF_RE.search(card_text or "")
    if match is None:
        return None
    return match.group(1).strip()


async def _document_reference_id(card: Locator, card_text: str) -> Optional[str]:
    """Value sits in ``.heading6`` above ``Document reference ID`` label."""
    label = card.locator(".dataHeading, span.dataHeading, div.dataHeading").filter(
        has_text=_DOC_REF_LABEL_RE
    )
    if await label.count():
        parent = label.first.locator("xpath=..")
        heading = parent.locator(".heading6, mat-label.heading6").first
        if await heading.count():
            value = await _inner_text(heading)
            if value and DOC_REF_RE.search(value):
                return value.strip()
        prev = label.first.locator(
            "xpath=preceding-sibling::*[contains(@class,'heading6')][1]"
        )
        if await prev.count():
            value = await _inner_text(prev.first)
            if value and DOC_REF_RE.search(value):
                return value.strip()
    return parse_document_reference_id(card_text)


async def _notice_section_from_headings(card: Locator) -> Optional[str]:
    """Notice u/s value like ``142(1)`` — not the ITBA document reference heading6."""
    sections = card.locator(".heading6, mat-label.heading6")
    count = await sections.count()
    for index in range(count):
        raw = await _inner_text(sections.nth(index))
        if not raw or DOC_REF_RE.search(raw):
            continue
        section = coerce_notice_section(raw)
        if section:
            return section
    return None


async def _parse_notice_card(card: Locator) -> Optional[dict[str, Any]]:
    text = await _inner_text(card)
    if not text:
        return None

    din = None
    din_match = DIN_RE.search(text)
    if din_match:
        din = din_match.group(1)
    else:
        heading = card.locator(".heading5, mat-label.heading5").first
        if await heading.count():
            h = await _inner_text(heading)
            digits = re.search(r"\d{8,}", h)
            if digits:
                din = digits.group(0)

    doc_ref = await _document_reference_id(card, text)

    description = await _labeled_subtitle(card, "Description")
    if not description:
        description = parse_labeled_field_from_text(text, "Description")

    notice_us_label = await _labeled_subtitle(card, "Notice u/s")
    if not notice_us_label:
        notice_us_label = parse_labeled_field_from_text(text, "Notice u/s")

    section = coerce_notice_section(
        await _notice_section_from_headings(card),
        notice_us_label,
        description,
        doc_ref,
    )

    issued = None
    due = None
    issued_raw = await _labeled_subtitle(card, "Issued On")
    due_raw = await _labeled_subtitle(card, "Response Due Date")
    if issued_raw:
        issued = parse_ui_date(issued_raw)
    if due_raw:
        due = parse_ui_date(due_raw)

    if issued is None:
        # Fallback: first date in card = issued, second = due
        dates = [parse_ui_date(m.group(0)) for m in _UI_DATE_RE.finditer(text)]
        dates = [d for d in dates if d is not None]
        if dates:
            issued = dates[0]
        if due is None and len(dates) > 1:
            due = dates[1]

    has_submit = False
    submit_btn = card.locator("button").filter(has_text=SUBMIT_RESPONSE_RE)
    view_btn = card.locator("button").filter(has_text=VIEW_RESPONSE_RE)
    if await submit_btn.count() and await submit_btn.first.is_visible():
        has_submit = True
    elif await view_btn.count():
        has_submit = False

    if not din and not section:
        return None

    return {
        "source": SOURCE,
        "din": din,
        "notice_section": section,
        "document_reference_id": doc_ref,
        "description": description,
        "issued_on": _date_iso(issued),
        "response_due_date": _date_iso(due),
        "assessment_year": None,
        "proceeding_name": None,
        "proceeding_req_id": None,
        "has_submit_response": has_submit,
    }


async def _proceeding_meta(card: Locator) -> dict[str, Any]:
    text = await _inner_text(card)
    name = None
    heading = card.locator(".heading5, .heading6, mat-label.contentHeadingText").first
    if await heading.count():
        name = await _inner_text(heading)
    ay = None
    ay_match = re.search(r"A\.?Y\.?\s*(\d{4})\s*[-–]\s*(\d{2,4})", text, re.I)
    if ay_match:
        start = int(ay_match.group(1))
        ay = f"{start}-{(start + 1) % 100:02d}"
    elif re.search(r"\b(20\d{2})\b", text):
        # assessmentYear sometimes shown as 2025 only
        year = int(re.search(r"\b(20\d{2})\b", text).group(1))
        ay = ay_display(year)
    return {"proceeding_name": name, "assessment_year": ay, "raw_text": text}


def _is_notice_api_response(response) -> bool:
    try:
        if response.request.method not in ("GET", "POST"):
            return False
        if response.request.resource_type not in ("xhr", "fetch"):
            return False
        url = response.url or ""
        if "incometax.gov.in" not in url:
            return False
        if "loginapi" in url:
            return False
        return True
    except Exception:
        return False


async def _try_parse_notice_response(response) -> list[dict[str, Any]]:
    try:
        data = await response.json()
    except Exception:
        return []
    return notices_from_api_payload(data)


async def _scrape_notices_ui(page: Page) -> list[dict[str, Any]]:
    cards = page.locator(NOTICE_CARD_SELECTOR)
    try:
        await cards.first.wait_for(state="visible", timeout=_CARD_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        # Empty list is valid
        count = await cards.count()
        if count == 0:
            return []
    count = await cards.count()
    notices: list[dict[str, Any]] = []
    for index in range(count):
        parsed = await _parse_notice_card(cards.nth(index))
        if parsed:
            notices.append(parsed)
    return notices


async def _open_view_notices(card: Locator, page: Page) -> tuple[bool, list[dict[str, Any]]]:
    """Click View Notices; return (opened, api_notices_if_any)."""
    button = card.locator("button.defaultButton.primaryButton, button").filter(
        has_text=VIEW_NOTICES_BUTTON
    )
    if await button.count() == 0:
        return False, []

    api_notices: list[dict[str, Any]] = []

    async def _on_response(response) -> None:
        if not _is_notice_api_response(response):
            return
        rows = await _try_parse_notice_response(response)
        if rows:
            api_notices.extend(rows)

    page.on("response", _on_response)
    try:
        await button.first.click(timeout=_PAGE_TIMEOUT_MS)
        # Wait for notice cards or URL change
        try:
            await page.wait_for_url(
                re.compile(r"viewNotices", re.I),
                timeout=_PAGE_TIMEOUT_MS,
            )
        except PlaywrightTimeoutError:
            pass
        await page.wait_for_timeout(800)
        return True, api_notices
    except (PlaywrightTimeoutError, PlaywrightError):
        logger.warning("View Notices/Orders click failed", exc_info=True)
        return False, api_notices
    finally:
        try:
            page.remove_listener("response", _on_response)
        except Exception:
            pass


async def _current_page_size(page: Page) -> Optional[int]:
    value = page.locator(PAGINATOR_SELECTOR).locator(PAGE_SIZE_SELECT).locator(
        PAGE_SIZE_VALUE
    )
    if await value.count() == 0:
        return None
    text = await _inner_text(value.first)
    return int(text) if text.isdigit() else None


async def _open_page_size_menu(page: Page) -> None:
    paginator = page.locator(PAGINATOR_SELECTOR)
    target = paginator.locator(PAGE_SIZE_TOUCH_TARGET)
    if await target.count():
        await target.first.click(timeout=_CARD_TIMEOUT_MS)
    else:
        await paginator.locator(PAGE_SIZE_SELECT).first.click(timeout=_CARD_TIMEOUT_MS)


async def _close_overlay(page: Page) -> None:
    """Close an open dropdown; its backdrop would block every later click."""
    backdrop = page.locator(OVERLAY_BACKDROP_SELECTOR)
    try:
        if await backdrop.count() == 0:
            return
        await page.keyboard.press("Escape")
        await backdrop.first.wait_for(state="hidden", timeout=_OVERLAY_TIMEOUT_MS)
    except (PlaywrightTimeoutError, PlaywrightError):
        logger.warning("Dropdown backdrop is still open", exc_info=True)


async def _show_largest_page(page: Page) -> Optional[int]:
    """Pick the largest Items per Page option; return it, or None if unavailable."""
    select = page.locator(PAGINATOR_SELECTOR).locator(PAGE_SIZE_SELECT)
    try:
        if await select.count() == 0:
            return None
        await _open_page_size_menu(page)
        options = page.locator(PAGE_SIZE_OPTION)
        await options.first.wait_for(state="visible", timeout=_CARD_TIMEOUT_MS)
        sizes: list[tuple[int, int]] = []
        for index in range(await options.count()):
            text = await _inner_text(options.nth(index))
            if text.isdigit():
                sizes.append((int(text), index))
        if not sizes:
            return None
        size, index = max(sizes)
        # The panel is position:fixed and can open below the window, out of
        # reach of a mouse click.
        await options.nth(index).evaluate("el => el.click()")
        await page.locator(OVERLAY_BACKDROP_SELECTOR).first.wait_for(
            state="hidden", timeout=_OVERLAY_TIMEOUT_MS
        )
        await page.wait_for_timeout(800)
        if await _current_page_size(page) != size:
            logger.warning("e-Proceedings page size did not change to %s", size)
            return None
        return size
    except (PlaywrightTimeoutError, PlaywrightError):
        logger.warning("Could not change e-Proceedings page size", exc_info=True)
        return None
    finally:
        await _close_overlay(page)


async def _keep_page_size(page: Page, size: Optional[int]) -> None:
    """The list may reset to its default size after Back; restore it."""
    if size is None or await _current_page_size(page) == size:
        return
    await _show_largest_page(page)


async def _expected_proceedings(page: Page) -> Optional[int]:
    tab = page.get_by_role("tab", name=FOR_YOUR_ACTION_TAB)
    if await tab.count() == 0:
        tab = page.locator("[role='tab']").filter(has_text=FOR_YOUR_ACTION_TAB)
    if await tab.count() == 0:
        return None
    return parse_for_your_action_count(await _inner_text(tab.first))


async def _total_pages(page: Page) -> Optional[int]:
    label = page.locator(PAGINATOR_SELECTOR).locator(RANGE_LABEL_SELECTOR)
    if await label.count() == 0:
        return None
    return parse_total_pages(await _inner_text(label.first))


async def _harvest_proceeding(
    card: Locator, page: Page
) -> tuple[bool, Optional[dict[str, Any]]]:
    """Return (opened, latest notice). ``opened`` False means the card was not read."""
    meta = await _proceeding_meta(card)
    opened, api_notices = await _open_view_notices(card, page)
    if not opened:
        return False, None

    ui_notices = await _scrape_notices_ui(page)
    notices = ui_notices
    if not notices and api_notices:
        notices = api_notices
        logger.info(
            "UI notice parse empty; using API fallback (%s notices)",
            len(api_notices),
        )
    elif not notices:
        logger.info("No notices for proceeding %s", meta.get("proceeding_name"))

    latest = pick_latest_notice(notices)
    await back_to_e_proceedings_list(page)

    if latest is None:
        return True, None

    if not latest.get("proceeding_name"):
        latest["proceeding_name"] = meta.get("proceeding_name")
    if not latest.get("assessment_year"):
        latest["assessment_year"] = meta.get("assessment_year")
    latest.pop("scrape_path", None)
    return True, latest


async def read_e_proceedings_notices(page: Page | None) -> NoticeHarvest:
    if settings.PORTAL_AUTOMATION_DRY_RUN:
        return NoticeHarvest(
            source=SOURCE,
            ok=True,
            notices=[],
            scrape_path="dry-run",
        )
    if page is None:
        return NoticeHarvest(
            source=SOURCE,
            ok=False,
            notices=[],
            error="no page",
        )

    opened = await open_e_proceedings_for_your_action(page)
    if not opened:
        return NoticeHarvest(
            source=SOURCE,
            ok=False,
            notices=[],
            error="Could not open e-Proceedings → For your Action",
        )

    def _incomplete(reason: str) -> NoticeHarvest:
        logger.warning("e-Proceedings list incomplete: %s", reason)
        return NoticeHarvest(
            source=SOURCE,
            ok=False,
            notices=[],
            error=f"Incomplete e-Proceedings list: {reason}",
            scrape_path="ui",
        )

    cards = page.locator(PROCEEDING_CARD_SELECTOR)
    try:
        await cards.first.wait_for(state="visible", timeout=_CARD_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        if await cards.count() == 0:
            expected = await _expected_proceedings(page)
            if expected:
                return _incomplete(f"tab shows {expected} but no proceedings loaded")
            logger.info("No e-proceeding cards on For your Action")
            return NoticeHarvest(source=SOURCE, ok=True, notices=[], scrape_path="ui")

    page_size = await _show_largest_page(page)
    expected = await _expected_proceedings(page)
    total_pages = await _total_pages(page)
    count = await cards.count()
    logger.info(
        "Found %s e-proceeding card(s); tab count=%s pages=%s page_size=%s",
        count,
        expected,
        total_pages,
        page_size,
    )
    gap = proceedings_gap(found=count, expected=expected, total_pages=total_pages)
    if gap:
        return _incomplete(gap)

    harvested: list[dict[str, Any]] = []
    # Re-query each iteration — DOM rebuilds after Back.
    for index in range(count):
        unread = f"proceeding {index + 1} of {count} could not be read"
        if index:
            await _keep_page_size(page, page_size)
        cards = page.locator(PROCEEDING_CARD_SELECTOR)
        try:
            await cards.nth(index).wait_for(state="visible", timeout=_CARD_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            return _incomplete(f"{unread} (card missing after Back)")
        try:
            opened, notice = await _harvest_proceeding(cards.nth(index), page)
        except Exception:
            logger.exception("Failed harvesting proceeding card %s", index)
            return _incomplete(unread)
        if not opened:
            return _incomplete(f"{unread} (View Notices/Orders did not open)")
        if notice:
            harvested.append(notice)

    enriched = attach_summaries(harvested)
    return NoticeHarvest(
        source=SOURCE,
        ok=True,
        notices=enriched,
        scrape_path="ui",
    )
