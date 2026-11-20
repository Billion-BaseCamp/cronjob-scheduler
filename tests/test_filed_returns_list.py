"""Load every View Filed Returns card before the year filter.

Page 1 of the short list has no footer View More. That button sits beside
Back on the last short page. Clicking it grows the list, removes itself, and
turns the next-page arrow back on. The View More inside a card is ignored.
"""

from __future__ import annotations

import json

import pytest

playwright_api = pytest.importorskip("playwright.async_api")

from app.portal.actions.read_filed_return_status import (  # noqa: E402
    _click_view_more,
)

_PAGE = """
<app-itr-status>
  <div id="cards"></div>
  <mat-paginator class="mat-mdc-paginator" style="display:block;margin-top:24px">
    <div class="mat-mdc-paginator-page-size" style="position:relative;display:inline-block">
      <div class="mat-mdc-paginator-page-size-label">Items per page:</div>
      <mat-select class="mat-mdc-select" style="display:inline-block;width:48px;height:24px">
        <span class="mat-mdc-select-value-text"><span class="mat-mdc-select-min-line">06</span></span>
      </mat-select>
      <div class="mat-mdc-paginator-touch-target" aria-hidden="true"
           style="__TOUCH_STYLE__"></div>
    </div>
    <div class="mat-mdc-paginator-range-label"></div>
    <button type="button" class="mat-mdc-paginator-navigation-next"
            style="__NEXT_STYLE__">></button>
  </mat-paginator>
  <div style="display:flex;justify-content:space-between;margin-top:16px">
    <button type="button" class="largeButton">&lt; Back</button>
    <div class="viewMoreBtnContainer"></div>
  </div>
</app-itr-status>
<div class="cdk-overlay-container"></div>
<script>
  const sizes = __SIZES__;
  let pageSize = 6;
  let page = 1;
  let expanded = false;
  let inlineClicks = 0;
  let viewMoreClicks = 0;
  let nextClicks = 0;
  const overlay = document.querySelector('.cdk-overlay-container');

  function total() { return expanded ? 19 : 8; }
  function pageCount() { return Math.ceil(total() / pageSize); }

  function render() {
    if (page > pageCount()) page = pageCount();
    const start = (page - 1) * pageSize;
    const end = Math.min(start + pageSize, total());
    document.querySelector('.mat-mdc-select-min-line').textContent =
      String(pageSize).padStart(2, '0');
    document.querySelector('.mat-mdc-paginator-range-label').textContent =
      `${start + 1} – ${end} of ${total()}`;
    const next = document.querySelector('.mat-mdc-paginator-navigation-next');
    next.disabled = page >= pageCount();
    const container = document.querySelector('.viewMoreBtnContainer');
    container.innerHTML = '';
    if (!expanded && page === pageCount()) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'largeButton';
      button.textContent = 'View More';
      button.addEventListener('click', () => {
        viewMoreClicks += 1;
        expanded = true;
        render();
      });
      container.appendChild(button);
    }
    const cards = document.querySelector('#cards');
    cards.innerHTML = '';
    for (let index = start; index < end; index += 1) {
      const card = document.createElement('mat-card');
      card.className = 'contextBox';
      const inline = document.createElement('button');
      inline.type = 'button';
      inline.className = 'hyperLink';
      inline.textContent = 'View more';
      inline.addEventListener('click', () => { inlineClicks += 1; });
      card.appendChild(inline);
      cards.appendChild(card);
    }
    document.body.dataset.pageSize = String(pageSize);
    document.body.dataset.page = String(page);
    document.body.dataset.pages = String(pageCount());
    document.body.dataset.expanded = expanded ? '1' : '0';
    document.body.dataset.inlineClicks = String(inlineClicks);
    document.body.dataset.viewMoreClicks = String(viewMoreClicks);
    document.body.dataset.nextClicks = String(nextClicks);
  }

  function closeMenu() { overlay.innerHTML = ''; }
  function openMenu() {
    closeMenu();
    const backdrop = document.createElement('div');
    backdrop.className = 'cdk-overlay-backdrop cdk-overlay-backdrop-showing';
    const pane = document.createElement('div');
    pane.className = 'cdk-overlay-pane';
    for (const label of sizes) {
      const option = document.createElement('mat-option');
      option.textContent = label;
      option.addEventListener('click', () => {
        pageSize = parseInt(label, 10);
        page = 1;
        closeMenu();
        render();
      });
      pane.appendChild(option);
    }
    overlay.append(backdrop, pane);
  }
  document.querySelector('.mat-mdc-paginator-touch-target').addEventListener('click', openMenu);
  document.querySelector('mat-select').addEventListener('click', openMenu);
  document.querySelector('.mat-mdc-paginator-navigation-next').addEventListener('click', () => {
    if (page >= pageCount()) return;
    nextClicks += 1;
    page += 1;
    render();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeMenu();
  });
  render();
</script>
"""


def _html(sizes: list[str], *, hidden_controls: bool = False) -> str:
    touch = "display:none" if hidden_controls else "position:absolute;inset:0"
    nxt = "visibility:hidden" if hidden_controls else ""
    return (
        _PAGE.replace("__SIZES__", json.dumps(sizes))
        .replace("__TOUCH_STYLE__", touch)
        .replace("__NEXT_STYLE__", nxt)
    )


@pytest.fixture
async def page():
    async with playwright_api.async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch()
        except Exception as exc:
            pytest.skip(f"Chromium unavailable: {exc}")
        try:
            yield await browser.new_page(viewport={"width": 1440, "height": 900})
        finally:
            await browser.close()


async def _state(page) -> dict[str, str]:
    return await page.evaluate(
        """() => ({
            pageSize: document.body.dataset.pageSize,
            page: document.body.dataset.page,
            pages: document.body.dataset.pages,
            expanded: document.body.dataset.expanded,
            inlineClicks: document.body.dataset.inlineClicks,
            viewMoreClicks: document.body.dataset.viewMoreClicks,
            nextClicks: document.body.dataset.nextClicks,
        })"""
    )


async def _assert_loaded(page, sizes: list[str]) -> dict[str, str]:
    await _click_view_more(page)
    state = await _state(page)
    assert state["expanded"] == "1"
    assert state["page"] == state["pages"]
    assert state["viewMoreClicks"] == "1"
    assert state["inlineClicks"] == "0"
    assert state["pageSize"] == str(max(int(size) for size in sizes))
    if sizes == ["06"]:
        assert int(state["nextClicks"]) >= 1
    return state


@pytest.mark.parametrize("sizes", [["06"], ["06", "10"]])
async def test_footer_view_more_and_next_load_every_filing(page, sizes: list[str]) -> None:
    await page.set_content(_html(sizes))
    await _assert_loaded(page, sizes)


async def test_hidden_page_size_target_still_pages_to_view_more(page) -> None:
    """Staging keeps the touch target and Next in the DOM without a visible box."""
    sizes = ["06"]
    await page.set_content(_html(sizes, hidden_controls=True))
    await _assert_loaded(page, sizes)
