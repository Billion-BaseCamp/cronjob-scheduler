"""Items per Page on the e-Proceedings list, in a real headless browser.

The page copies the portal's Angular Material paginator as captured on
9 Oct 2026: a transparent touch target over the select opens it; the options
(10 / 25 / 50) render in a position:fixed overlay panel; a page-wide
transparent backdrop stays until the panel closes. On staging the panel opened
below the 1440x900 window, so a mouse click could not reach "50", and the
backdrop left open then blocked View Notices/Orders and logout.
"""

from __future__ import annotations

import json

import pytest

playwright_api = pytest.importorskip("playwright.async_api")

from app.portal.actions import read_e_proceedings_notices as notices  # noqa: E402
from app.portal.actions.read_e_proceedings_notices import (  # noqa: E402
    OVERLAY_BACKDROP_SELECTOR,
    PAGE_SIZE_OPTION,
    PAGE_SIZE_SELECT,
    PAGINATOR_SELECTOR,
    _current_page_size,
    _open_page_size_menu,
    _show_largest_page,
)

_VIEWPORT = {"width": 1440, "height": 900}
# Three 48px options from 820px: "50" sits at 916-964px, below the window.
_PANEL_BELOW_WINDOW = 820
_PANEL_ON_SCREEN = 500

_PAGE = """
<button id="view-notices" style="margin:16px"
        onclick="document.body.dataset.viewed = '1'">View Notices/Orders (1)</button>
<mat-paginator id="paginator" class="mat-mdc-paginator"
               style="display:block;position:absolute;top:740px;left:150px">
  <div class="mat-mdc-paginator-page-size" style="position:relative;display:inline-block">
    <div class="mat-mdc-paginator-page-size-label">Items per Page:</div>
    <mat-form-field style="display:inline-block;position:relative">
      <mat-select class="mat-mdc-select" role="combobox"
                  style="display:inline-block;width:80px;height:24px">
        <span class="mat-mdc-select-value-text"><span class="mat-mdc-select-min-line">10</span></span>
      </mat-select>
      __TOUCH_TARGET__
    </mat-form-field>
  </div>
  <div class="mat-mdc-paginator-range-label">1 of 1 pages</div>
</mat-paginator>
<div class="cdk-overlay-container"></div>
<script>
  const cfg = __CONFIG__;
  const overlay = document.querySelector('.cdk-overlay-container');
  function closeMenu() { overlay.innerHTML = ''; }
  function openMenu() {
    const backdrop = document.createElement('div');
    backdrop.className =
      'cdk-overlay-backdrop cdk-overlay-transparent-backdrop cdk-overlay-backdrop-showing';
    backdrop.style.cssText = 'position:fixed;inset:0;z-index:1000';
    backdrop.addEventListener('click', closeMenu);
    const panel = document.createElement('div');
    panel.className = 'mat-mdc-select-panel';
    panel.style.cssText =
      `position:fixed;left:300px;top:${cfg.panelTop}px;width:84px;z-index:1001;background:#fff`;
    for (const n of ['10', '25', '50']) {
      const option = document.createElement('mat-option');
      option.innerHTML = `<span class="mdc-list-item__primary-text"> ${n} </span>`;
      option.style.cssText = 'display:block;height:48px';
      option.addEventListener('click', () => {
        if (!cfg.optionWorks) return;
        document.querySelector('.mat-mdc-select-min-line').textContent = n;
        closeMenu();
      });
      panel.appendChild(option);
    }
    overlay.append(backdrop, panel);
  }
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeMenu(); });
  const target = document.querySelector('.mat-mdc-paginator-touch-target');
  (target || document.querySelector('mat-select')).addEventListener('click', openMenu);
</script>
"""

_TOUCH_TARGET = (
    '<div aria-hidden="true" class="mat-mdc-paginator-touch-target" '
    'style="position:absolute;top:-12px;left:-12px;width:104px;height:48px"></div>'
)


def _html(*, touch_target: bool = True, panel_top: int = _PANEL_BELOW_WINDOW,
          option_works: bool = True) -> str:
    config = json.dumps({"panelTop": panel_top, "optionWorks": option_works})
    return (
        _PAGE.replace("__TOUCH_TARGET__", _TOUCH_TARGET if touch_target else "")
        .replace("__CONFIG__", config)
    )


@pytest.fixture
async def page():
    async with playwright_api.async_playwright() as p:
        try:
            browser = await p.chromium.launch()
        except Exception as exc:  # browser binaries not installed
            pytest.skip(f"Chromium unavailable: {exc}")
        try:
            yield await browser.new_page(viewport=_VIEWPORT)
        finally:
            await browser.close()


async def _page_is_clickable(page) -> bool:
    await page.locator("#view-notices").click(timeout=1500)
    return await page.evaluate("document.body.dataset.viewed === '1'")


async def test_touch_target_blocks_a_direct_click_on_the_select(page) -> None:
    await page.set_content(_html())
    select = page.locator(PAGINATOR_SELECTOR).locator(PAGE_SIZE_SELECT)
    with pytest.raises(playwright_api.TimeoutError, match="intercepts pointer events"):
        await select.first.click(timeout=1500)


async def test_mouse_cannot_reach_an_option_below_the_window(page) -> None:
    await page.set_content(_html())
    await _open_page_size_menu(page)
    with pytest.raises(playwright_api.TimeoutError, match="outside of the viewport"):
        await page.locator(PAGE_SIZE_OPTION).nth(2).click(timeout=1500)


@pytest.mark.parametrize("panel_top", [_PANEL_BELOW_WINDOW, _PANEL_ON_SCREEN])
async def test_largest_page_size_is_picked_and_the_page_stays_clickable(
    page, panel_top: int
) -> None:
    await page.set_content(_html(panel_top=panel_top))
    assert await _show_largest_page(page) == 50
    assert await _current_page_size(page) == 50
    assert await page.locator(OVERLAY_BACKDROP_SELECTOR).count() == 0
    assert await _page_is_clickable(page)


async def test_select_is_clicked_when_there_is_no_touch_target(page) -> None:
    await page.set_content(_html(touch_target=False, panel_top=_PANEL_ON_SCREEN))
    assert await _show_largest_page(page) == 50
    assert await _current_page_size(page) == 50


async def test_failed_pick_still_closes_the_dropdown(page, monkeypatch) -> None:
    monkeypatch.setattr(notices, "_OVERLAY_TIMEOUT_MS", 500)
    await page.set_content(_html(option_works=False))
    assert await _show_largest_page(page) is None
    assert await _current_page_size(page) == 10
    assert await page.locator(OVERLAY_BACKDROP_SELECTOR).count() == 0
    assert await _page_is_clickable(page)
