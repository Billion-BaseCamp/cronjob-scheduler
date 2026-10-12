"""Harvest Pending Actions → Response to Outstanding Demand.

API first (``getEntity`` with ``sn: outstandingDemand``), DOM cards for what the
API does not carry (Applicable Act, CTAs, rendered journey). One record per
demand line item — DIN alone is not unique (same DIN, different transactionId).
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Optional

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.core.config import settings
from app.portal.actions.pending_actions_hub import open_response_to_outstanding_demand
from app.portal.actions.read_e_proceedings_notices import (
    NoticeHarvest,
    epoch_ms_to_date,
    parse_ui_date,
)
from app.portal.notice_diagnosis import attach_summaries

logger = logging.getLogger(__name__)

SOURCE = "outstanding_demand"

DEMAND_CARD_SELECTOR = ".card-container.matCard[data-index]"
API_PATH = "/iec/servicesapi/auth/getEntity"
API_SERVICE_NAME = "outstandingDemand"

LABEL_CURRENT_STATUS = "Current Status"
LABEL_RESPONSE_SUBMITTED = "Response Submitted"
LABEL_SERVICE_OF_NOTICE = "Date of Service of Notice"
LABEL_DEMAND_RAISED = "Date of Demand Raised"

# Current Status values seen on live cards and the icon state rendered for each.
_KNOWN_STATUS_STATE = {
    "PENDING PAYMENT": "in_progress",
    "EXTINGUISHED DEMAND": "done",
}

_CARD_TIMEOUT_MS = 12_000
_API_WAIT_S = 8.0

_EXTRACT_CARDS_JS = r"""
(cards) => cards.map((card) => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const ownText = (el) => clean(
    Array.from(el.childNodes)
      .filter((n) => n.nodeType === Node.TEXT_NODE)
      .map((n) => n.textContent)
      .join(' ')
  );
  const stripColon = (s) => s.replace(/\s*:\s*$/, '').trim();

  const header = {};
  card.querySelectorAll('.innerBoxHeader span.body1').forEach((lbl) => {
    const val = lbl.nextElementSibling;
    if (val) header[stripColon(clean(lbl.textContent))] = clean(val.textContent);
  });

  const amounts = {};
  const body = card.querySelector('.card-body');
  if (body) {
    body.querySelectorAll('.dataHeading').forEach((lbl) => {
      const box = lbl.parentElement;
      const val = box && box.querySelector(':scope > .heading6');
      if (val) amounts[ownText(lbl)] = clean(val.textContent);
    });
  }

  const fields = {};
  card.querySelectorAll('.row-part3-large > div').forEach((row) => {
    const lbl = row.querySelector(':scope > .dataHeading');
    if (!lbl) return;
    const val = lbl.nextElementSibling;
    fields[stripColon(clean(lbl.textContent))] = val ? clean(val.textContent) : null;
  });

  const journey = Array.from(card.querySelectorAll('mat-vertical-stepper .mat-step')).map((step) => {
    const icon = step.querySelector('.mat-step-icon');
    const label = step.querySelector('.mat-step-text-label .dataHeading');
    const value = step.querySelector(
      '.mat-step-text-label .statusValue, .mat-step-text-label .subtitle2, .mat-step-text-label .hyperLink'
    );
    return {
      label: label ? clean(label.textContent) : null,
      value: value ? clean(value.textContent) : null,
      icon_class: icon ? icon.className : '',
      extinguished: !!(value && value.classList.contains('ExdFontClr')),
    };
  });

  const buttons = Array.from(card.querySelectorAll('button'))
    .filter((b) => !b.hidden)
    .map((b) => clean(b.textContent))
    .filter(Boolean);

  return {
    data_index: card.getAttribute('data-index'),
    header,
    amounts,
    fields,
    journey,
    buttons,
  };
})
"""


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def _dash_to_none(value: Any) -> Optional[str]:
    text = _clean(value)
    if text is None or text == "-":
        return None
    return text


def parse_rupee_amount(text: Any) -> Optional[int]:
    """``₹4190703`` / ``₹2,73,042`` → int rupees."""
    if text is None:
        return None
    digits = re.sub(r"[^\d.]", "", str(text))
    if not digits:
        return None
    try:
        return int(round(float(digits)))
    except ValueError:
        return None


def _api_amount(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _iso(value: Any) -> Optional[str]:
    parsed = epoch_ms_to_date(value)
    return parsed.isoformat() if parsed else None


def _ui_iso(value: Any) -> Optional[str]:
    parsed = parse_ui_date(str(value or ""))
    return parsed.isoformat() if parsed else None


def journey_from_api(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Rebuild the card stepper from API fields; steps with null values are not rendered."""
    steps: list[dict[str, Any]] = []
    status = _clean(raw.get("currentStatus"))
    if status:
        steps.append(
            {
                "label": LABEL_CURRENT_STATUS,
                "value": status,
                "state": _KNOWN_STATUS_STATE.get(status.upper()),
            }
        )
    for label, key in (
        (LABEL_RESPONSE_SUBMITTED, "responseSubmitted"),
        (LABEL_SERVICE_OF_NOTICE, "dateOfServiceNotice"),
        (LABEL_DEMAND_RAISED, "dateOfDemandraised"),
    ):
        value = _iso(raw.get(key))
        if value:
            steps.append({"label": label, "value": value, "state": "done"})
    return steps


