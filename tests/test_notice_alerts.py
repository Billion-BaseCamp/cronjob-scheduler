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


_CHECKED = datetime(2026, 10, 8, 6, 0, tzinfo=timezone.utc)


def _proceeding(din: str, due: str | None, **extra) -> dict:
    return {
        "source": "e_proceedings",
        "has_submit_response": True,
        "proceeding_name": "Assessment Proceedings",
        "assessment_year": "2024-25",
        "notice_section": "142(1)",
        "din": din,
        "issued_on": "2026-09-20",
        "response_due_date": due,
        **extra,
    }


def _demand(din: str, amount: float, **extra) -> dict:
    return {
        "source": "outstanding_demand",
        "is_extinguished": False,
        "actions": {"submit_response": True},
        "assessment_year": "2023-24",
        "notice_section": "143(1)(a)",
        "din": din,
        "date_of_demand_raised": "2025-06-05",
        "outstanding_demand_amount": amount,
        "accrued_interest": 8400,
        "current_status": "Demand confirmed",
        **extra,
    }


def _rows(html: str) -> list[str]:
    return html.split("<tbody>", 1)[1].split("</tbody>", 1)[0].split("</tr>")[:-1]


def test_e_proceedings_email_is_a_table_with_most_urgent_first() -> None:
    _subject, text, html = render_action_required_email(
        client_name="Test Client",
        pan="ABCDE1234F",
        source="e_proceedings",
        notices=[
            _proceeding("DIN-LATER", "2026-10-30", description="Explain cash deposits"),
            _proceeding("DIN-NODATE", None, summary="Pending Submit Response"),
            _proceeding("DIN-OVERDUE", "2026-10-05", description="Bank account details"),
            _proceeding("DIN-SOON", "2026-10-10"),
        ],
        checked_at=_CHECKED,
    )
    for header in ("Proceeding", "AY", "Section", "DIN", "Issued on", "Response due", "Details"):
        assert f">{header}</th>" in html
    assert "<ul>" not in html

    rows = _rows(html)
    assert [r.split("DIN-")[1].split("<")[0] for r in rows] == [
        "OVERDUE", "SOON", "LATER", "NODATE",
    ]
    assert "overdue 3 days" in rows[0] and "color:#b91c1c" in rows[0]
    assert "2 days left" in rows[1] and "color:#b45309" in rows[1]
    assert "color:#" not in rows[2] and "color:#" not in rows[3]
    assert "20-Sep-2026" in rows[0]
    assert ">Bank account details</td>" in rows[0]
    assert ">Pending Submit Response</td>" in rows[3]
    assert ">Submit Response pending</td>" in rows[1]

    assert text.index("DIN-OVERDUE") < text.index("DIN-LATER")
    assert "Details: Explain cash deposits" in text


def test_long_details_are_clipped_and_escaped() -> None:
    _subject, text, html = render_action_required_email(
        client_name="Test Client",
        pan=None,
        source="e_proceedings",
        notices=[_proceeding("D1", "2026-10-30", description="<script>x</script> " + "a" * 300)],
        checked_at=_CHECKED,
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    details = text.split("Details: ", 1)[1].splitlines()[0]
    assert len(details) == 160 and details.endswith("…")


def test_outstanding_demand_email_table_largest_first() -> None:
    _subject, text, html = render_action_required_email(
        client_name="Test Client",
        pan="ABCDE1234F",
        source="outstanding_demand",
        notices=[
            _demand("SMALL", 5000),
            _demand(
                "BIG",
                125000,
                rectification_rights="Assessing Officer",
                applicable_act="Income Tax Act, 1961",
                ao_response="Demand upheld",
            ),
        ],
        checked_at=_CHECKED,
    )
    for header in (
        "AY", "Section", "Demand ref (DIN)", "Raised on", "Outstanding",
        "Interest", "Status", "Details",
    ):
        assert f">{header}</th>" in html

    big, small = _rows(html)
    assert ">BIG</td>" in big and ">SMALL</td>" in small
    assert "₹1,25,000" in big and "₹8,400" in big
    assert "05-Jun-2025" in big
    assert ">Demand confirmed</td>" in big
    assert (
        ">Rectification: Assessing Officer · Act: Income Tax Act, 1961 · AO: Demand upheld</td>"
        in big
    )
    assert ">No response filed</td>" in small

    assert text.index("DIN BIG") < text.index("DIN SMALL")
    assert "Status: Demand confirmed" in text
    assert "Details: No response filed" in text


def test_missing_values_show_a_dash() -> None:
    _subject, _text, html = render_action_required_email(
        client_name="Test Client",
        pan=None,
        source="outstanding_demand",
        notices=[{"source": "outstanding_demand", "din": "D1"}],
        checked_at=_CHECKED,
    )
    (row,) = _rows(html)
    assert row.count(">—</td>") == 6


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
