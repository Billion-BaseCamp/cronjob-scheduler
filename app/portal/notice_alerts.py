"""Which harvested notices need an advisor, and the email copy for that.

Sent for every completed check that found notices needing action: scheduled
runs, and single or bulk checks started from tax-engine. Not sent for
``cron-silent`` runs, or when the same pending notices were already emailed
for that client in the last ``ALERT_REPEAT_HOURS``. Login and technical
failures are recorded on the job and are not emailed.
"""

from __future__ import annotations

from datetime import date, datetime
from html import escape
from typing import Any, Iterable, Mapping, Optional
from zoneinfo import ZoneInfo

from app.portal.notice_diagnosis import format_inr

CRON_REQUESTED_BY = "cron"
# Scheduled jobs started by hand with --no-email. Queued like cron, never emailed.
CRON_SILENT_REQUESTED_BY = "cron-silent"
IST = ZoneInfo("Asia/Kolkata")
ALERT_REPEAT_HOURS = 24

# Failures caused by the client's portal login, not by the run. A monthly run
# leaves them out of its success ratio.
ADVISOR_FIXABLE_ERRORS = frozenset(
    {
        "INVALID_PASSWORD",
        "MISSING_PASSWORD",
        "INVALID_USER_ID",
        "OTP_REQUIRED",
        "CAPTCHA_REQUIRED",
        "DUAL_LOGIN",
    }
)

_SOURCE_LABEL = {
    "e_proceedings": "e-Proceedings",
    "outstanding_demand": "outstanding demand",
}
_SOURCE_CADENCE = {
    "e_proceedings": "weekly",
    "outstanding_demand": "monthly",
}

_PROCEEDING_HEADERS = (
    "Proceeding", "AY", "Section", "DIN", "Issued on", "Response due", "Details",
)
_DEMAND_HEADERS = (
    "AY", "Section", "Demand ref (DIN)", "Raised on", "Outstanding", "Interest",
    "Status", "Details",
)
_DETAILS_MAX = 160
_DUE_SOON_DAYS = 3

# Inline only: Outlook and Gmail drop <style> blocks.
_BODY_STYLE = "font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#111827;"
_TABLE_STYLE = (
    "border-collapse:collapse;font-family:Arial,Helvetica,sans-serif;"
    "font-size:13px;margin:8px 0 16px;"
)
_TH_STYLE = (
    "border:1px solid #d1d5db;background:#f3f4f6;padding:6px 8px;"
    "text-align:left;white-space:nowrap;"
)
_TD_STYLE = "border:1px solid #d1d5db;padding:6px 8px;vertical-align:top;"
_NOWRAP = "white-space:nowrap;"
_OVERDUE_STYLE = "color:#b91c1c;font-weight:bold;"
_DUE_SOON_STYLE = "color:#b45309;font-weight:bold;"


def notice_needs_action(notice: Mapping[str, Any]) -> bool:
    """Response still pending. Pending Payment after a filed response does not count."""
    source = (notice.get("source") or "").strip()
    if source == "outstanding_demand" or "is_extinguished" in notice:
        if notice.get("is_extinguished"):
            return False
        actions = notice.get("actions") or {}
        return bool(actions.get("submit_response"))
    return bool(notice.get("has_submit_response"))


def actionable_notices(notices: list[Mapping[str, Any]] | None) -> list[Mapping[str, Any]]:
    return [notice for notice in notices or [] if notice_needs_action(notice)]


def is_scheduled(requested_by: Optional[str]) -> bool:
    return (requested_by or "") in (CRON_REQUESTED_BY, CRON_SILENT_REQUESTED_BY)


def emails_allowed(requested_by: Optional[str]) -> bool:
    return (requested_by or "") != CRON_SILENT_REQUESTED_BY


def alert_key(notice: Mapping[str, Any]) -> str:
    """Identity of one pending notice, for "was this already emailed?"."""
    din = str(notice.get("din") or "").strip()
    if din:
        return din
    parts = (
        notice.get("source"),
        notice.get("notice_section"),
        notice.get("assessment_year"),
        notice.get("issued_on") or notice.get("date_of_demand_raised"),
    )
    return "|".join(str(part or "") for part in parts)


def alert_keys(notices: Iterable[Mapping[str, Any]] | None) -> list[str]:
    return sorted({alert_key(notice) for notice in notices or []})


