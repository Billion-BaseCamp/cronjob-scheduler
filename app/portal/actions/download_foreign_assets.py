"""Download one Foreign Assets Information PDF for a calendar year.

The year control is ``app-foreign-asset-report app-dropdown-btn.fy-dropdown``
(C.Y., not the Payment of Taxes F.Y. list). Download PDF is
``app-foreign-asset-report button.download-btn``.

Both results are ``app-logout-alert`` inside ``#dynamicCommonPopups``.
"No data available for the selected year" means there is no file.
"The downloaded PDF will be password protected" means a file is in flight.
Ok is ``button.btn-primary`` on that dialog.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from app.portal.foreign_assets import classify_download_dialog

logger = logging.getLogger(__name__)

_REPORT = "app-foreign-asset-report"
_DROPDOWN = f"{_REPORT} app-dropdown-btn.fy-dropdown"
_DOWNLOAD = f"{_REPORT} button.download-btn"
_DIALOG = "mat-dialog-container#dynamicCommonPopups"
_MESSAGE = f"{_DIALOG} app-logout-alert .sub-message"
_OK = f"{_DIALOG} app-logout-alert button.btn-primary"
_MENU_TIMEOUT_MS = 10_000
_DIALOG_TIMEOUT_MS = 30_000
_DOWNLOAD_TIMEOUT_S = 45.0
_DOWNLOAD_AFTER_OK_S = 20.0


def _year_pattern(year: int) -> re.Pattern[str]:
    return re.compile(rf"^\s*C\.Y\.\s*{year}\s*$")


async def _dismiss_dialog(page: Page) -> None:
    ok = page.locator(_OK).filter(has_text=re.compile(r"^\s*Ok\s*$"))
    try:
        await ok.first.click(timeout=5_000)
        await page.locator(_DIALOG).first.wait_for(state="hidden", timeout=10_000)
    except PlaywrightTimeout:
        logger.warning("Foreign-assets dialog did not close url=%s", page.url)


async def _take_download(future: asyncio.Future, timeout: float) -> Any | None:
    if future.done():
        return future.result()
    try:
        return await asyncio.wait_for(asyncio.shield(future), timeout)
    except asyncio.TimeoutError:
        return None


async def _read_download(download: Any) -> bytes | None:
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    handle.close()
    try:
        await download.save_as(handle.name)
        return Path(handle.name).read_bytes()
    except Exception:
        logger.exception("Could not read the foreign-assets download")
        return None
    finally:
        try:
            os.remove(handle.name)
        except OSError:
            pass


async def _select_year(page: Page, year: int) -> str:
    """``selected``, ``missing``, or ``drift``."""
    dropdown = page.locator(_DROPDOWN)
    toggle = dropdown.locator("button.dropdown-toggle")
    try:
        await toggle.first.wait_for(state="visible", timeout=_MENU_TIMEOUT_MS)
        await toggle.first.click()
        await dropdown.locator("div.dropdown-menu").first.wait_for(
            state="visible", timeout=_MENU_TIMEOUT_MS
        )
    except PlaywrightTimeout:
        logger.warning("Calendar-year dropdown did not open")
        return "drift"

    item = dropdown.locator("button.dropdown-item").filter(
        has_text=_year_pattern(year)
    )
    if await item.count() == 0:
        await page.keyboard.press("Escape")
        return "missing"
    await item.first.click()
    try:
        await dropdown.locator("span.value-text").filter(
            has_text=re.compile(rf"C\.Y\.\s*{year}\b")
        ).first.wait_for(state="visible", timeout=_MENU_TIMEOUT_MS)
    except PlaywrightTimeout:
        logger.warning("C.Y. %s did not become the selected year", year)
        return "drift"
    return "selected"


async def download_foreign_assets_year(page: Page, year: int) -> dict[str, Any]:
    """One calendar year. ``pdf`` is set only when a PDF file arrived."""
    selected = await _select_year(page, year)
    if selected == "missing":
        return {"calendar_year": year, "status": "year_not_in_dropdown"}
    if selected != "selected":
        return {
            "status": "page_drift",
            "error_code": "UI_DRIFT",
            "error": "The calendar-year dropdown on Foreign Assets Information did not work.",
        }

    button = page.locator(_DOWNLOAD)
    try:
        await button.first.wait_for(state="visible", timeout=_MENU_TIMEOUT_MS)
    except PlaywrightTimeout:
        return {
            "status": "page_drift",
            "error_code": "UI_DRIFT",
            "error": "Download PDF is not on the Foreign Assets Information page.",
        }

    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()

    def _on_download(download: Any) -> None:
        if future.done():
            return
        loop.call_soon_threadsafe(future.set_result, download)

    page.on("download", _on_download)
    try:
        logger.info("Download PDF for C.Y. %s", year)
        await button.first.click()
        message = page.locator(_MESSAGE)
        try:
            await message.first.wait_for(state="visible", timeout=_DIALOG_TIMEOUT_MS)
        except PlaywrightTimeout:
            return {
                "calendar_year": year,
                "status": "failed",
                "error_code": "DOWNLOAD_FAILED",
                "error": (
                    f"Download PDF for C.Y. {year} did not show a result dialog."
                ),
            }
        kind = classify_download_dialog(await message.first.inner_text())
        if kind == "no_data":
            await _dismiss_dialog(page)
            return {"calendar_year": year, "status": "no_data"}
        if kind != "downloaded":
            await _dismiss_dialog(page)
            return {
                "calendar_year": year,
                "status": "failed",
                "error_code": "DOWNLOAD_FAILED",
                "error": f"Download PDF for C.Y. {year} showed an unknown dialog.",
            }

        download = await _take_download(future, _DOWNLOAD_TIMEOUT_S)
        await _dismiss_dialog(page)
        if download is None:
            download = await _take_download(future, _DOWNLOAD_AFTER_OK_S)
        if download is None:
            return {
                "calendar_year": year,
                "status": "failed",
                "error_code": "DOWNLOAD_FAILED",
                "error": (
                    f"The password dialog appeared for C.Y. {year}, "
                    "but no PDF arrived."
                ),
            }
        pdf = await _read_download(download)
        if not pdf or not pdf.startswith(b"%PDF"):
            return {
                "calendar_year": year,
                "status": "failed",
                "error_code": "DOWNLOAD_FAILED",
                "error": f"C.Y. {year} did not download as a PDF.",
            }
        return {"calendar_year": year, "status": "downloaded", "pdf": pdf}
    finally:
        page.remove_listener("download", _on_download)
