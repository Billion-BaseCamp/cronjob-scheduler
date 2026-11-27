"""Open Form 168 / TRACES Annual Tax Statement from e-filing.

Flow (selectors are text-resilient; refine after a headed pass):
  1. Return to Dashboard (My Profile leaves the tiles/menus).
  2. Click ``View Form 168, Income Tax Act 2025`` only — never Form 26AS / Act 1961.
  3. Land on TDS TRACES dashboard (new tab or same tab).
  4. Click ``View/Download Annual Tax Statement``.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)

_PAGE_TIMEOUT_MS = 20_000
_POPUP_TIMEOUT_S = 30.0
_POLL_MS = 250

DASHBOARD_SELECTOR = "a#Dashboard"
E_FILE_SELECTOR = "a#e-File"
SERVICES_SELECTOR = "a#Services, a[id*='Service' i]"
_MENU_ITEM = "button.mat-mdc-menu-item"
# Strict: Form 168 under Income Tax Act 2025 only. Never match Form 26AS / Act 1961.
_FORM_168_LINK = re.compile(
    r"View\s+Form\s+168\s*,?\s*Income\s+Tax\s+Act\s+2025|"
    r"Form\s+168\s*,?\s*(?:Income\s+Tax\s+)?Act\s+2025",
    re.I,
)
_REJECT_LEGACY_26AS = re.compile(r"26AS|Act\s+1961", re.I)
_ATS_CARD = re.compile(
    r"View\s*/\s*Download\s+Annual\s+Tax\s+Statement|Annual\s+Tax\s+Statement",
    re.I,
)
_TRACES_HINT = re.compile(r"TRACES|Welcome\s+Taxpayer|Annual\s+Tax\s+Statement", re.I)
_TRACES_LOGOUT = re.compile(r"^\s*Logout\s*$", re.I)
_INCOME_TAX_FORMS = re.compile(r"Income\s+Tax\s+Forms", re.I)
_INCOME_TAX_RETURNS = re.compile(r"Income\s+Tax\s+Returns", re.I)
# Form 168 is under e-File → Income Tax Forms (flyout). Prefer hover over click.
_FORM_168_MENU_POLL_MS = 100
_FORM_168_AFTER_SUBMENU_MS = 3_000
_FORM_168_TOPLEVEL_FALLBACK_MS = 600


@dataclass
class TracesPages:
    page: Page | None = None
    error_code: str | None = None
    message: str | None = None


async def _visible_text(page: Page, pattern: re.Pattern[str], timeout: int = 1_500) -> bool:
    try:
        return await page.get_by_text(pattern).first.is_visible(timeout=timeout)
    except PlaywrightTimeout:
        return False


async def _is_traces(page: Page) -> bool:
    url = (page.url or "").lower()
    if "tdscpc.gov.in" in url or "traces" in url:
        return True
    try:
        body = await page.locator("body").inner_text(timeout=2_000)
    except Exception:
        return False
    return bool(_TRACES_HINT.search(body or ""))


async def _take_opened_page(page: Page, opened: list[Page]) -> Page | None:
    if opened:
        tab = opened[-1]
        try:
            await tab.wait_for_load_state("domcontentloaded")
        except PlaywrightTimeout:
            logger.warning("Form 168 / TRACES tab did not finish loading url=%s", tab.url)
        logger.info("Form 168 opened in a new tab url=%s", tab.url)
        return tab
    if await _is_traces(page):
        logger.info("Form 168 opened in the current tab url=%s", page.url)
        return page
    return None


async def _go_dashboard(page: Page) -> None:
    """My Profile / deep links hide dashboard tiles — Form 168 lives there."""
    if "/dashboard" in (page.url or "") and "myProfile" not in (page.url or ""):
        # Already on dashboard home — skip the slow re-navigation wait.
        return
    dash = page.locator(DASHBOARD_SELECTOR)
    try:
        if await dash.count() and await dash.first.is_visible(timeout=2_000):
            logger.info("Opening Dashboard before Form 168")
            await dash.first.click()
            try:
                await page.wait_for_url(
                    re.compile(r".*#/dashboard(?:$|/|\?)"), timeout=5_000
                )
            except PlaywrightTimeout:
                await page.wait_for_timeout(800)

    except PlaywrightTimeout:
        logger.warning("Dashboard control not clickable url=%s", page.url)


def _is_form_168_act_2025_label(text: str | None) -> bool:
    label = (text or "").strip()
    if not label:
        return False
    if _REJECT_LEGACY_26AS.search(label):
        return False
    return bool(_FORM_168_LINK.search(label))


async def _locator_label(locator) -> str:
    try:
        text = (await locator.inner_text(timeout=500) or "").strip()
        if text:
            return re.sub(r"\s+", " ", text)
    except Exception:
        pass
    try:
        return ((await locator.get_attribute("aria-label")) or "").strip()
    except Exception:
        return ""


async def _find_form_168_locator(page: Page):
    """Return a Form 168 / Act 2025 control if present; never Form 26AS / Act 1961."""
    candidates = [
        page.locator(_MENU_ITEM).filter(has_text=_FORM_168_LINK),
        page.get_by_role("link", name=_FORM_168_LINK),
        page.get_by_role("button", name=_FORM_168_LINK),
        page.get_by_role("menuitem", name=_FORM_168_LINK),
        page.get_by_text(_FORM_168_LINK),
    ]
    for loc in candidates:
        count = await loc.count()
        for i in range(count):
            item = loc.nth(i)
            label = await _locator_label(item)
            if not _is_form_168_act_2025_label(label):
                continue
            try:
                if await item.is_visible(timeout=400):
                    return item
            except PlaywrightTimeout:
                continue
            except Exception:
                continue
    return None


async def _wait_for_form_168_menu(page: Page, timeout_ms: int) -> object | None:
    """Poll until Form 168 / Act 2025 is visible; return the locator or None."""
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        found = await _find_form_168_locator(page)
        if found is not None:
            return found
        await page.wait_for_timeout(_FORM_168_MENU_POLL_MS)
    return None


async def _open_income_tax_forms_submenu(page: Page) -> bool:
    """Open the Income Tax Forms flyout.

    Material submenu triggers are slow when clicked; prefer hover / expand only.
    """
    item = page.locator(_MENU_ITEM).filter(has_text=_INCOME_TAX_FORMS)
    if not await item.count():
        item = page.get_by_role("menuitem", name=_INCOME_TAX_FORMS)
    if not await item.count():
        return False
    try:
        target = item.first
        await target.wait_for(state="visible", timeout=5_000)
        # Already expanded from a prior hover — do not click again.
        expanded = await target.get_attribute("aria-expanded")
        if expanded == "true":
            logger.info("Income Tax Forms submenu already expanded")
            return True

        logger.info("Opening Income Tax Forms submenu via hover")
        await target.hover()
        # Give the flyout a tick; Form 168 wait is polled separately.
        await page.wait_for_timeout(200)

        expanded = await target.get_attribute("aria-expanded")
        if expanded == "true":
            return True

        # Fallback: focus + ArrowRight is cheaper than a full click on some builds.
        try:
            await target.focus()
            await page.keyboard.press("ArrowRight")
            await page.wait_for_timeout(200)
            expanded = await target.get_attribute("aria-expanded")
            if expanded == "true":
                return True
        except Exception:
            pass

        # Last resort click — this is the slow path the portal shows.
        logger.info("Income Tax Forms hover did not expand; clicking submenu trigger")
        await target.click()
        return True
    except Exception:
        logger.exception("Income Tax Forms submenu open failed")
        return False


async def _reveal_form_168_menus(page: Page) -> None:
    """Open e-File → Income Tax Forms and surface Form 168 / Act 2025."""
    await _go_dashboard(page)

    efile = page.locator(E_FILE_SELECTOR)
    if not await efile.count():
        return

    try:
        logger.info("Opening e-File for Form 168")
        await efile.first.click()
        # Form 168 is often a direct e-File leaf — prefer that over the slow
        # Income Tax Forms submenu trigger.
        if await _wait_for_form_168_menu(page, _FORM_168_TOPLEVEL_FALLBACK_MS) is not None:
            logger.info("Form 168 / Act 2025 visible under e-File")
            return

        if await _open_income_tax_forms_submenu(page):
            if await _wait_for_form_168_menu(page, _FORM_168_AFTER_SUBMENU_MS) is not None:
                logger.info("Form 168 / Act 2025 visible under Income Tax Forms")
                return

        returns = page.locator(_MENU_ITEM).filter(has_text=_INCOME_TAX_RETURNS)
        if await returns.count():
            try:
                await returns.first.hover()
                await page.wait_for_timeout(150)
            except Exception:
                pass
            await _wait_for_form_168_menu(page, 1_000)
    except Exception:
        logger.exception("e-File Form 168 reveal failed")


async def _click_form_168_target(page: Page) -> Page | None:
    """Click Form 168 / Act 2025 only; return the TRACES page if it opens."""
    target = await _find_form_168_locator(page)
    if target is None:
        return None
    try:
        if not await target.is_visible(timeout=500):
            return None
    except PlaywrightTimeout:
        return None

    label = await _locator_label(target)
    if not _is_form_168_act_2025_label(label):
        logger.warning("Refusing non-Form-168 Act 2025 menu leaf label=%r", label)
        return None

    opened: list[Page] = []

    def _on_page(new_page: Page) -> None:
        opened.append(new_page)

    context = page.context
    context.on("page", _on_page)
    try:
        logger.info("Clicking Form 168 / Act 2025 entry label=%r", label)
        await target.click()
        deadline = time.monotonic() + _POPUP_TIMEOUT_S
        while time.monotonic() < deadline:
            tab = await _take_opened_page(page, opened)
            if tab is not None:
                return tab
            await page.wait_for_timeout(_POLL_MS)
    finally:
        context.remove_listener("page", _on_page)
    return None


async def _open_form_168_tab(page: Page) -> Page | None:
    await _go_dashboard(page)
    tab = await _click_form_168_target(page)
    if tab is not None:
        return tab

    await _reveal_form_168_menus(page)
    tab = await _click_form_168_target(page)
    if tab is not None:
        return tab

    try:
        body = await page.locator("body").inner_text(timeout=3_000)
        sample = " | ".join(
            line.strip()
            for line in (body or "").splitlines()
            if re.search(r"168|26AS|TRACES|Annual\s+Tax|ATS", line, re.I)
        )[:500]
        logger.warning(
            "Form 168 / Act 2025 link not visible url=%s hints=%s",
            page.url,
            sample or "(none)",
        )
    except Exception:
        logger.warning("Form 168 / Act 2025 link not visible url=%s", page.url)
    return None


def _frames(page: Page):
    """Main page plus iframes (TRACES taxpayer UI is often framed)."""
    return [page, *page.frames]


async def _enable_flutter_semantics(page: Page) -> bool:
    """TRACES is Flutter CanvasKit — DOM text is empty until semantics are on."""
    try:
        # Wait for the Flutter shell / canvas to mount.
        await page.wait_for_selector(
            "flt-glass-pane, canvas, flt-semantics-placeholder",
            timeout=60_000,
        )
    except PlaywrightTimeout:
        logger.warning("Flutter shell not found on TRACES url=%s", page.url)
        return False

    try:
        logger.info("Enabling Flutter accessibility semantics on TRACES")
        clicked = await page.evaluate(
            """() => {
              const el = document.querySelector(
                "flt-semantics-placeholder, [aria-label='Enable accessibility']"
              );
              if (!el) return false;
              el.click();
              return true;
            }"""
        )
        if not clicked:
            logger.warning("Flutter semantics placeholder not found")
        await page.wait_for_timeout(1_500)
    except Exception:
        logger.exception("Could not enable Flutter semantics")

    # Semantics tree may appear under flt-semantics.
    try:
        await page.wait_for_selector(
            "flt-semantics, [flt-semantics-node], flt-semantics-host",
            timeout=15_000,
        )
        return True
    except PlaywrightTimeout:
        # Some builds expose role=text / buttons without flt-semantics wrapper.
        return True


async def _wait_traces_ready(page: Page) -> object | None:
    """Return the page/frame once Flutter taxpayer UI is interactable."""
    await _enable_flutter_semantics(page)
    deadline = time.monotonic() + 90.0
    welcome_re = re.compile(r"Welcome\s+Taxpayer", re.I)
    ats_re = re.compile(r"View\s*/\s*Download\s+Annual\s+Tax\s+Statement", re.I)
    while time.monotonic() < deadline:
        for frame in _frames(page):
            try:
                welcome = frame.get_by_text(welcome_re)
                ats = frame.get_by_text(ats_re)
                if await welcome.count() and await welcome.first.is_visible(timeout=400):
                    logger.info("TRACES dashboard ready url=%s", page.url)
                    return frame
                if await ats.count() and await ats.first.is_visible(timeout=400):
                    logger.info("TRACES ATS card ready url=%s", page.url)
                    return frame
                # Flutter semantics often expose roles instead of plain text nodes.
                for name in (
                    "View/Download Annual Tax Statement",
                    "Annual Tax Statement",
                ):
                    role = frame.get_by_role("button", name=name)
                    if await role.count() and await role.first.is_visible(timeout=400):
                        logger.info("TRACES ATS role ready url=%s", page.url)
                        return frame
            except Exception:
                continue
        await page.wait_for_timeout(_POLL_MS)
    logger.warning("TRACES Flutter UI not interactable url=%s", page.url)
    return None


async def _open_ats_card(page: Page) -> bool:
    try:
        await page.wait_for_load_state("domcontentloaded")
    except PlaywrightTimeout:
        pass
    frame = await _wait_traces_ready(page)
    scopes = [frame] if frame is not None else _frames(page)

    for scope in scopes:
        if scope is None:
            continue
        selectors = [
            scope.get_by_role("button", name="View/Download Annual Tax Statement"),
            scope.get_by_role("button", name="Annual Tax Statement"),
            scope.get_by_text("View/Download Annual Tax Statement"),
            scope.get_by_text(re.compile(r"Annual\s+Tax\s+Statement", re.I)),
            scope.locator("[flt-semantics-node]").filter(
                has_text=re.compile(r"Annual\s+Tax\s+Statement", re.I)
            ),
        ]
        for card in selectors:
            try:
                if await card.count() == 0:
                    continue
                await card.first.wait_for(state="visible", timeout=10_000)
            except PlaywrightTimeout:
                continue
            logger.info("Clicking View/Download Annual Tax Statement")
            try:
                await card.first.click(timeout=5_000)
            except Exception:
                await card.first.click(timeout=5_000, force=True)
            # Form may render in the same or another frame.
            form_deadline = time.monotonic() + (_PAGE_TIMEOUT_MS / 1000.0)
            while time.monotonic() < form_deadline:
                for form_scope in _frames(page):
                    try:
                        ctrl = form_scope.get_by_text(
                            re.compile(
                                r"Tax\s+Year|Download\s+Text|Download\s+PDF|Proceed",
                                re.I,
                            )
                        )
                        if await ctrl.count() and await ctrl.first.is_visible(timeout=300):
                            return True
                    except Exception:
                        continue
                await page.wait_for_timeout(_POLL_MS)
            logger.warning(
                "ATS form controls not confirmed after card click url=%s", page.url
            )
            return True

    try:
        samples: list[str] = []
        for scope in _frames(page):
            try:
                body = await scope.locator("body").inner_text(timeout=2_000)
            except Exception:
                continue
            samples.extend(
                line.strip()
                for line in (body or "").splitlines()
                if re.search(r"ATS|26AS|168|Tax\s+Statement|Download|Taxpayer", line, re.I)
            )
        logger.warning(
            "ATS card not visible url=%s hints=%s",
            page.url,
            " | ".join(samples[:20]) or "(none)",
        )
    except Exception:
        logger.warning("ATS card not visible url=%s", page.url)
    return False


async def _save_debug_screenshot(page: Page, label: str) -> None:
    try:
        path = f"/tmp/form168_{label}.png"
        await page.screenshot(path=path, full_page=True)
        logger.info("Wrote Form 168 debug screenshot %s", path)
    except Exception:
        logger.exception("Could not write Form 168 debug screenshot")


async def open_traces_ats(page: Page) -> TracesPages:
    """From e-filing home, open TRACES ATS selection page."""
    traces = await _open_form_168_tab(page)
    if traces is None:
        await _save_debug_screenshot(page, "missing_link")
        return TracesPages(
            error_code="UI_DRIFT",
            message=(
                "Could not find View Form 168, Income Tax Act 2025 on e-filing. "
                "Will not use View Form 26AS, Income Tax Act 1961."
            ),
        )
    if not await _open_ats_card(traces):
        await _save_debug_screenshot(traces, "traces_no_ats")
        return TracesPages(
            page=traces,
            error_code="UI_DRIFT",
            message="Could not open View/Download Annual Tax Statement on TRACES.",
        )
    return TracesPages(page=traces)


async def run_traces_logout(page: Page | None) -> bool:
    """Best-effort TRACES logout. Returns True when Logout was clicked."""
    if page is None:
        return False
    try:
        if page.is_closed():
            return False
    except Exception:
        return False

    try:
        logout = page.get_by_role("link", name=_TRACES_LOGOUT)
        if not await logout.count():
            logout = page.get_by_role("button", name=_TRACES_LOGOUT)
        if not await logout.count():
            logout = page.get_by_text(_TRACES_LOGOUT)
        if not await logout.count():
            logger.info("TRACES Logout control not found url=%s", page.url)
            return False
        await logout.first.click(timeout=5_000)
        logger.info("Clicked TRACES Logout")
        return True
    except Exception:
        logger.exception("TRACES logout failed")
        return False
