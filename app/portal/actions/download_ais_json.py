"""Download AIS JSON (for AIS Utility) from ais.insight.gov.in with Gemini captcha solving.

Flow on the AIS compliance portal:
  1. On AIS Instructions (``/complianceportal/ais/instructions``):
     - If ``Download AIS/TIS (F.Y. <fy>)`` is visible for the requested FY, click it.
     - Otherwise open the ``AIS`` sub-tab, pick ``F.Y. <fy>`` from ``fy-dropdown``,
       and click its ``Download`` button.
  2. In the ``Download`` modal, click ``Download`` on the row containing
     ``Annual Information Statement (AIS - Form 168) - JSON (for AIS Utility)``.
  3. In the confirmation + CAPTCHA modal, capture the captcha ``<img>``, solve it
     via Gemini, fill the input, arm Playwright's download listener, and click
     ``Proceed`` (retrying with the refresh icon on failure).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from playwright.async_api import Locator, Page, TimeoutError as PlaywrightTimeout

from app.core.config import settings
from app.portal.ais_json import is_valid_ais_download
from app.portal.captcha_solver import CaptchaSolveError, solve_captcha_with_gemini

logger = logging.getLogger(__name__)

_PAGE_TIMEOUT_MS = 20_000
_DIALOG_TIMEOUT_MS = 20_000
_DOWNLOAD_TIMEOUT_S = 30.0
_CAPTCHA_ERROR_GRACE_S = 3.0
_RETRY_SETTLE_MS = 1_000

_INSTRUCTIONS_DOWNLOAD_BTN_RE = re.compile(r"Download\s+AIS\s*/\s*TIS", re.I)
_AIS_SUBTAB_RE = re.compile(r"^\s*AIS\s*$")
_AIS_JSON_ROW_RE = re.compile(r"JSON\s*\(\s*for\s+AIS\s+Utility\s*\)", re.I)
_DOWNLOAD_BTN_RE = re.compile(r"^\s*Download\s*$", re.I)
_PROCEED_BTN_RE = re.compile(r"^\s*Proceed\s*$", re.I)
_CANCEL_BTN_RE = re.compile(r"^\s*Cancel\s*$", re.I)
_CAPTCHA_MODAL_TEXT_RE = re.compile(
    r"Please\s+download\s+JSON\s+file\s+only|AIS\s+data\s+on\s+the\s+utility", re.I
)
_NO_DATA_RE = re.compile(r"No\s+data\s+available\s+for\s+the\s+selected", re.I)
_INVALID_CAPTCHA_RE = re.compile(
    r"invalid\s+captcha|incorrect\s+captcha|captcha\s+does\s+not\s+match|enter\s+valid\s+captcha",
    re.I,
)

CaptchaSolver = Callable[[bytes], Awaitable[str]]


@dataclass(frozen=True)
class AisJsonDownloadResult:
    status: str  # downloaded | no_data | year_not_in_dropdown | page_drift | failed
    financial_year: str
    payload: bytes | None = None
    error_code: str | None = None
    error_message: str | None = None
    captcha_attempts: int = 0


def _fy_regex(financial_year: str) -> re.Pattern[str]:
    return re.compile(rf"(?:F\.?Y\.?\s*)?{re.escape(financial_year)}\b", re.I)


async def _visible(locator: Locator, timeout: int = 1_500) -> bool:
    try:
        return await locator.first.is_visible(timeout=timeout)
    except PlaywrightTimeout:
        return False


async def _select_fy_on_ais_tab(page: Page, financial_year: str) -> str:
    """Switch to the ``AIS`` sub-tab and pick ``F.Y. <financial_year>``.

    Returns ``selected``, ``missing``, or ``drift``.
    """
    ais_tab = page.locator("a, button, [role='tab'], li").filter(
        has_text=_AIS_SUBTAB_RE
    )
    if await ais_tab.count() == 0:
        return "drift"
    try:
        await ais_tab.first.click(timeout=_PAGE_TIMEOUT_MS)
    except PlaywrightTimeout:
        return "drift"

    dropdown = page.locator("app-dropdown-btn.fy-dropdown")
    toggle = dropdown.locator("button.dropdown-toggle")
    try:
        await toggle.first.wait_for(state="visible", timeout=_PAGE_TIMEOUT_MS)
        current_text = (await dropdown.first.inner_text()).strip()
        if not _fy_regex(financial_year).search(current_text):
            await toggle.first.click()
            await dropdown.locator("div.dropdown-menu").first.wait_for(
                state="visible", timeout=_PAGE_TIMEOUT_MS
            )
            item = dropdown.locator("button.dropdown-item").filter(
                has_text=_fy_regex(financial_year)
            )
            if await item.count() == 0:
                await page.keyboard.press("Escape")
                return "missing"
            await item.first.click()
            await dropdown.locator("span.value-text").filter(
                has_text=_fy_regex(financial_year)
            ).first.wait_for(state="visible", timeout=_PAGE_TIMEOUT_MS)
    except PlaywrightTimeout:
        logger.warning("Could not select F.Y. %s on AIS tab", financial_year)
        return "drift"
    return "selected"


async def _open_download_modal(page: Page, financial_year: str) -> str:
    """Open the AIS/TIS Download modal for ``financial_year``.

    Returns ``opened``, ``missing``, or ``drift``.
    """
    instructions_btn = page.locator("button, a").filter(
        has_text=_INSTRUCTIONS_DOWNLOAD_BTN_RE
    )
    if await _visible(instructions_btn, timeout=3_000):
        btn_text = " ".join((await instructions_btn.first.inner_text()).split())
        if _fy_regex(financial_year).search(btn_text):
            logger.info("Clicking Instructions download button (%s)", btn_text)
            await instructions_btn.first.click()
            return "opened"

    selected = await _select_fy_on_ais_tab(page, financial_year)
    if selected != "selected":
        return selected

    ais_download_btn = page.locator("button, a").filter(
        has_text=re.compile(r"Download", re.I)
    )
    if await _visible(ais_download_btn, timeout=10_000):
        await ais_download_btn.first.click()
        return "opened"
    return "drift"


async def _click_json_download_row(page: Page) -> bool:
    """In the Download modal, click Download on the AIS JSON (for AIS Utility) row."""
    dialog = page.locator("mat-dialog-container:visible").filter(
        has_text=_AIS_JSON_ROW_RE
    ).last
    try:
        await dialog.wait_for(state="visible", timeout=_DIALOG_TIMEOUT_MS)
    except PlaywrightTimeout:
        return False

    row = dialog.locator("div, tr, li").filter(has_text=_AIS_JSON_ROW_RE).filter(
        has=page.locator("button").filter(has_text=_DOWNLOAD_BTN_RE)
    )
    if await row.count() > 0:
        btn = row.last.locator("button").filter(has_text=_DOWNLOAD_BTN_RE)
        if await btn.count() > 0:
            logger.info("Clicking Download on AIS JSON (for AIS Utility) row")
            await btn.first.click()
            return True
    return False


async def _find_captcha_dialog(page: Page) -> Locator | None:
    dialogs = page.locator("mat-dialog-container:visible")
    count = await dialogs.count()
    for idx in range(count - 1, -1, -1):
        dlg = dialogs.nth(idx)
        try:
            text = await dlg.inner_text()
        except PlaywrightTimeout:
            continue
        if _CAPTCHA_MODAL_TEXT_RE.search(text) or await dlg.locator("input").count() > 0:
            return dlg
    return None


async def _find_captcha_image(dialog: Locator) -> Locator | None:
    imgs = dialog.locator(
        "img[src^='data:image'], img[alt*='captcha' i], img[id*='captcha' i], img"
    )
    count = await imgs.count()
    best: Locator | None = None
    best_area = 0.0
    for idx in range(count):
        candidate = imgs.nth(idx)
        try:
            if not await candidate.is_visible():
                continue
            box = await candidate.bounding_box()
        except PlaywrightTimeout:
            continue
        if not box:
            continue
        area = float(box["width"]) * float(box["height"])
        # Captcha images are wide banners; skip small info/refresh/audio icons.
        if box["width"] >= 80 and box["height"] >= 25 and area > best_area:
            best = candidate
            best_area = area
    return best


async def _refresh_captcha(dialog: Locator, previous_src: str | None) -> None:
    refresh = dialog.locator(
        "button[aria-label*='refresh' i], a[aria-label*='refresh' i], "
        "button[title*='refresh' i], a[title*='refresh' i], "
        "i[class*='refresh' i], span[class*='refresh' i], "
        "img[src*='refresh' i], img[alt*='refresh' i]"
    )
    if await refresh.count() > 0:
        try:
            await refresh.first.click(timeout=5_000)
        except PlaywrightTimeout:
            return
    else:
        # In the modal, the refresh icon sits right next to the audio button near the input.
        icons = dialog.locator("button:has(svg), button:has(i), a:has(i), a:has(svg)")
        if await icons.count() >= 2:
            try:
                await icons.last.click(timeout=5_000)
            except PlaywrightTimeout:
                return

    await dialog.page.wait_for_timeout(_RETRY_SETTLE_MS)
    if previous_src:
        img = await _find_captcha_image(dialog)
        if img is not None:
            for _ in range(10):
                src = await img.get_attribute("src")
                if src and src != previous_src:
                    break
                await dialog.page.wait_for_timeout(200)


async def _read_download_bytes(download: Any) -> bytes | None:
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
    handle.close()
    try:
        await download.save_as(handle.name)
        return Path(handle.name).read_bytes()
    except Exception:
        logger.exception("Could not read downloaded AIS JSON file")
        return None
    finally:
        try:
            os.remove(handle.name)
        except OSError:
            pass


async def _wait_download_or_reject(
    page: Page,
    future: asyncio.Future,
    captcha_dialog: Locator,
) -> tuple[Any | None, str]:
    """Wait for a file download, or bail out early if an error/no-data message appears."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _DOWNLOAD_TIMEOUT_S
    error_seen_at: float | None = None

    while loop.time() < deadline:
        if future.done():
            return future.result(), ""
        try:
            visible_dialogs = page.locator("mat-dialog-container:visible")
            dlg_text = (
                await visible_dialogs.last.inner_text()
                if await visible_dialogs.count() > 0
                else ""
            )
        except PlaywrightTimeout:
            dlg_text = ""

        if _NO_DATA_RE.search(dlg_text):
            return None, dlg_text
        if _INVALID_CAPTCHA_RE.search(dlg_text):
            if error_seen_at is None:
                error_seen_at = loop.time()
            elif loop.time() - error_seen_at >= 0.4:
                return None, dlg_text
        elif error_seen_at is None and await captcha_dialog.is_visible():
            # Keep polling in case the server is still streaming the file.
            pass
        await page.wait_for_timeout(200)

    if future.done():
        return future.result(), ""
    return None, ""