def map_api_demand(raw: dict[str, Any]) -> dict[str, Any]:
    status = _clean(raw.get("currentStatus"))
    transaction_id = raw.get("transactionId") or raw.get("customerTrxId")
    return {
        "source": SOURCE,
        "din": _clean(raw.get("din")),
        "transaction_id": str(transaction_id) if transaction_id is not None else None,
        "assessment_year": _clean(raw.get("itrAy")),
        "notice_section": _clean(raw.get("sectionCode")),
        # Card "Outstanding Demand Amount" is the principal (API ``...WOInterest``).
        "outstanding_demand_amount": _api_amount(
            raw.get("outstandingDemandAmountWOInterest")
        ),
        "outstanding_with_interest": _api_amount(raw.get("outstandingDemandAmount")),
        "original_demand_amount": _api_amount(raw.get("orignalOutStDemandAmount")),
        "accrued_interest": _api_amount(raw.get("accruedInterest")),
        "final_interest": _api_amount(raw.get("finalInterest")),
        "interest_us": _clean(raw.get("interestUs")),
        "current_status": status,
        "current_status_date": _iso(raw.get("currentStatusDate")),
        "is_extinguished": bool(status and "EXTINGUISHED" in status.upper()),
        "rectification_rights": _clean(raw.get("rectificationRights")),
        "mode_of_service": _dash_to_none(raw.get("modeOfService")),
        "response_type": _clean(raw.get("responseType")),
        "response_reason": _clean(raw.get("responseReason")),
        "response_submitted_on": _iso(raw.get("responseSubmitted")),
        "ao_response_type": _clean(raw.get("aoResponseType")),
        "ao_response": _clean(raw.get("aoResponseReason")),
        "ao_response_date": _iso(raw.get("aoResponseDate")),
        "date_of_demand_raised": _iso(raw.get("dateOfDemandraised")),
        "date_of_service_of_notice": _iso(raw.get("dateOfServiceNotice")),
        "applicable_act": None,
        "journey": journey_from_api(raw),
        "journey_source": "api",
        "actions": None,
        "has_submit_response": False,
    }