def already_alerted(keys: Iterable[str], recent: Iterable[str]) -> bool:
    """Nothing new: every pending notice was in an email sent recently."""
    wanted = set(keys)
    return bool(wanted) and wanted <= set(recent)


def mask_pan(pan: Optional[str]) -> str:
    raw = (pan or "").strip().upper()
    if len(raw) < 6:
        return raw or "—"
    return f"{raw[:5]}****{raw[-1]}"


def client_display_name(client) -> str:
    name = f"{getattr(client, 'first_name', '') or ''} {getattr(client, 'last_name', '') or ''}".strip()
    return name or "Unnamed client"


def _source_label(source: str) -> str:
    return _SOURCE_LABEL.get(source, source or "notices")


def _iso_date(value: Any) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _show_date(value: Any) -> str:
    parsed = _iso_date(value)
    if parsed:
        return parsed.strftime("%d-%b-%Y")
    return _cell(value)


def _cell(value: Any) -> str:
    text = " ".join(str(value).split()) if value is not None else ""
    return text or "—"


def _clip(text: str, limit: int = _DETAILS_MAX) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _due_phrase(iso: Optional[str], today: date) -> str:
    due = _iso_date(iso)
    if due is None:
        return "due date unknown"
    shown = due.strftime("%d-%b-%Y")
    delta = (due - today).days
    if delta > 1:
        return f"{shown} ({delta} days left)"
    if delta == 1:
        return f"{shown} (1 day left)"
    if delta == 0:
        return f"{shown} (due today)"
    overdue = -delta
    unit = "day" if overdue == 1 else "days"
    return f"{shown} (overdue {overdue} {unit})"


def _inr(amount: Any) -> str:
    if amount is None:
        return "—"
    return format_inr(amount)


def _demand_interest(notice: Mapping[str, Any]) -> Any:
    interest = notice.get("accrued_interest")
    if interest is None:
        interest = notice.get("final_interest")
    return interest


def _proceeding_details(notice: Mapping[str, Any]) -> str:
    text = (
        (notice.get("description") or "").strip()
        or (notice.get("summary") or "").strip()
        or "Submit Response pending"
    )
    return _clip(text)


def _demand_details(notice: Mapping[str, Any]) -> str:
    parts = [
        f"{label}: {' '.join(str(value).split())}"
        for label, value in (
            ("Rectification", notice.get("rectification_rights")),
            ("Act", notice.get("applicable_act")),
            ("AO", notice.get("ao_response")),
        )
        if value and str(value).strip()
    ]
    return _clip(" · ".join(parts)) if parts else "No response filed"


def _proceeding_sort_key(notice: Mapping[str, Any]) -> tuple[bool, date]:
    due = _iso_date(notice.get("response_due_date"))
    return (due is None, due or date.max)


