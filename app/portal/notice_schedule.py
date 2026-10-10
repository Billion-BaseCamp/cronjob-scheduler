"""Rules for scheduled notice runs. No database, no browser.

- Monthly e-Proceedings (28th): check every client, then choose next month's
  weekly list.
- Weekly e-Proceedings (Mondays): check only that list plus manual entries.
- Monthly outstanding demand (27th): check every client.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable, Mapping, Optional
from uuid import UUID

from app.portal.notice_alerts import ADVISOR_FIXABLE_ERRORS, actionable_notices

WORKFLOW_E_PROCEEDINGS = "CHECK_E_PROCEEDINGS_NOTICES"
WORKFLOW_OUTSTANDING_DEMAND = "CHECK_OUTSTANDING_DEMAND"

RUN_E_PROCEEDINGS_MONTHLY = "e_proceedings_monthly"
RUN_E_PROCEEDINGS_WEEKLY = "e_proceedings_weekly"
RUN_OUTSTANDING_DEMAND_MONTHLY = "outstanding_demand_monthly"

RUN_WORKFLOW = {
    RUN_E_PROCEEDINGS_MONTHLY: WORKFLOW_E_PROCEEDINGS,
    RUN_E_PROCEEDINGS_WEEKLY: WORKFLOW_E_PROCEEDINGS,
    RUN_OUTSTANDING_DEMAND_MONTHLY: WORKFLOW_OUTSTANDING_DEMAND,
}

E_PROCEEDINGS_MONTHLY_DAY = 28
OUTSTANDING_DEMAND_MONTHLY_DAY = 27
CRON_HOUR = 23

REASON_ACTION_REQUIRED = "action_required"
REASON_CARRIED_FORWARD = "carried_forward"

# Waiting jobs need a person and would never finish on their own.
SETTLED_STATUSES = frozenset(
    {
        "completed",
        "failed",
        "timed_out",
        "cancelled",
        "waiting_for_password",
        "waiting_for_otp",
        "waiting_for_human",
    }
)
WAITING_STATUSES = frozenset(
    {"waiting_for_password", "waiting_for_otp", "waiting_for_human"}
)


def notice_assessment_year(today: date) -> str:
    """Label for the job row. Notices are not tied to one year; this is a dedup key."""
    start = today.year if today.month >= 4 else today.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


def weekly_should_skip(today: date) -> bool:
    """Skip on the 28th and on the Monday right after a Sunday 28th.

    The monthly run owns the 28th. The next night it has just checked the same
    clients, so a weekly run would only repeat the advisor emails.
    """
    yesterday = today - timedelta(days=1)
    return E_PROCEEDINGS_MONTHLY_DAY in (today.day, yesterday.day)


@dataclass(frozen=True)
class JobOutcome:
    client_id: UUID
    status: str
    error_code: Optional[str] = None
    job_id: Optional[UUID] = None
    action_required_count: Optional[int] = None


@dataclass(frozen=True)
class WatchEntry:
    client_id: UUID
    reason: str
    action_required_count: Optional[int]
    job_id: Optional[UUID]


def is_settled(status_counts: Mapping[str, int]) -> bool:
    return all(status in SETTLED_STATUSES for status, n in status_counts.items() if n)


def action_count_from_result(result: Any) -> Optional[int]:
    """Count stored on the job, or recomputed from its notices for older rows."""
    if not isinstance(result, Mapping):
        return None
    raw = result.get("action_required_count")
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    notices = result.get("notices")
    if isinstance(notices, list):
        return len(actionable_notices(notices))
    return None


def _is_client_problem(outcome: JobOutcome) -> bool:
    return outcome.status in WAITING_STATUSES or (
        outcome.error_code or ""
    ) in ADVISOR_FIXABLE_ERRORS


def summarize(outcomes: Iterable[JobOutcome]) -> dict[str, int]:
    rows = list(outcomes)
    completed = sum(1 for o in rows if o.status == "completed")
    client_problem = sum(1 for o in rows if o.status != "completed" and _is_client_problem(o))
    return {
        "total": len(rows),
        "completed": completed,
        "client_problem": client_problem,
        "system_failed": len(rows) - completed - client_problem,
        "action_required": sum(
            1
            for o in rows
            if o.status == "completed" and (o.action_required_count or 0) >= 1
        ),
    }


def monthly_run_ok(summary: Mapping[str, int], min_success_ratio: float) -> tuple[bool, str]:
    """A portal outage or changed page must not replace the list with an empty one.

    Password, OTP, and CAPTCHA problems belong to the client, not the run, so
    they are left out of the ratio.
    """
    total = summary.get("total", 0)
    if total == 0:
        return False, "No client checks were queued."
    checked = total - summary.get("client_problem", 0)
    completed = summary.get("completed", 0)
    if checked <= 0 or completed == 0:
        return False, "No client check succeeded."
    ratio = completed / checked
    if ratio < min_success_ratio:
        return False, (
            f"Only {completed} of {checked} checks succeeded "
            f"({ratio:.0%}, need {min_success_ratio:.0%})."
        )
    return True, ""


def select_watchlist(
    outcomes: Iterable[JobOutcome],
    previous_client_ids: Iterable[UUID],
) -> list[WatchEntry]:
    """Next month's weekly list.

    A completed check with at least one notice needing action puts the client
    on. A client from last month's list without a completed check this month
    (failed, stuck, or not queued) keeps their place.
    """
    rows = list(outcomes)
    completed = {o.client_id: o for o in rows if o.status == "completed"}
    latest_job = {o.client_id: o.job_id for o in rows}
    entries: dict[UUID, WatchEntry] = {}
    for client_id, outcome in completed.items():
        count = outcome.action_required_count or 0
        if count >= 1:
            entries[client_id] = WatchEntry(
                client_id=client_id,
                reason=REASON_ACTION_REQUIRED,
                action_required_count=count,
                job_id=outcome.job_id,
            )
    for client_id in previous_client_ids:
        if client_id in completed or client_id in entries:
            continue
        entries[client_id] = WatchEntry(
            client_id=client_id,
            reason=REASON_CARRIED_FORWARD,
            action_required_count=None,
            job_id=latest_job.get(client_id),
        )
    return list(entries.values())