def demands_from_api_payload(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    rows = payload.get("demandList")
    if not isinstance(rows, list):
        return []
    return [map_api_demand(row) for row in rows if isinstance(row, dict) and row.get("din")]


def _step_state(icon_class: str) -> Optional[str]:
    if "mat-step-icon-state-edit" in icon_class:
        return "in_progress"
    if "mat-step-icon-state-done" in icon_class:
        return "done"
    return None


def journey_from_dom(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for step in steps or []:
        label = _clean(step.get("label"))
        raw_value = _clean(step.get("value"))
        if not label:
            continue
        value = raw_value if label == LABEL_CURRENT_STATUS else (_ui_iso(raw_value) or raw_value)
        out.append(
            {
                "label": label,
                "value": value,
                "state": _step_state(step.get("icon_class") or ""),
            }
        )
    return out


def _actions_from_buttons(buttons: list[str]) -> dict[str, bool]:
    names = {(_clean(b) or "").lower() for b in buttons or []}
    return {
        "pay_now": "pay now" in names,
        "download": "download" in names,
        "submit_response": "submit response" in names,
        "resubmit_response": "re-submit response" in names,
    }


def map_dom_card(raw: dict[str, Any]) -> dict[str, Any]:
    header = raw.get("header") or {}
    amounts = raw.get("amounts") or {}
    fields = raw.get("fields") or {}
    journey = journey_from_dom(raw.get("journey") or [])
    status_step = next(
        (step for step in journey if step["label"] == LABEL_CURRENT_STATUS), None
    )
    by_label = {step["label"]: step["value"] for step in journey}
    status = status_step["value"] if status_step else None
    extinguished = any(step.get("extinguished") for step in raw.get("journey") or [])
    actions = _actions_from_buttons(raw.get("buttons") or [])
    return {
        "source": SOURCE,
        "din": _clean(header.get("Demand Reference No")),
        "transaction_id": None,
        "assessment_year": _clean(header.get("Assessment Year")),
        "notice_section": _clean(fields.get("Section Code")),
        "outstanding_demand_amount": parse_rupee_amount(
            amounts.get("Outstanding Demand Amount")
        ),
        "outstanding_with_interest": None,
        "original_demand_amount": None,
        "accrued_interest": parse_rupee_amount(amounts.get("Accrued Interest")),
        "final_interest": parse_rupee_amount(amounts.get("Final Interest")),
        "interest_us": None,
        "current_status": status,
        "current_status_date": None,
        "is_extinguished": extinguished
        or bool(status and "EXTINGUISHED" in status.upper()),
        "rectification_rights": _dash_to_none(fields.get("Rectification Rights")),
        "mode_of_service": _dash_to_none(fields.get("Mode of Service")),
        "response_type": _clean(fields.get("Response Type")),
        "response_reason": None,
        "response_submitted_on": by_label.get(LABEL_RESPONSE_SUBMITTED),
        "ao_response_type": None,
        "ao_response": _clean(fields.get("AO Response")),
        "ao_response_date": _ui_iso(fields.get("AO Response Date")),
        "date_of_demand_raised": by_label.get(LABEL_DEMAND_RAISED),
        "date_of_service_of_notice": by_label.get(LABEL_SERVICE_OF_NOTICE),
        "applicable_act": _clean(fields.get("Applicable Act")),
        "journey": journey,
        "journey_source": "dom",
        "actions": actions,
        "has_submit_response": actions["submit_response"],
    }


def _match_key(row: dict[str, Any]) -> tuple:
    return (row.get("din"), row.get("assessment_year"), row.get("outstanding_demand_amount"))


def _overlay_dom(api_row: dict[str, Any], dom_row: dict[str, Any]) -> dict[str, Any]:
    merged = dict(api_row)
    merged["applicable_act"] = dom_row.get("applicable_act")
    merged["actions"] = dom_row.get("actions")
    merged["has_submit_response"] = bool(dom_row.get("has_submit_response"))
    if dom_row.get("journey"):
        merged["journey"] = dom_row["journey"]
        merged["journey_source"] = "dom"
    for key in (
        "final_interest",
        "rectification_rights",
        "mode_of_service",
        "response_type",
        "ao_response",
        "ao_response_date",
    ):
        if merged.get(key) is None and dom_row.get(key) is not None:
            merged[key] = dom_row[key]
    return merged


def merge_demands(
    api_rows: list[dict[str, Any]], dom_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """API rows are primary. Cards are matched on (DIN, AY, principal); card order differs from API order."""
    unused = list(dom_rows)
    out: list[dict[str, Any]] = []
    for api_row in api_rows:
        match = next((d for d in unused if _match_key(d) == _match_key(api_row)), None)
        if match is None:
            same = [
                d
                for d in unused
                if (d.get("din"), d.get("assessment_year"))
                == (api_row.get("din"), api_row.get("assessment_year"))
            ]
            match = same[0] if len(same) == 1 else None
        if match is None:
            out.append(api_row)
            continue
        unused.remove(match)
        out.append(_overlay_dom(api_row, match))
    out.extend(unused)
    return out


def _is_outstanding_demand_response(response) -> bool:
    try:
        request = response.request
        if request.method != "POST" or API_PATH not in (response.url or ""):
            return False
        if (request.headers or {}).get("sn") == API_SERVICE_NAME:
            return True
        return API_SERVICE_NAME in (request.post_data or "")
    except Exception:
        return False


async def _scrape_cards(page: Page) -> list[dict[str, Any]]:
    cards = page.locator(DEMAND_CARD_SELECTOR)
    try:
        await cards.first.wait_for(state="visible", timeout=_CARD_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        if await cards.count() == 0:
            return []
    raw_cards = await page.eval_on_selector_all(DEMAND_CARD_SELECTOR, _EXTRACT_CARDS_JS)
    return [map_dom_card(raw) for raw in raw_cards or []]


async def read_outstanding_demand(page: Page | None) -> NoticeHarvest:
    if settings.PORTAL_AUTOMATION_DRY_RUN:
        return NoticeHarvest(source=SOURCE, ok=True, notices=[], scrape_path="dry-run")
    if page is None:
        return NoticeHarvest(source=SOURCE, ok=False, notices=[], error="no page")

    payloads: list[Any] = []
    got_api = asyncio.Event()

    async def _on_response(response) -> None:
        if not _is_outstanding_demand_response(response):
            return
        try:
            payloads.append(await response.json())
            got_api.set()
        except Exception:
            logger.warning("Outstanding demand XHR body not JSON", exc_info=True)

    page.on("response", _on_response)
    try:
        opened = await open_response_to_outstanding_demand(page)
        if not opened:
            return NoticeHarvest(
                source=SOURCE,
                ok=False,
                notices=[],
                error="Could not open Response to Outstanding Demand",
            )
        try:
            await asyncio.wait_for(got_api.wait(), timeout=_API_WAIT_S)
        except asyncio.TimeoutError:
            logger.info("No outstandingDemand XHR captured; relying on DOM")
        dom_rows = await _scrape_cards(page)
    finally:
        try:
            page.remove_listener("response", _on_response)
        except Exception:
            pass

    api_rows = demands_from_api_payload(payloads[-1]) if payloads else []
    if not payloads and not dom_rows:
        return NoticeHarvest(
            source=SOURCE,
            ok=False,
            notices=[],
            error="Outstanding demand page loaded but neither API data nor cards were found",
        )

    demands = merge_demands(api_rows, dom_rows)
    scrape_path = "api+ui" if payloads and dom_rows else ("api" if payloads else "ui")
    logger.info(
        "Outstanding demand harvest: %s api row(s), %s card(s), %s record(s)",
        len(api_rows),
        len(dom_rows),
        len(demands),
    )
    return NoticeHarvest(
        source=SOURCE,
        ok=True,
        notices=attach_summaries(demands),
        scrape_path=scrape_path,
    )
