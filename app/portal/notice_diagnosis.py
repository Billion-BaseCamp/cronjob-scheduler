"""Pure notice diagnosis — no browser. Summaries for scraped notices."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping, Optional

from app.portal.notice_sections import section_short_name


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


def diagnose_notice(notice: Mapping[str, Any]) -> str:
    """Short advisor-facing summary for one notice row."""
    section = (notice.get("notice_section") or "").strip() or "notice"
    label = section_short_name(
        None if section == "notice" else section,
        default="E-proceeding notice",
    )
    due = _parse_iso_date(notice.get("response_due_date"))
    due_label = due.strftime("%d-%b-%Y") if due else "unknown date"
    actionable = bool(notice.get("has_submit_response"))
    source = (notice.get("source") or "e_proceedings").strip()
    us = f"u/s {section}" if section != "notice" else "notice"

    if source == "e_proceedings":
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
