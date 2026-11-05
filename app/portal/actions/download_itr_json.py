"""Download one ITR JSON from View Filed Returns.

The assessment-year filter and card list match the status scrape. Download
JSON is the button on the list card, next to View Details. Rectification and
Revised cards that lack the button fall through to the next older card.
The chosen file is returned as bytes; the job stores it in S3.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Optional

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.portal.actions.read_filed_return_status import (
    CARD_SELECTOR,
    ITR_NOT_FILED,
    LIFECYCLE_SELECTOR,
    STEP_SELECTOR,
    VIEW_DETAILS_SELECTOR,
    _apply_ay_filter,
    _cards_for_ay,
    no_filing_message,
    parse_filing_date,
    portal_assessment_year,
)
from app.portal.itr_json import (
    JSON_NOT_AVAILABLE,
    FiledReturnCard,
    choose_itr_json_card,
    is_itr_json,
    parse_acknowledgement_no,
    parse_e_verified_on,
    parse_filing_section,
    parse_filing_type,
    parse_itr_form,
)

logger = logging.getLogger(__name__)

_DOWNLOAD_JSON_RE = re.compile(r"Download JSON")
_DOWNLOAD_CONTROL = "button, a, span.hyperLink"
_PAGINATOR = "app-itr-status mat-paginator"
_NEXT = "button.mat-mdc-paginator-navigation-next"
_FIRST = "button.mat-mdc-paginator-navigation-first"
_RANGE = ".mat-mdc-paginator-range-label"
_CARD_TIMEOUT_MS = 15_000
_DETAILS_TIMEOUT_MS = 20_000
_DOWNLOAD_TIMEOUT_S = 45.0
_MAX_PAGES = 20


@dataclass(frozen=True)
class ItrJsonScrape:
    assessment_year: str
    cards: tuple[FiledReturnCard, ...]
    chosen: Optional[FiledReturnCard]
    payload: Optional[bytes]
    error_code: Optional[str] = None
    error_message: Optional[str] = None


def _empty(
    ay: str,
    *,
    error_code: str,
    error_message: str,
    cards: tuple[FiledReturnCard, ...] = (),
) -> ItrJsonScrape:
    return ItrJsonScrape(
        assessment_year=ay,
        cards=cards,
        chosen=None,
        payload=None,
        error_code=error_code,
        error_message=error_message,
    )


def _paginator(page: Page) -> Locator:
    return page.locator(_PAGINATOR)


async def _is_disabled(button: Locator) -> bool:
    if await button.count() == 0:
        return True
    if not await button.first.is_visible():
        return True
    if await button.first.get_attribute("disabled") is not None:
        return True
    return await button.first.get_attribute("aria-disabled") == "true"


async def _range_text(page: Page) -> str:
    label = _paginator(page).locator(_RANGE)
    if await label.count() == 0:
        return ""
    return " ".join((await label.first.inner_text()).split())


async def _wait_range_change(page: Page, before: str) -> bool:
    if not before:
        await page.wait_for_timeout(800)
        return True
    for _ in range(20):
        await page.wait_for_timeout(250)
        if await _range_text(page) != before:
            return True
    return False


async def _download_button(card: Locator) -> Optional[Locator]:
    buttons = card.locator(_DOWNLOAD_CONTROL).filter(has_text=_DOWNLOAD_JSON_RE)
    count = await buttons.count()
    for index in range(count):
        button = buttons.nth(index)
        if await button.is_visible() and await button.is_enabled():
            return button
    return None


async def _snapshot_page(
    page: Page, assessment_year: str, page_index: int
) -> list[FiledReturnCard]:
    cards = _cards_for_ay(page, assessment_year)
    count = await cards.count()
    snapshots: list[FiledReturnCard] = []
    for index in range(count):
        card = cards.nth(index)
        text = await card.inner_text()
        button = await _download_button(card)
        snapshots.append(
            FiledReturnCard(
                filing_type=parse_filing_type(text),
                filing_date=parse_filing_date(text),
                has_download_json=button is not None,
                page_index=page_index,
                card_index=index,
                itr_form=parse_itr_form(text),
                acknowledgement_no=parse_acknowledgement_no(text),
                filing_section=parse_filing_section(text),
                e_verified_on=parse_e_verified_on(text),
            )
        )
    return snapshots


async def _collect_cards(
    page: Page, assessment_year: str
) -> tuple[list[FiledReturnCard], int]:
    collected: list[FiledReturnCard] = []
    page_index = 0
    seen_ranges: set[str] = set()
    while page_index < _MAX_PAGES:
        collected.extend(await _snapshot_page(page, assessment_year, page_index))
        nxt = _paginator(page).locator(_NEXT)
        if await _is_disabled(nxt):
            return collected, page_index
        before = await _range_text(page)
        if before and before in seen_ranges:
            return collected, page_index
        if before:
            seen_ranges.add(before)
        await nxt.first.click()
        if not await _wait_range_change(page, before):
            logger.warning("Filed-returns page did not advance")
            return collected, page_index
        page_index += 1
    logger.warning("Stopped after %s filed-return pages", _MAX_PAGES)
    return collected, page_index


async def _open_results_page(page: Page, target: int, current: int) -> bool:
    if target == current:
        return True
    first = _paginator(page).locator(_FIRST)
    if await _is_disabled(first):
        return False
    before = await _range_text(page)
    await first.first.click()
    if not await _wait_range_change(page, before):
        return False
    current = 0
    while current < target:
        nxt = _paginator(page).locator(_NEXT)
        if await _is_disabled(nxt):
            return False
        before = await _range_text(page)
        await nxt.first.click()
        if not await _wait_range_change(page, before):
            return False
        current += 1
    return True


async def _read_download(download: Any) -> Optional[bytes]:
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
    handle.close()
    try:
        await download.save_as(handle.name)
        return Path(handle.name).read_bytes()
    except Exception:
        logger.exception("Could not read the ITR JSON download")
        return None
    finally:
        try:
            os.remove(handle.name)
        except OSError:
            pass


async def _back_to_list(page: Page) -> None:
    back = page.get_by_role("button", name=re.compile(r"^\s*Back\s*$"))
    await back.first.click()
    await page.locator("app-itr-status").wait_for(
        state="visible",
        timeout=_DETAILS_TIMEOUT_MS,
    )


async def _e_verified_from_details(
    page: Page, card: FiledReturnCard, ay: str
) -> str:
    """Open View Details when the list card does not show Successfully e-verified."""
    if card.e_verified_on:
        return card.e_verified_on
    cards = _cards_for_ay(page, ay)
    if card.card_index >= await cards.count():
        return ""
    link = cards.nth(card.card_index).locator(VIEW_DETAILS_SELECTOR).filter(
        has_text="View Details"
    )
    found = ""
    try:
        await link.first.click()
        lifecycle = page.locator(LIFECYCLE_SELECTOR)
        await lifecycle.wait_for(state="visible", timeout=_DETAILS_TIMEOUT_MS)
        await lifecycle.locator(STEP_SELECTOR).first.wait_for(
            state="visible",
            timeout=_CARD_TIMEOUT_MS,
        )
        found = parse_e_verified_on(await lifecycle.inner_text())
    except (PlaywrightTimeoutError, PlaywrightError):
        logger.warning("View Details did not show the e-verified step", exc_info=True)
    try:
        if await page.locator(LIFECYCLE_SELECTOR).count():
            await _back_to_list(page)
    except (PlaywrightTimeoutError, PlaywrightError):
        logger.warning("Could not return from View Details", exc_info=True)
    return found


async def _click_download(page: Page, card: FiledReturnCard, ay: str) -> Optional[bytes]:
    cards = _cards_for_ay(page, ay)
    if card.card_index >= await cards.count():
        logger.warning("Card %s is gone on page %s", card.card_index, card.page_index)
        return None
    button = await _download_button(cards.nth(card.card_index))
    if button is None:
        logger.warning(
            "Download JSON disappeared on %s card %s",
            card.filing_type,
            card.card_index,
        )
        return None

    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()

    def _on_download(download: Any) -> None:
        if future.done():
            return
        loop.call_soon_threadsafe(future.set_result, download)

    page.on("download", _on_download)
    try:
        logger.info(
            "Download JSON filing_type=%s date=%s page=%s card=%s",
            card.filing_type,
            card.filing_date,
            card.page_index,
            card.card_index,
        )
        await button.click()
        try:
            download = await asyncio.wait_for(asyncio.shield(future), _DOWNLOAD_TIMEOUT_S)
        except asyncio.TimeoutError:
            return None
        return await _read_download(download)
    finally:
        page.remove_listener("download", _on_download)


async def download_itr_json(page: Page, assessment_year: str) -> ItrJsonScrape:
    ay = portal_assessment_year(assessment_year)
    try:
        await page.locator(CARD_SELECTOR).first.wait_for(
            state="visible",
            timeout=_CARD_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError:
        logger.warning("No filed-return cards on the page")
        return _empty(
            ay,
            error_code=ITR_NOT_FILED,
            error_message=no_filing_message(ay),
        )

    if not await _apply_ay_filter(page, ay):
        return _empty(
            ay,
            error_code="UI_DRIFT",
            error_message="Assessment year filter did not apply on View Filed Returns.",
        )

    try:
        await _cards_for_ay(page, ay).first.wait_for(
            state="visible",
            timeout=_CARD_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError:
        logger.info("No card for A.Y. %s after Filter", ay)
        return _empty(
            ay,
            error_code=ITR_NOT_FILED,
            error_message=no_filing_message(ay),
        )

    try:
        cards, current_page = await _collect_cards(page, ay)
    except (PlaywrightTimeoutError, PlaywrightError):
        logger.warning("Could not read filed-return cards", exc_info=True)
        return _empty(
            ay,
            error_code="UI_DRIFT",
            error_message="Filed-return cards could not be read.",
        )

    chosen = choose_itr_json_card(cards)
    found = tuple(cards)
    if chosen is None:
        return _empty(
            ay,
            error_code=JSON_NOT_AVAILABLE,
            error_message=f"No Download JSON could be saved for AY {ay}.",
            cards=found,
        )

    try:
        opened = await _open_results_page(page, chosen.page_index, current_page)
    except (PlaywrightTimeoutError, PlaywrightError):
        logger.warning("Could not return to the chosen filed-return page", exc_info=True)
        opened = False
    if not opened:
        return ItrJsonScrape(
            assessment_year=ay,
            cards=found,
            chosen=chosen,
            payload=None,
            error_code="UI_DRIFT",
            error_message="Could not return to the filing that has Download JSON.",
        )

    if not chosen.e_verified_on:
        verified = await _e_verified_from_details(page, chosen, ay)
        if verified:
            chosen = replace(chosen, e_verified_on=verified)
        if chosen.page_index:
            try:
                opened = await _open_results_page(page, chosen.page_index, 0)
            except (PlaywrightTimeoutError, PlaywrightError):
                opened = False
            if not opened:
                return ItrJsonScrape(
                    assessment_year=ay,
                    cards=found,
                    chosen=chosen,
                    payload=None,
                    error_code="UI_DRIFT",
                    error_message="Could not return to the filing that has Download JSON.",
                )

    try:
        payload = await _click_download(page, chosen, ay)
    except (PlaywrightTimeoutError, PlaywrightError):
        logger.warning("Download JSON click failed", exc_info=True)
        payload = None
    if not payload:
        return ItrJsonScrape(
            assessment_year=ay,
            cards=found,
            chosen=chosen,
            payload=None,
            error_code="DOWNLOAD_FAILED",
            error_message=f"Download JSON did not arrive for AY {ay}.",
        )
    if not is_itr_json(payload):
        return ItrJsonScrape(
            assessment_year=ay,
            cards=found,
            chosen=chosen,
            payload=None,
            error_code="DOWNLOAD_FAILED",
            error_message=f"Download JSON for AY {ay} was not a JSON file.",
        )
    return ItrJsonScrape(
        assessment_year=ay,
        cards=found,
        chosen=chosen,
        payload=payload,
    )
