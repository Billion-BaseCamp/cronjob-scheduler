"""Scheduling rules for notice runs. No database, no browser."""

from __future__ import annotations

from datetime import date
from uuid import uuid4

from app.portal.notice_schedule import (
    REASON_ACTION_REQUIRED,
    REASON_CARRIED_FORWARD,
    JobOutcome,
    action_count_from_result,
    is_settled,
    monthly_run_ok,
    notice_assessment_year,
    select_watchlist,
    summarize,
    weekly_should_skip,
)


def test_assessment_year_label_follows_the_financial_year() -> None:
    assert notice_assessment_year(date(2026, 10, 3)) == "2026-27"
    assert notice_assessment_year(date(2027, 3, 31)) == "2026-27"
    assert notice_assessment_year(date(2027, 4, 1)) == "2027-28"
    assert notice_assessment_year(date(2099, 12, 1)) == "2099-00"


def test_weekly_is_skipped_on_a_monday_28th() -> None:
    assert date(2026, 9, 28).weekday() == 0
    assert weekly_should_skip(date(2026, 9, 28)) is True
    assert weekly_should_skip(date(2026, 10, 5)) is False
    assert weekly_should_skip(date(2026, 10, 27)) is False


def test_weekly_is_skipped_the_monday_after_a_sunday_28th() -> None:
    # 28 Feb 2027 and 28 Mar 2027 are Sundays.
    assert date(2027, 3, 1).weekday() == 0
    assert weekly_should_skip(date(2027, 3, 1)) is True
    assert date(2027, 3, 29).weekday() == 0
    assert weekly_should_skip(date(2027, 3, 29)) is True
    assert weekly_should_skip(date(2027, 3, 8)) is False
    assert weekly_should_skip(date(2027, 4, 5)) is False


def test_waiting_jobs_count_as_settled_but_queued_and_running_do_not() -> None:
    assert is_settled({}) is True
    assert is_settled({"completed": 10, "failed": 2, "waiting_for_password": 3}) is True
    assert is_settled({"completed": 10, "timed_out": 1, "cancelled": 1}) is True
    assert is_settled({"completed": 10, "running": 1}) is False
    assert is_settled({"completed": 10, "queued": 4}) is False
    assert is_settled({"completed": 10, "queued": 0}) is True


def test_action_count_uses_the_stored_value_then_the_notices() -> None:
    assert action_count_from_result({"action_required_count": 2}) == 2
    assert action_count_from_result({"action_required_count": 0, "notices": []}) == 0
    assert (
        action_count_from_result(
            {
                "notices": [
                    {"source": "e_proceedings", "has_submit_response": True},
                    {"source": "e_proceedings", "has_submit_response": False},
                ]
            }
        )
        == 1
    )
    assert action_count_from_result(None) is None
    assert action_count_from_result({"action_required_count": True}) is None


def _outcomes(completed: int, ui_drift: int = 0, bad_password: int = 0, waiting: int = 0):
    rows = [JobOutcome(uuid4(), "completed", action_required_count=0) for _ in range(completed)]
    rows += [JobOutcome(uuid4(), "failed", "UI_DRIFT") for _ in range(ui_drift)]
    rows += [JobOutcome(uuid4(), "failed", "INVALID_PASSWORD") for _ in range(bad_password)]
    rows += [
        JobOutcome(uuid4(), "waiting_for_otp", "OTP_REQUIRED") for _ in range(waiting)
    ]
    return rows


def test_portal_outage_fails_the_monthly_run() -> None:
    summary = summarize(_outcomes(completed=100, ui_drift=1100))
    ok, reason = monthly_run_ok(summary, 0.8)
    assert ok is False
    assert "100 of 1200" in reason


def test_password_and_otp_problems_do_not_count_against_the_run() -> None:
    summary = summarize(_outcomes(completed=700, ui_drift=50, bad_password=300, waiting=150))
    assert summary["client_problem"] == 450
    assert summary["system_failed"] == 50
    ok, _ = monthly_run_ok(summary, 0.8)
    assert ok is True


def test_empty_or_all_client_problem_runs_fail() -> None:
    assert monthly_run_ok(summarize([]), 0.8)[0] is False
    assert monthly_run_ok(summarize(_outcomes(completed=0, bad_password=20)), 0.8)[0] is False


def test_watchlist_takes_action_required_and_carries_forward_unchecked_clients() -> None:
    needs_action = uuid4()
    cleared = uuid4()
    failed_listed = uuid4()
    failed_new = uuid4()
    not_queued_listed = uuid4()
    job_failed = uuid4()
    outcomes = [
        JobOutcome(needs_action, "completed", job_id=uuid4(), action_required_count=2),
        JobOutcome(cleared, "completed", job_id=uuid4(), action_required_count=0),
        JobOutcome(failed_listed, "failed", "INVALID_PASSWORD", job_id=job_failed),
        JobOutcome(failed_new, "waiting_for_otp", "OTP_REQUIRED", job_id=uuid4()),
    ]
    previous = [cleared, failed_listed, not_queued_listed]

    entries = {e.client_id: e for e in select_watchlist(outcomes, previous)}

    assert set(entries) == {needs_action, failed_listed, not_queued_listed}
    assert entries[needs_action].reason == REASON_ACTION_REQUIRED
    assert entries[needs_action].action_required_count == 2
    assert entries[failed_listed].reason == REASON_CARRIED_FORWARD
    assert entries[failed_listed].job_id == job_failed
    assert entries[not_queued_listed].reason == REASON_CARRIED_FORWARD
    assert entries[not_queued_listed].job_id is None


def test_first_monthly_run_has_nothing_to_carry_forward() -> None:
    a, b = uuid4(), uuid4()
    entries = select_watchlist(
        [
            JobOutcome(a, "completed", action_required_count=1),
            JobOutcome(b, "failed", "UI_DRIFT"),
        ],
        [],
    )
    assert [e.client_id for e in entries] == [a]
