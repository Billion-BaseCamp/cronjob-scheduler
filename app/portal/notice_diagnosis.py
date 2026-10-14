"""Pure notice diagnosis — no browser. Summaries for scraped notices."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping, Optional

from app.portal.notice_sections import section_short_name

# The only e-Proceedings description that needs a response without a due date.
NO_RESPONSE_LETTER = "no response letter"


def _parse_iso_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%B-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text[:32], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def format_inr(amount: Any) -> str:
    """Indian digit grouping: 4190703 → ₹41,90,703."""
    try:
        value = int(round(float(amount)))
    except (TypeError, ValueError):
        return "₹?"
    sign = "-" if value < 0 else ""
    digits = str(abs(value))
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups + [tail])
    return f"{sign}₹{digits}"


def _is_no_response_letter(description: Any) -> bool:
    return NO_RESPONSE_LETTER in " ".join(str(description or "").split()).lower()


def notice_needs_action(notice: Mapping[str, Any]) -> bool:
    """Response still pending. Pending Payment after a filed response does not count.

    e-Proceedings: Submit Response counts only with a Response Due Date, or for a
    "No Response Letter". A due-date label the scraper could not read counts too,
    so a parse failure never hides a notice.
    """
    source = (notice.get("source") or "").strip()
    if source == "outstanding_demand" or "is_extinguished" in notice:
        if notice.get("is_extinguished"):
            return False
        actions = notice.get("actions") or {}
        return bool(actions.get("submit_response"))
    if not notice.get("has_submit_response"):
        return False
    if notice.get("response_due_date") or notice.get("response_due_date_unreadable"):
        return True
    return _is_no_response_letter(notice.get("description"))


def diagnose_outstanding_demand(demand: Mapping[str, Any]) -> str:
    section = (demand.get("notice_section") or "").strip()
    us = f"u/s {section}" if section else "(section unknown)"
    ay = demand.get("assessment_year")
    where = f"{us}, AY {ay}" if ay else us
    principal = demand.get("outstanding_demand_amount")
    amount = format_inr(principal) if principal is not None else "amount unknown"

    if demand.get("is_extinguished"):
        return f"Demand {where} is extinguished ({amount}); no action needed."

    interest = demand.get("accrued_interest") or demand.get("final_interest")
    total = f"{amount} + interest {format_inr(interest)}" if interest else amount
    status = demand.get("current_status") or "status unknown"
    actions = demand.get("actions") or {}
    response_type = demand.get("response_type")
    ao = demand.get("ao_response")
    ao_part = f" AO: {ao}." if ao else ""

    if actions.get("submit_response"):
        return (
            f"Outstanding demand {total} {where} — {status}. "
            f"No response filed; Submit Response pending.{ao_part}"
        )
    if response_type:
        tail = " Re-Submit Response available." if actions.get("resubmit_response") else ""
        return (
            f"Outstanding demand {total} {where} — {status}. "
            f"Response: {response_type}.{ao_part}{tail}"
        )
    return f"Outstanding demand {total} {where} — {status}.{ao_part}"


def diagnose_notice(notice: Mapping[str, Any]) -> str:
    """Short advisor-facing summary for one notice row."""
    if (notice.get("source") or "").strip() == "outstanding_demand":
        return diagnose_outstanding_demand(notice)
    section = (notice.get("notice_section") or "").strip() or "notice"
    label = section_short_name(
        None if section == "notice" else section,
        default="E-proceeding notice",
    )
    due = _parse_iso_date(notice.get("response_due_date"))
    due_label = due.strftime("%d-%b-%Y") if due else "unknown date"
    actionable = notice_needs_action(notice)
    source = (notice.get("source") or "e_proceedings").strip()
    us = f"u/s {section}" if section != "notice" else "notice"

    if source == "e_proceedings":
        if not actionable and notice.get("has_submit_response"):
            return (
                f"{label} {us} shows Submit Response but has no response due date; "
                "not counted as action required."
            )
        if actionable:
            # Keep 142(1) wording sharp — highest operational urgency at filing stage.
            if section.upper().startswith("142"):
                return (
                    f"Critical inquiry notice {us} pending response. "
                    f"Legal timeline closes on {due_label}."
                )
            return (
                f"{label} {us} pending Submit Response "
                f"(due {due_label})."
            )
        return (
            f"{label} {us} already responded "
            f"(latest notice; due was {due_label})."
        )

    if actionable:
        return f"Pending action from {source} (due {due_label})."
    return f"Latest notice from {source} appears responded (due was {due_label})."


def attach_summaries(notices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in notices:
        enriched = dict(row)
        enriched["summary"] = diagnose_notice(enriched)
        out.append(enriched)
    return out
