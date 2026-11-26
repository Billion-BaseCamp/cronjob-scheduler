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
import random
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
_ACTIVITY_HISTORY_TIMEOUT_S = 180.0
_ACTIVITY_POLL_INTERVAL_S = 5.0
_IST = timezone(timedelta(hours=5, minutes=30))
_MONTH_ABBRS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)
_CAPTCHA_ERROR_GRACE_S = 3.0
_RETRY_SETTLE_MS = 1_000

_INSTRUCTIONS_DOWNLOAD_BTN_RE = re.compile(r"Download\s+AIS\s*/\s*TIS", re.I)
_AIS_SUBTAB_RE = re.compile(r"^\s*AIS\s*$")
_AIS_JSON_ROW_RE = re.compile(r"JSON\s*\(\s*for\s+AIS\s+Utility\s*\)", re.I)
_DOWNLOAD_BTN_RE = re.compile(r"^\s*Download\s*$", re.I)
_PROCEED_BTN_RE = re.compile(r"^\s*Proceed\s*$", re.I)
_CANCEL_BTN_RE = re.compile(r"^\s*Cancel\s*$", re.I)
_CAPTCHA_MODAL_TEXT_RE = re.compile(
    r"Please\s+download\s+JSON\s+file|data\s+on\s+the\s+utility", re.I
)
_NO_DATA_RE = re.compile(r"No\s+data\s+available\s+for\s+the\s+selected", re.I)
_INVALID_CAPTCHA_RE = re.compile(
    r"invalid\s+captcha|incorrect\s+captcha|captcha\s+does\s+not\s+match|enter\s+valid\s+captcha",
    re.I,
)
_LARGE_FILE_QUEUED_RE = re.compile(
    r"Go\s+To\s+Activity\s+History|available\s+shortly\s+under\s+Activity\s+History|"
    r"download\s+request\s+has\s+been\s+submitted",
    re.I,
)
_REFERENCE_ID_RE = re.compile(r"Reference\s+ID\s*:\s*([A-Za-z0-9]+)", re.I)
_GO_TO_ACTIVITY_HISTORY_RE = re.compile(r"Go\s+To\s+Activity\s+History", re.I)

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
        await locator.first.wait_for(state="visible", timeout=timeout)
        return True
    except PlaywrightTimeout:
        return False


async def _human_pause(page: Page) -> None:
    """Brief randomized pause between portal UI steps to avoid bot-speed bursts."""
    lo = max(0.0, settings.PORTAL_ACTION_MIN_DELAY_SECONDS)
    hi = max(lo, settings.PORTAL_ACTION_MAX_DELAY_SECONDS)
    if hi <= 0.0:
        return
    ms = int(random.uniform(lo, hi) * 1000)
    if ms > 0:
        await page.wait_for_timeout(ms)


async def _find_ais_subtab(page: Page) -> Locator | None:
    """Find the ``AIS`` sub-tab next to ``Instructions`` (never the top navbar/breadcrumb)."""
    inst = page.locator("a, button, [role='tab'], li, span").filter(
        has_text=re.compile(r"^\s*Instructions\s*$")
    )
    if await inst.count() > 0:
        for xpath in ("xpath=../..", "xpath=..", "xpath=../../.."):
            container = inst.first.locator(xpath)
            if await container.count() == 0:
                continue
            candidate = container.locator(
                "a, button, [role='tab'], li"
            ).filter(has_text=_AIS_SUBTAB_RE)
            if await candidate.count() == 1 and await candidate.first.is_visible():
                return candidate.first

    non_nav = page.locator(
        "a:not(nav.ctm-navbar a):not(.breadcrumb a), "
        "button:not(nav.ctm-navbar button), "
        "[role='tab']"
    ).filter(has_text=_AIS_SUBTAB_RE)
    count = await non_nav.count()
    for idx in range(count - 1, -1, -1):
        cand = non_nav.nth(idx)
        if await cand.is_visible():
            return cand
    return None


async def _wait_for_spinner(page: Page, timeout_ms: int = 10_000) -> None:
    spinner = page.locator(
        "ngx-spinner .overlay, .ngx-spinner-overlay, div[class*='spinner-overlay']:visible"
    )
    try:
        if await spinner.count() > 0 and await spinner.first.is_visible():
            await spinner.first.wait_for(state="hidden", timeout=timeout_ms)
    except PlaywrightTimeout:
        pass


