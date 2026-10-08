"""Items per Page on the e-Proceedings list, in a real headless browser.

The page copies the portal's Angular Material paginator: a transparent touch
target sits over the select and opens it, so a click on the select itself is
intercepted (seen on staging, 8 Oct 2026).
"""

from __future__ import annotations

import pytest

playwright_api = pytest.importorskip("playwright.async_api")

from app.portal.actions.read_e_proceedings_notices import (  # noqa: E402
    PAGE_SIZE_SELECT,
    PAGINATOR_SELECTOR,
    _current_page_size,
    _show_largest_page,
)

_PAGINATOR_HTML = """
<mat-paginator class="mat-mdc-paginator" style="display:block;padding:24px">
  <div class="mat-mdc-paginator-page-size" style="position:relative;display:inline-block">
    <mat-select class="mat-mdc-select" role="combobox"
                style="display:inline-block;width:80px;height:24px">
      <span class="mat-mdc-select-value-text">10</span>
    </mat-select>
    %s
  </div>
</mat-paginator>
<div id="overlay"></div>
<script>
  function openMenu() {
    const panel = document.createElement('div');
    panel.className = 'mat-mdc-select-panel';
    for (const n of ['10', '25', '50']) {
      const option = document.createElement('mat-option');
      option.textContent = n;
      option.style.display = 'block';
      option.addEventListener('click', () => {
        document.querySelector('.mat-mdc-select-value-text').textContent = n;
        panel.remove();
      });
      panel.appendChild(option);
    }
    document.getElementById('overlay').appendChild(panel);
  }
  const target = document.querySelector('.mat-mdc-paginator-touch-target');
  (target || document.querySelector('mat-select')).addEventListener('click', openMenu);
</script>
"""

_TOUCH_TARGET = (
    '<div aria-hidden="true" class="mat-mdc-paginator-touch-target" '
    'style="position:absolute;top:-12px;left:-12px;width:104px;height:48px"></div>'
)


@pytest.fixture
async def page():
    async with playwright_api.async_playwright() as p:
        try:
            browser = await p.chromium.launch()
        except Exception as exc:  # browser binaries not installed
            pytest.skip(f"Chromium unavailable: {exc}")
        try:
            yield await browser.new_page()
        finally:
            await browser.close()


async def test_touch_target_blocks_a_direct_click_on_the_select(page) -> None:
    await page.set_content(_PAGINATOR_HTML % _TOUCH_TARGET)
    select = page.locator(PAGINATOR_SELECTOR).locator(PAGE_SIZE_SELECT)
    with pytest.raises(playwright_api.TimeoutError, match="intercepts pointer events"):
        await select.first.click(timeout=1500)


async def test_largest_page_size_is_picked_through_the_touch_target(page) -> None:
    await page.set_content(_PAGINATOR_HTML % _TOUCH_TARGET)
    assert await _show_largest_page(page) == 50
    assert await _current_page_size(page) == 50


async def test_select_is_clicked_when_there_is_no_touch_target(page) -> None:
    await page.set_content(_PAGINATOR_HTML % "")
    assert await _show_largest_page(page) == 50
    assert await _current_page_size(page) == 50