def _amount(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _demand_sort_key(notice: Mapping[str, Any]) -> float:
    return -_amount(notice.get("outstanding_demand_amount"))


def _due_style(iso: Optional[str], today: date) -> str:
    due = _iso_date(iso)
    if due is None:
        return ""
    days = (due - today).days
    if days <= 0:
        return _OVERDUE_STYLE
    if days <= _DUE_SOON_DAYS:
        return _DUE_SOON_STYLE
    return ""


def _proceeding_line(notice: Mapping[str, Any], today: date) -> str:
    section = notice.get("notice_section") or "notice"
    name = notice.get("proceeding_name") or "e-Proceedings"
    ay = notice.get("assessment_year") or "—"
    din = notice.get("din") or "—"
    issued = _show_date(notice.get("issued_on"))
    due = _due_phrase(notice.get("response_due_date"), today)
    return (
        f"- {name} · AY {ay} · u/s {section}\n"
        f"  DIN {din} · issued {issued} · response due {due}\n"
        f"  Details: {_proceeding_details(notice)}"
    )


def _demand_line(notice: Mapping[str, Any]) -> str:
    ay = notice.get("assessment_year") or "—"
    section = notice.get("notice_section") or "—"
    din = notice.get("din") or "—"
    raised = _show_date(notice.get("date_of_demand_raised"))
    return (
        f"- AY {ay} · u/s {section} · DIN {din}\n"
        f"  Outstanding {_inr(notice.get('outstanding_demand_amount'))}"
        f" · interest {_inr(_demand_interest(notice))} · raised {raised}\n"
        f"  Status: {_cell(notice.get('current_status'))}\n"
        f"  Details: {_demand_details(notice)}"
    )


def _proceeding_cells(notice: Mapping[str, Any], today: date) -> list[tuple[str, str]]:
    due = notice.get("response_due_date")
    return [
        (_cell(notice.get("proceeding_name") or "e-Proceedings"), ""),
        (_cell(notice.get("assessment_year")), _NOWRAP),
        (_cell(notice.get("notice_section")), _NOWRAP),
        (_cell(notice.get("din")), ""),
        (_show_date(notice.get("issued_on")), _NOWRAP),
        (_due_phrase(due, today), _NOWRAP + _due_style(due, today)),
        (_proceeding_details(notice), ""),
    ]


def _demand_cells(notice: Mapping[str, Any]) -> list[tuple[str, str]]:
    return [
        (_cell(notice.get("assessment_year")), _NOWRAP),
        (_cell(notice.get("notice_section")), _NOWRAP),
        (_cell(notice.get("din")), ""),
        (_show_date(notice.get("date_of_demand_raised")), _NOWRAP),
        (_inr(notice.get("outstanding_demand_amount")), _NOWRAP),
        (_inr(_demand_interest(notice)), _NOWRAP),
        (_cell(notice.get("current_status")), ""),
        (_demand_details(notice), ""),
    ]


def _html_table(headers: Iterable[str], rows: Iterable[list[tuple[str, str]]]) -> str:
    head = "".join(f'<th style="{_TH_STYLE}">{escape(h)}</th>' for h in headers)
    body = "".join(
        "<tr>"
        + "".join(f'<td style="{_TD_STYLE}{extra}">{escape(text)}</td>' for text, extra in row)
        + "</tr>"
        for row in rows
    )
    return (
        f'<table cellpadding="0" cellspacing="0" style="{_TABLE_STYLE}">'
        f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
    )


def render_action_required_email(
    *,
    client_name: str,
    pan: Optional[str],
    source: str,
    notices: list[Mapping[str, Any]],
    advisor_first_name: Optional[str] = None,
    checked_at: Optional[datetime] = None,
    scheduled: bool = True,
) -> tuple[str, str, str]:
    today = (checked_at.astimezone(IST).date() if checked_at else datetime.now(IST).date())
    label = _source_label(source)
    count = len(notices)
    noun = "notice" if count == 1 else "notices"
    if source == "outstanding_demand":
        noun = "demand" if count == 1 else "demands"
    subject = f"Action required: {client_name} — {count} {label} {noun} pending response"
    greeting = f"Hi {advisor_first_name}," if (advisor_first_name or "").strip() else "Hi,"
    when = (
        checked_at.astimezone(IST).strftime("%d-%b-%Y %H:%M IST")
        if checked_at
        else "just now"
    )
    if source == "outstanding_demand":
        ordered = sorted(notices, key=_demand_sort_key)
        lines = [_demand_line(notice) for notice in ordered]
        table = _html_table(_DEMAND_HEADERS, [_demand_cells(n) for n in ordered])
        what = f"{count} outstanding {noun} with no response filed"
    else:
        ordered = sorted(notices, key=_proceeding_sort_key)
        lines = [_proceeding_line(notice, today) for notice in ordered]
        table = _html_table(
            _PROCEEDING_HEADERS, [_proceeding_cells(n, today) for n in ordered]
        )
        what = f"{count} {noun} pending response"
    if scheduled:
        check = f"The {_SOURCE_CADENCE.get(source, 'scheduled')} Income Tax portal check"
    else:
        check = "An Income Tax portal check"
    footer = (
        f"Checked on {when}. You will be reminded on later checks while a "
        "response is still pending."
    )
    text = (
        f"{greeting}\n\n"
        f"{check} for {client_name} "
        f"(PAN {mask_pan(pan)}) found {what}.\n\n"
        + "\n".join(lines)
        + f"\n\n{footer}\n"
    )
    html = (
        f'<div style="{_BODY_STYLE}">'
        f"<p>{escape(greeting)}</p>"
        f"<p>{escape(check)} for <strong>{escape(client_name)}</strong> "
        f"(PAN {escape(mask_pan(pan))}) found {escape(what)}.</p>"
        f"{table}"
        f"<p>{escape(footer)}</p>"
        "</div>"
    )
    return subject, text, html