async def _select_fy_on_ais_tab(page: Page, financial_year: str) -> str:
    """Switch to the ``AIS`` sub-tab and pick ``F.Y. <financial_year>``.

    Returns ``selected``, ``missing``, or ``drift``.
    """
    await _wait_for_spinner(page)
    ais_tab = await _find_ais_subtab(page)
    if ais_tab is None:
        logger.warning("AIS sub-tab was not found url=%s", page.url)
        return "drift"
    try:
        logger.info("Clicking AIS sub-tab url=%s", page.url)
        await ais_tab.click(timeout=_PAGE_TIMEOUT_MS)
    except PlaywrightTimeout:
        logger.warning("Timed out clicking AIS sub-tab url=%s", page.url)
        return "drift"

    try:
        await page.locator("app-ais-instructions").first.wait_for(
            state="hidden", timeout=10_000
        )
    except PlaywrightTimeout:
        pass
    await _wait_for_spinner(page)
    logger.info("On AIS sub-tab url=%s", page.url)

    dropdown = page.locator("app-dropdown-btn.fy-dropdown, app-dropdown-btn")
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
            await _human_pause(page)
    except PlaywrightTimeout:
        # Fallback: native <select> or Angular Material <mat-select> if present
        native_select = page.locator("select").filter(has_text=_fy_regex(financial_year))
        if await native_select.count() > 0 and await native_select.first.is_visible():
            options = await native_select.first.locator("option").all_inner_texts()
            matched = next(
                (opt for opt in options if _fy_regex(financial_year).search(opt)),
                None,
            )
            if matched:
                await native_select.first.select_option(label=matched)
                return "selected"
        logger.warning(
            "Could not select F.Y. %s on AIS tab url=%s", financial_year, page.url
        )
        return "drift"
    return "selected"