async def _close_cancel_best_effort(dialog: Locator | None) -> None:
    if dialog is None:
        return
    cancel = dialog.locator("button").filter(has_text=_CANCEL_BTN_RE)
    if await cancel.count() > 0:
        try:
            await cancel.first.click(timeout=3_000)
        except PlaywrightTimeout:
            pass


async def download_ais_json(
    page: Page,
    financial_year: str,
    *,
    captcha_solver: CaptchaSolver = solve_captcha_with_gemini,
    max_captcha_attempts: int | None = None,
) -> AisJsonDownloadResult:
    """Download AIS Utility JSON for ``financial_year`` from the open AIS portal."""
    opened = await _open_download_modal(page, financial_year)
    if opened == "missing":
        return AisJsonDownloadResult(
            status="year_not_in_dropdown",
            financial_year=financial_year,
            error_code="YEAR_NOT_FOUND",
            error_message=f"F.Y. {financial_year} is not available in AIS.",
        )
    if opened != "opened":
        return AisJsonDownloadResult(
            status="page_drift",
            financial_year=financial_year,
            error_code="UI_DRIFT",
            error_message=f"Could not open the AIS/TIS download dialog for F.Y. {financial_year}.",
        )

    if not await _click_json_download_row(page):
        return AisJsonDownloadResult(
            status="page_drift",
            financial_year=financial_year,
            error_code="UI_DRIFT",
            error_message="Could not find the AIS JSON (for AIS Utility) Download button.",
        )

    await page.wait_for_timeout(400)
    visible_dialogs = page.locator("mat-dialog-container:visible")
    top_text = ""
    if await visible_dialogs.count() > 0:
        try:
            top_text = await visible_dialogs.last.inner_text()
        except PlaywrightTimeout:
            top_text = ""
    if _NO_DATA_RE.search(top_text):
        return AisJsonDownloadResult(
            status="no_data",
            financial_year=financial_year,
        )

    captcha_dialog = await _find_captcha_dialog(page)
    if captcha_dialog is None:
        return AisJsonDownloadResult(
            status="page_drift",
            financial_year=financial_year,
            error_code="UI_DRIFT",
            error_message="AIS JSON captcha confirmation dialog did not appear.",
        )

    attempts_limit = max(
        1,
        max_captcha_attempts
        if max_captcha_attempts is not None
        else settings.AIS_CAPTCHA_MAX_ATTEMPTS,
    )
    attempts_used = 0
    last_error = "Captcha could not be solved."

    for attempt in range(1, attempts_limit + 1):
        attempts_used = attempt
        captcha_img = await _find_captcha_image(captcha_dialog)
        if captcha_img is None:
            return AisJsonDownloadResult(
                status="page_drift",
                financial_year=financial_year,
                error_code="UI_DRIFT",
                error_message="Captcha image was not visible in the AIS JSON dialog.",
                captcha_attempts=attempts_used,
            )

        previous_src = await captcha_img.get_attribute("src")
        png_bytes = await captcha_img.screenshot(type="png")
        try:
            solution = await captcha_solver(png_bytes)
        except CaptchaSolveError as exc:
            last_error = exc.message
            logger.warning(
                "AIS captcha solve attempt %s/%s failed: %s",
                attempt,
                attempts_limit,
                exc.message,
            )
            if attempt < attempts_limit:
                await _refresh_captcha(captcha_dialog, previous_src)
            continue

        text_input = captcha_dialog.locator("input[type='text'], input:not([type='hidden'])")
        if await text_input.count() == 0:
            return AisJsonDownloadResult(
                status="page_drift",
                financial_year=financial_year,
                error_code="UI_DRIFT",
                error_message="Captcha text input was not found in the AIS JSON dialog.",
                captcha_attempts=attempts_used,
            )

        await text_input.first.fill("")
        await text_input.first.fill(solution)

        proceed_btn = captcha_dialog.locator("button").filter(has_text=_PROCEED_BTN_RE)
        if await proceed_btn.count() == 0:
            return AisJsonDownloadResult(
                status="page_drift",
                financial_year=financial_year,
                error_code="UI_DRIFT",
                error_message="Proceed button was not found in the AIS JSON dialog.",
                captcha_attempts=attempts_used,
            )

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()

        def _on_download(download: Any) -> None:
            if future.done():
                return
            loop.call_soon_threadsafe(future.set_result, download)

        page.on("download", _on_download)
        try:
            logger.info(
                "Submitting AIS JSON captcha attempt %s/%s for F.Y. %s",
                attempt,
                attempts_limit,
                financial_year,
            )
            await proceed_btn.first.click()
            download, dlg_text = await _wait_download_or_reject(
                page, future, captcha_dialog
            )
        finally:
            page.remove_listener("download", _on_download)

        if download is not None:
            raw = await _read_download_bytes(download)
            if raw and is_valid_ais_download(raw):
                return AisJsonDownloadResult(
                    status="downloaded",
                    financial_year=financial_year,
                    payload=raw,
                    captcha_attempts=attempts_used,
                )
            return AisJsonDownloadResult(
                status="failed",
                financial_year=financial_year,
                error_code="DOWNLOAD_FAILED",
                error_message="Portal download did not contain valid AIS JSON data.",
                captcha_attempts=attempts_used,
            )

        if not dlg_text:
            try:
                visible_dialogs = page.locator("mat-dialog-container:visible")
                dlg_text = (
                    await visible_dialogs.last.inner_text()
                    if await visible_dialogs.count() > 0
                    else ""
                )
            except PlaywrightTimeout:
                dlg_text = ""
        if _NO_DATA_RE.search(dlg_text):
            return AisJsonDownloadResult(
                status="no_data",
                financial_year=financial_year,
                captcha_attempts=attempts_used,
            )
        if _INVALID_CAPTCHA_RE.search(dlg_text):
            last_error = "Portal rejected the captcha answer."
        else:
            last_error = "Portal did not start the AIS JSON download after Proceed."

        logger.warning(
            "AIS captcha attempt %s/%s did not yield a download: %s",
            attempt,
            attempts_limit,
            last_error,
        )
        if attempt < attempts_limit:
            still_open = await _find_captcha_dialog(page)
            if still_open is None:
                break
            captcha_dialog = still_open
            await _refresh_captcha(captcha_dialog, previous_src)

    await _close_cancel_best_effort(captcha_dialog)
    return AisJsonDownloadResult(
        status="failed",
        financial_year=financial_year,
        error_code="CAPTCHA_FAILED",
        error_message=f"{last_error} ({attempts_used} attempt(s)).",
        captcha_attempts=attempts_used,
    )
