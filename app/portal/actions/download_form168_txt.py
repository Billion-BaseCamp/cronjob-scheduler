"""Download Form 168 / ATS as Text for a Tax Year on TRACES."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from app.portal.form_168_txt import is_valid_form_168_txt, portal_tax_year

logger = logging.getLogger(__name__)

_PAGE_TIMEOUT_MS = 20_000
_DOWNLOAD_TIMEOUT_S = 60.0
_TAX_YEAR_LABEL = re.compile(r"Tax\s*Year", re.I)
_DOWNLOAD_TEXT = re.compile(r"Download\s+Text", re.I)
_PROCEED = re.compile(r"Proceed", re.I)
_NO_DATA = re.compile(
    r"no\s+(?:data|record|statement)|not\s+available|does\s+not\s+exist",
    re.I,
)


@dataclass
class Form168TxtResult:
    status: str  # downloaded | no_data | year_not_in_dropdown | failed
    tax_year: str
    payload: bytes | None = None
    error_code: str | None = None
    error_message: str | None = None


async def _read_download(download: Any) -> bytes | None:
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
    handle.close()
    try:
        await download.save_as(handle.name)
        return Path(handle.name).read_bytes()
    except Exception:
        logger.exception("Could not read Form 168 TXT download")
        return None
    finally:
        try:
            os.remove(handle.name)
        except OSError:
            pass


async def _flutter_click(locator) -> bool:
    """Click Flutter semantics controls (CanvasKit + transform-positioned hit targets)."""
    try:
        box = await locator.bounding_box()
    except Exception:
        box = None
    if box and box.get("width", 0) > 0 and box.get("height", 0) > 0:
        try:
            page = locator.page
            await page.mouse.click(
                box["x"] + box["width"] / 2,
                box["y"] + box["height"] / 2,
                delay=50,
            )
            return True
        except Exception:
            logger.debug("Mouse click on Flutter control failed", exc_info=True)
    try:
        await locator.click(timeout=5_000)
        return True
    except Exception:
        pass
    try:
        await locator.click(timeout=5_000, force=True)
        return True
    except Exception:
        pass
    try:
        handle = await locator.element_handle(timeout=2_000)
        if handle is None:
            return False
        await handle.evaluate("el => el.click()")
        return True
    except Exception:
        logger.exception("Flutter click failed")
        return False


async def _access_mode(page: Page) -> str | None:
    """Return the selected ATS access radio label, if semantics expose it."""
    try:
        body = await page.locator("body").inner_text(timeout=2_000)
    except Exception:
        return None
    match = re.search(
        r"(View Online|Download PDF|Download Excel|Download Text)\s+Choose how you want",
        body or "",
        re.I,
    )
    return match.group(1) if match else None


async def _download_text_radio(page: Page):
    """Flutter splits each radio into an unlabeled circle + a text label sibling."""
    label = page.locator("flt-semantics").filter(has_text=re.compile(r"^Download Text$"))
    if await label.count() == 0:
        label = page.get_by_text(re.compile(r"^Download Text$"))
    if await label.count() == 0:
        return None
    try:
        label_box = await label.first.bounding_box()
    except Exception:
        label_box = None
    if not label_box:
        return None

    radios = page.locator("[flt-tappable][role='button'], flt-semantics[role='button'][tabindex='0']")
    count = await radios.count()
    for i in range(count):
        radio = radios.nth(i)
        try:
            name = ((await radio.get_attribute("aria-label")) or "").strip()
            text = ((await radio.inner_text()) or "").strip()
            if name or text:
                continue
            box = await radio.bounding_box()
        except Exception:
            continue
        if not box:
            continue
        same_row = abs((box["y"] + box["height"] / 2) - (label_box["y"] + label_box["height"] / 2)) <= 12
        left_of_label = box["x"] < label_box["x"]
        if same_row and left_of_label:
            return radio
    return None


async def _tax_year_already_selected(page: Page, year: str) -> bool:
    # Flutter uses "2026-27, selected. …" — do not treat "not selected" as selected.
    selected_re = re.compile(rf"{re.escape(year)},\s*selected", re.I)
    try:
        body = await page.locator("body").inner_text(timeout=1_500)
    except Exception:
        body = ""
    if selected_re.search(body or ""):
        return True
    combo = page.get_by_role("button", name=selected_re)
    return await combo.count() > 0


async def _open_tax_year_combobox(page: Page) -> bool:
    """TRACES Flutter exposes Tax Year as a semantics button/combobox."""
    candidates = [
        page.get_by_role("button", name=re.compile(r"Tax\s*Year.*combo\s*box", re.I)),
        page.get_by_role("combobox", name=_TAX_YEAR_LABEL),
        page.locator("[flt-tappable]").filter(
            has_text=re.compile(r"Tax\s*Year.*combo\s*box|combo\s*box,\s*collapsed", re.I)
        ),
        page.locator("flt-semantics[role='button'][tabindex='0']").filter(
            has_text=re.compile(r"Tax\s*Year.*combo\s*box|combo\s*box,\s*collapsed", re.I)
        ),
    ]
    for loc in candidates:
        try:
            if await loc.count() == 0:
                continue
            if await _flutter_click(loc.first):
                # Wait until the list expands or a year option appears.
                option_re = re.compile(r"20\d{2}-\d{2}\s+not\s+selected", re.I)
                for _ in range(20):
                    await page.wait_for_timeout(150)
                    try:
                        body = await page.locator("body").inner_text(timeout=800)
                    except Exception:
                        body = ""
                    if option_re.search(body or "") or re.search(
                        r"combo\s*box,\s*expanded", body or "", re.I
                    ):
                        return True
                # Click registered even if expansion text lagged.
                return True
        except Exception:
            continue
    return False


async def _find_tax_year_option(page: Page, year: str):
    """Prefer the compact dropdown row, never the full-page help text that mentions TY."""
    option_re = re.compile(
        rf"^{re.escape(year)}(?:\s+not\s+selected(?:\s+\d+\s+of\s+\d+)?)?$",
        re.I,
    )
    loose_re = re.compile(rf"{re.escape(year)}\s+not\s+selected", re.I)
    candidates = [
        page.get_by_role("button", name=option_re),
        page.get_by_role("option", name=option_re),
        page.locator("[flt-tappable]").filter(has_text=loose_re),
        page.locator("flt-semantics[tabindex='0']").filter(has_text=loose_re),
        page.locator("flt-semantics").filter(has_text=loose_re),
    ]
    for loc in candidates:
        count = await loc.count()
        for i in range(count):
            item = loc.nth(i)
            try:
                box = await item.bounding_box()
            except Exception:
                continue
            if not box:
                continue
            # Dropdown rows are small; ignore giant semantics nodes / side panels.
            if box["width"] > 700 or box["height"] > 80:
                continue
            return item
    return None


async def _select_tax_year(page: Page, tax_year: str) -> str:
    """``selected``, ``missing``, or ``drift``."""
    year = portal_tax_year(tax_year)

    # Native <select>
    native = page.locator("select").filter(has_text=re.compile(r"20\d{2}", re.I))
    if await native.count():
        try:
            await native.first.select_option(label=year)
            return "selected"
        except Exception:
            try:
                await native.first.select_option(value=year)
                return "selected"
            except Exception:
                logger.warning("Native Tax Year select failed for %s", year)

    if await _tax_year_already_selected(page, year):
        return "selected"

    for attempt in range(3):
        if not await _open_tax_year_combobox(page):
            if attempt == 0:
                label = page.get_by_text(_TAX_YEAR_LABEL)
                try:
                    await label.first.wait_for(state="visible", timeout=_PAGE_TIMEOUT_MS)
                except PlaywrightTimeout:
                    return "drift"
            continue

        option = await _find_tax_year_option(page, year)
        if option is None:
            await page.wait_for_timeout(400)
            option = await _find_tax_year_option(page, year)
        if option is None:
            logger.warning("Tax Year %s not found in dropdown (attempt %s)", year, attempt + 1)
            continue

        if not await _flutter_click(option):
            logger.error("Could not click Tax Year %s", year)
            continue

        for _ in range(15):
            await page.wait_for_timeout(200)
            if await _tax_year_already_selected(page, year):
                return "selected"

    if await _tax_year_already_selected(page, year):
        return "selected"
    # Distinguish missing vs click-didn't-stick after retries.
    opened = await _open_tax_year_combobox(page)
    if opened and await _find_tax_year_option(page, year) is None:
        logger.warning("Tax Year %s not found in dropdown", year)
        return "missing"
    logger.error("Tax Year %s click did not stick on TRACES", year)
    return "drift"


async def _choose_download_text(page: Page) -> bool:
    radio = page.get_by_label(_DOWNLOAD_TEXT)
    if await radio.count():
        try:
            await radio.first.check(timeout=5_000)
            return True
        except Exception:
            try:
                await radio.first.click(timeout=5_000)
                return True
            except Exception:
                logger.exception("Could not select Download Text via label")

    flutter_radio = await _download_text_radio(page)
    if flutter_radio is not None and await _flutter_click(flutter_radio):
        # Semantics label update lags behind the real radio state.
        for _ in range(10):
            await page.wait_for_timeout(200)
            mode = await _access_mode(page)
            if mode and re.search(r"Download\s+Text", mode, re.I):
                return True
        # Proceed still downloads TXT even when the label has not refreshed yet.
        logger.info("Download Text radio clicked; semantics label not confirmed yet")
        return True

    for loc in (
        page.locator("flt-semantics[tabindex='0']").filter(has_text=_DOWNLOAD_TEXT),
        page.get_by_role("radio", name=_DOWNLOAD_TEXT),
        page.get_by_role("button", name=_DOWNLOAD_TEXT),
        page.get_by_text(_DOWNLOAD_TEXT),
    ):
        if await loc.count() == 0:
            continue
        if await _flutter_click(loc.first):
            await page.wait_for_timeout(300)
            return True
        logger.error("Could not click Download Text")
    return False


async def _click_proceed(page: Page) -> bool:
    candidates = [
        page.locator("[flt-tappable]").filter(has_text=re.compile(r"^Proceed$")),
        page.get_by_role("button", name=_PROCEED),
        page.locator("flt-semantics[tabindex='0']").filter(has_text=_PROCEED),
        page.get_by_text(_PROCEED),
    ]
    for btn in candidates:
        count = await btn.count()
        if count == 0:
            continue
        # Prefer the footer Proceed (first match can be the sticky header label).
        target = btn.nth(count - 1) if count > 1 else btn.first
        if await _flutter_click(target):
            return True
    logger.error("Could not click Proceed")
    return False


async def _body_suggests_no_data(page: Page) -> bool:
    try:
        body = await page.locator("body").inner_text(timeout=2_000)
    except Exception:
        return False
    return bool(_NO_DATA.search(body or ""))


async def download_form168_txt(page: Page, tax_year: str | None = None) -> Form168TxtResult:
    year = portal_tax_year(tax_year)

    selected = await _select_tax_year(page, year)
    if selected == "missing":
        return Form168TxtResult(
            status="year_not_in_dropdown",
            tax_year=year,
            error_code="YEAR_NOT_FOUND",
            error_message=f"Tax Year {year} is not available on TRACES.",
        )
    if selected == "drift":
        return Form168TxtResult(
            status="failed",
            tax_year=year,
            error_code="UI_DRIFT",
            error_message="Could not interact with the Tax Year control on TRACES.",
        )

    if not await _choose_download_text(page):
        return Form168TxtResult(
            status="failed",
            tax_year=year,
            error_code="UI_DRIFT",
            error_message="Could not select Download Text on TRACES.",
        )

    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()

    def _on_download(download: Any) -> None:
        if not future.done():
            loop.call_soon_threadsafe(future.set_result, download)

    page.on("download", _on_download)
    try:
        if not await _click_proceed(page):
            return Form168TxtResult(
                status="failed",
                tax_year=year,
                error_code="UI_DRIFT",
                error_message="Could not click Proceed on TRACES.",
            )

        try:
            download = await asyncio.wait_for(asyncio.shield(future), _DOWNLOAD_TIMEOUT_S)
        except asyncio.TimeoutError:
            download = None

        if download is None:
            if await _body_suggests_no_data(page):
                return Form168TxtResult(
                    status="no_data",
                    tax_year=year,
                    error_code="NO_DATA",
                    error_message=f"No Annual Tax Statement available for {year}.",
                )
            return Form168TxtResult(
                status="failed",
                tax_year=year,
                error_code="DOWNLOAD_FAILED",
                error_message="Portal did not start the Form 168 TXT download after Proceed.",
            )

        raw = await _read_download(download)
        if raw and is_valid_form_168_txt(raw):
            return Form168TxtResult(status="downloaded", tax_year=year, payload=raw)

        return Form168TxtResult(
            status="failed",
            tax_year=year,
            error_code="DOWNLOAD_FAILED",
            error_message="Downloaded file is missing or is not a valid Form 168 TXT.",
        )
    finally:
        page.remove_listener("download", _on_download)