async def _open_download_modal(page: Page, financial_year: str) -> str:
    """Open the AIS/TIS Download modal for ``financial_year``.

    Returns ``opened``, ``missing``, or ``drift``.
    """
    instructions_btn = page.locator("button:visible, a:visible").filter(
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

    # On ais/home, the "Annual Information Statement (AIS)" card footer has an icon-only
    # <img title="Download AIS related documents" src="assets/images/svgs/download.svg">.
    ais_card = page.locator(".card, mat-card, app-ais-card").filter(
        has=page.locator(".app-ais-card-sub-title, .card-title").filter(
            has_text=re.compile(r"Annual\s+Information\s+Statement", re.I)
        )
    ).last
    ais_card_icon = ais_card.locator(
        "img[title*='Download AIS' i], img[src*='download.svg']"
    )
    if await _visible(ais_card_icon, timeout=10_000):
        await _human_pause(page)
        logger.info("Clicking AIS card download icon url=%s", page.url)
        await ais_card_icon.first.click()
        return "opened"

    ais_download_btn = page.locator(
        "button.download-btn:visible, button:visible"
    ).filter(
        has_text=re.compile(
            r"^\s*Download(?:\s+AIS(?:\s*/\s*TIS)?)?(?:\s*\(.*\))?\s*$", re.I
        )
    )
    if await _visible(ais_download_btn, timeout=3_000):
        logger.info("Clicking Download button on AIS tab url=%s", page.url)
        await ais_download_btn.first.click()
        return "opened"

    card_btn = ais_card.locator("button:visible").filter(
        has_text=re.compile(r"\bDownload\b", re.I)
    )
    if await _visible(card_btn, timeout=2_000):
        logger.info("Clicking AIS card Download button url=%s", page.url)
        await card_btn.first.click()
        return "opened"

    logger.warning("Download button not found on AIS tab url=%s", page.url)
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

    row = dialog.locator("div.d-flex").filter(has_text=_AIS_JSON_ROW_RE).filter(
        has=page.locator("button").filter(has_text=_DOWNLOAD_BTN_RE)
    )
    if await row.count() == 0:
        row = dialog.locator("div, tr, li").filter(has_text=_AIS_JSON_ROW_RE).filter(
            has=page.locator("button").filter(has_text=_DOWNLOAD_BTN_RE)
        )
    if await row.count() > 0:
        btn = row.last.locator("button").filter(has_text=_DOWNLOAD_BTN_RE)
        if await btn.count() > 0:
            await _human_pause(page)
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


async def _find_captcha_image(dialog: Locator, timeout_ms: int = 10_000) -> Locator | None:
    """Wait for the captcha ``<img>``, ``<canvas>``, or ``<svg>`` banner inside the dialog."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + (timeout_ms / 1000.0)
    selector = (
        "img[src^='data:image'], img[alt*='captcha' i], img[id*='captcha' i], "
        "canvas, img, svg"
    )
    while loop.time() < deadline:
        candidates = dialog.locator(selector)
        count = await candidates.count()
        best: Locator | None = None
        best_area = 0.0
        for idx in range(count):
            candidate = candidates.nth(idx)
            try:
                if not await candidate.is_visible():
                    continue
                box = await candidate.bounding_box()
            except PlaywrightTimeout:
                continue
            if not box:
                continue
            area = float(box["width"]) * float(box["height"])
            # Captcha banners are wide; skip small info/refresh/audio icons.
            if box["width"] >= 80 and box["height"] >= 25 and area > best_area:
                best = candidate
                best_area = area
        if best is not None:
            return best
        await dialog.page.wait_for_timeout(250)
    return None


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


def extract_reference_id(text: str | None) -> str | None:
    """Extract Reference ID from the large-AIS queued notice."""
    match = _REFERENCE_ID_RE.search(text or "")
    return match.group(1).strip() if match else None


async def _wait_download_or_reject(
    page: Page,
    future: asyncio.Future,
    captcha_dialog: Locator,
) -> tuple[Any | None, str]:
    """Wait for a direct file download, large-file Activity History notice, no-data, or invalid captcha."""
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

        if _NO_DATA_RE.search(dlg_text) or _LARGE_FILE_QUEUED_RE.search(dlg_text):
            return None, dlg_text
        if _INVALID_CAPTCHA_RE.search(dlg_text):
            if error_seen_at is None:
                error_seen_at = loop.time()
            elif loop.time() - error_seen_at >= 0.4:
                return None, dlg_text
        elif error_seen_at is None and await captcha_dialog.is_visible():
            pass
        await page.wait_for_timeout(200)

    if future.done():
        return future.result(), ""
    return None, ""


_ACTIVITY_DOWNLOAD_SELECTOR = (
    "a[title='Download file'], a:has(img[src*='download.svg']), img[src*='download.svg']"
)


def _today_ist_date_regex() -> re.Pattern[str]:
    now = datetime.now(_IST).date()
    day_pat = rf"0?{now.day}"
    month_num = f"{now.month:02d}"
    month_abbr = _MONTH_ABBRS[now.month - 1]
    return re.compile(
        rf"\b(?:{day_pat}[\s/-](?:{month_num}|{month_abbr})[\s/-]{now.year}|"
        rf"{now.year}-{month_num}-{now.day:02d})\b",
        re.I,
    )


async def _find_activity_download_icon(
    page: Page,
    *,
    financial_year: str,
    reference_id: str | None,
) -> Locator | None:
    """Find the actual download icon (never the processing-gauge icon) in Activity History."""
    rows = page.locator("tr.mat-row, tr.cdk-row")
    if reference_id:
        ref_rows = rows.filter(has_text=re.compile(re.escape(reference_id)))
        if await ref_rows.count() > 0:
            icon = ref_rows.first.locator(_ACTIVITY_DOWNLOAD_SELECTOR)
            if await icon.count() > 0 and await icon.first.is_visible():
                return icon.first
        return None

    # Only when no Reference ID was parsed from the modal: match today's row for this F.Y.
    fy_rows = (
        rows.filter(has_text=re.compile(r"AIS\s+Downloaded\s*-\s*JSON", re.I))
        .filter(has_text=_fy_regex(financial_year))
        .filter(has_text=_today_ist_date_regex())
    )
    if await fy_rows.count() > 0:
        icon = fy_rows.first.locator(_ACTIVITY_DOWNLOAD_SELECTOR)
        if await icon.count() > 0 and await icon.first.is_visible():
            return icon.first
    return None


async def _refresh_activity_history(page: Page) -> None:
    """Re-fetch Activity History via Angular navigation (``page.reload()`` loses the SPA session)."""
    other_nav = page.locator("nav.ctm-navbar a").filter(
        has_text=re.compile(r"e-Campaign", re.I)
    )
    ah_nav = page.locator("nav.ctm-navbar a").filter(
        has_text=re.compile(r"Activity\s+History", re.I)
    )
    try:
        if await other_nav.count() > 0:
            await other_nav.first.click(timeout=5_000)
            await page.wait_for_url(
                re.compile(r"campaign", re.I), timeout=10_000
            )
            await _wait_for_spinner(page)
        if await ah_nav.count() > 0:
            await ah_nav.first.click(timeout=10_000)
            await page.wait_for_url(
                re.compile(r"activity-history", re.I), timeout=10_000
            )
            await page.locator("table").first.wait_for(
                state="visible", timeout=_PAGE_TIMEOUT_MS
            )
    except PlaywrightTimeout:
        logger.warning("Activity History refresh did not return to table url=%s", page.url)


async def _download_from_activity_history(
    page: Page,
    *,
    financial_year: str,
    reference_id: str | None,
) -> bytes | None:
    """Navigate to Activity History (via button or navbar) and download the queued AIS JSON."""
    go_btn = page.locator("mat-dialog-container:visible button, mat-dialog-container:visible a").filter(
        has_text=_GO_TO_ACTIVITY_HISTORY_RE
    )
    if await go_btn.count() > 0 and await go_btn.first.is_visible():
        logger.info("Clicking Go To Activity History (ref=%s)", reference_id or "-")
        await go_btn.first.click()
    else:
        await _dismiss_open_dialogs(page)
        ah_nav = page.locator("nav.ctm-navbar a").filter(
            has_text=re.compile(r"Activity\s+History", re.I)
        )
        if await ah_nav.count() == 0:
            return None
        logger.info("Opening Activity History from navbar (ref=%s)", reference_id or "-")
        await ah_nav.first.click()

    await _dismiss_open_dialogs(page)
    try:
        await page.locator("table").first.wait_for(state="visible", timeout=_PAGE_TIMEOUT_MS)
    except PlaywrightTimeout:
        logger.warning("Activity History table did not load url=%s", page.url)
        return None

    loop = asyncio.get_running_loop()
    timeout_s = max(
        30.0,
        float(
            getattr(
                settings,
                "AIS_ACTIVITY_HISTORY_TIMEOUT_SECONDS",
                _ACTIVITY_HISTORY_TIMEOUT_S,
            )
        ),
    )
    deadline = loop.time() + timeout_s
    icon: Locator | None = None
    while loop.time() < deadline:
        await _wait_for_spinner(page)
        icon = await _find_activity_download_icon(
            page, financial_year=financial_year, reference_id=reference_id
        )
        if icon is not None:
            break
        logger.info(
            "AIS JSON still processing in Activity History (ref=%s url=%s); polling again",
            reference_id or "-",
            page.url,
        )
        await page.wait_for_timeout(int(_ACTIVITY_POLL_INTERVAL_S * 1000))
        await _refresh_activity_history(page)

    if icon is None:
        logger.warning(
            "AIS JSON download link did not appear in Activity History (ref=%s)",
            reference_id or "-",
        )
        return None

    future: asyncio.Future = loop.create_future()

    def _on_download(download: Any) -> None:
        if future.done():
            return
        loop.call_soon_threadsafe(future.set_result, download)

    page.on("download", _on_download)
    try:
        await _human_pause(page)
        logger.info("Clicking Activity History download icon (ref=%s)", reference_id or "-")
        await icon.click()
        dl_deadline = loop.time() + _DOWNLOAD_TIMEOUT_S
        while loop.time() < dl_deadline:
            if future.done():
                return await _read_download_bytes(future.result())
            await page.wait_for_timeout(200)
        return None
    finally:
        page.remove_listener("download", _on_download)


async def _dismiss_open_dialogs(page: Page) -> None:
    """Close any open AIS Download / Captcha modals so AIS logout can click the header."""
    for _ in range(3):
        dialogs = page.locator("mat-dialog-container:visible")
        if await dialogs.count() == 0:
            return
        top = dialogs.last
        close_btn = top.locator(
            "button:has-text('Cancel'), button:has-text('Ok'), "
            "button[aria-label='Close'], img[alt='Close']"
        )
        if await close_btn.count() > 0:
            try:
                await close_btn.first.click(timeout=3_000)
                await page.wait_for_timeout(300)
                continue
            except PlaywrightTimeout:
                pass
        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)
        except Exception:
            return


async def _close_cancel_best_effort(dialog: Locator | None) -> None:
    if dialog is None:
        return
    await _dismiss_open_dialogs(dialog.page)


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
        await _dismiss_open_dialogs(page)
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
        await _dismiss_open_dialogs(page)
        return AisJsonDownloadResult(
            status="no_data",
            financial_year=financial_year,
        )

    captcha_dialog = await _find_captcha_dialog(page)
    if captcha_dialog is None:
        await _dismiss_open_dialogs(page)
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
            await _dismiss_open_dialogs(page)
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
        await _human_pause(page)

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
            await _dismiss_open_dialogs(page)
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
            await _dismiss_open_dialogs(page)
            return AisJsonDownloadResult(
                status="no_data",
                financial_year=financial_year,
                captcha_attempts=attempts_used,
            )
        if _LARGE_FILE_QUEUED_RE.search(dlg_text):
            ref_id = extract_reference_id(dlg_text)
            raw = await _download_from_activity_history(
                page, financial_year=financial_year, reference_id=ref_id
            )
            await _dismiss_open_dialogs(page)
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
                error_message=(
                    "Large AIS JSON request was submitted to Activity History, "
                    "but the file could not be downloaded."
                ),
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
