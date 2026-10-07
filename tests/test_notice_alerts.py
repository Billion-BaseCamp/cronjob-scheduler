"""Advisor email rules for harvested notices. No browser, no database."""

from __future__ import annotations

from datetime import datetime, timezone

from app.portal.notice_alerts import (
    ADVISOR_FIXABLE_ERRORS,
    actionable_notices,
    alert_keys,
    already_alerted,
    emails_allowed,
    is_scheduled,
    mask_pan,
    notice_needs_action,
    render_action_required_email,
)


def test_pending_payment_after_a_filed_response_is_not_action() -> None:
    demand = {
        "source": "outstanding_demand",
        "is_extinguished": False,
        "current_status": "Pending Payment",
        "response_type": "Demand is correct",
        "actions": {"submit_response": False, "pay_now": True},
    }
    assert notice_needs_action(demand) is False


def test_no_response_and_extinguished() -> None:
    pending = {
        "source": "outstanding_demand",
        "is_extinguished": False,
        "actions": {"submit_response": True},
    }
    gone = {
        "source": "outstanding_demand",
        "is_extinguished": True,
        "actions": {"submit_response": False},
    }
    assert notice_needs_action(pending) is True
    assert notice_needs_action(gone) is False


def test_e_proceedings_uses_submit_response_button() -> None:
    assert notice_needs_action({"source": "e_proceedings", "has_submit_response": True})
    assert not notice_needs_action(
        {"source": "e_proceedings", "has_submit_response": False}
    )


def test_actionable_notices_keeps_only_pending_responses() -> None:
    rows = [
        {"source": "e_proceedings", "has_submit_response": True, "din": "a"},
        {"source": "e_proceedings", "has_submit_response": False, "din": "b"},
    ]
    assert [row["din"] for row in actionable_notices(rows)] == ["a"]


def test_mask_pan() -> None:
    assert mask_pan("abcde1234f") == "ABCDE****F"
    assert mask_pan("") == "—"


def test_action_email_names_the_client_and_due_date() -> None:
    subject, text, _html = render_action_required_email(
        client_name="Rahul Sharma",
        pan="ABCDE1234F",
        source="e_proceedings",
        notices=[
            {
                "source": "e_proceedings",
                "has_submit_response": True,
                "proceeding_name": "Inquiry before assessment",
                "assessment_year": "2024-25",
                "notice_section": "142(1)",
                "din": "ITBA/AST/1",
                "issued_on": "2026-09-22",
                "response_due_date": "2026-10-05",
                "summary": "Critical inquiry notice u/s 142(1) pending response.",
            }
        ],
        advisor_first_name="Priya",
        checked_at=datetime(2026, 10, 1, 18, 0, tzinfo=timezone.utc),
    )
    assert subject.startswith("Action required: Rahul Sharma")
    assert "ABCDE****F" in text
    assert "142(1)" in text
    assert "05-Oct-2026" in text
    assert "Priya" in text
    assert "password" not in text.lower()


def test_manual_check_email_has_no_cadence() -> None:
    _subject, text, html = render_action_required_email(
        client_name="Rahul Sharma",
        pan="ABCDE1234F",
        source="outstanding_demand",
        notices=[{"source": "outstanding_demand", "din": "D1"}],
        checked_at=datetime(2026, 10, 7, 6, 0, tzinfo=timezone.utc),
        scheduled=False,
    )
    assert "An Income Tax portal check for Rahul Sharma" in text
    assert "monthly" not in text
    assert "monthly" not in html


def test_who_gets_emails() -> None:
    assert emails_allowed("cron")
    assert emails_allowed("8f0c2f4e-advisor-sub")
    assert emails_allowed(None)
    assert not emails_allowed("cron-silent")
    assert is_scheduled("cron") and is_scheduled("cron-silent")
    assert not is_scheduled("8f0c2f4e-advisor-sub")


def test_alert_keys_use_din_then_notice_details() -> None:
    keys = alert_keys(
        [
            {"din": "100120049489"},
            {"din": "100120049489"},
            {"source": "outstanding_demand", "notice_section": "143(1)(a)",
             "assessment_year": "2024-25", "date_of_demand_raised": "2025-01-10"},
        ]
    )
    assert keys == ["100120049489", "outstanding_demand|143(1)(a)|2024-25|2025-01-10"]


def test_already_alerted_only_when_nothing_is_new() -> None:
    assert already_alerted(["a"], {"a", "b"})
    assert not already_alerted(["a", "c"], {"a", "b"})
    assert not already_alerted([], {"a"})


def test_technical_failures_are_not_advisor_mail() -> None:
    for code in ("UI_DRIFT", "PORTAL_BLOCKED", "UNKNOWN", "CRYPTO_ERROR"):
        assert code not in ADVISOR_FIXABLE_ERRORS
