"""RM outstanding-demand reports: two mails, each with a workbook."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from io import BytesIO

from openpyxl import load_workbook

from app.portal.notice_alerts import (
    DEMAND_REPORT_ACTION,
    DEMAND_REPORT_RESPONDED,
    outstanding_demand_report_filename,
    render_outstanding_demand_report,
)
from app.portal.outstanding_demand_report import (
    AdvisorReports,
    ReportClient,
    bucket_demands,
    deliver_demand_reports,
    demand_workbook,
    should_send_demand_reports,
)

_CHECKED = datetime(2026, 11, 27, 18, 10, tzinfo=timezone.utc)


def _action(din: str, amount: int) -> dict:
    return {
        "source": "outstanding_demand",
        "is_extinguished": False,
        "actions": {"submit_response": True},
        "assessment_year": "2024-25",
        "notice_section": "143(1)",
        "din": din,
        "date_of_demand_raised": "2026-08-12",
        "outstanding_demand_amount": amount,
        "accrued_interest": 1000,
        "current_status": "PENDING PAYMENT",
    }


def _responded(din: str, *, extinguished: bool = False) -> dict:
    return {
        "source": "outstanding_demand",
        "is_extinguished": extinguished,
        "actions": {"submit_response": False, "resubmit_response": not extinguished},
        "assessment_year": "2023-24",
        "notice_section": "154",
        "din": din,
        "date_of_demand_raised": "2026-03-11",
        "outstanding_demand_amount": 0 if extinguished else 125000,
        "current_status": "EXTINGUISHED DEMAND" if extinguished else "PENDING PAYMENT",
        "response_type": "Demand paid" if extinguished else "Disagree with demand",
        "ao_response": "Accepted" if extinguished else "Response under process",
    }


def test_only_a_finished_monthly_run_sends_reports() -> None:
    assert should_send_demand_reports("outstanding_demand_monthly", "cron")
    assert not should_send_demand_reports("outstanding_demand_monthly", "cron-silent")
    assert not should_send_demand_reports("e_proceedings_monthly", "cron")
    assert not should_send_demand_reports("e_proceedings_weekly", "cron")


def test_demands_split_into_action_and_response() -> None:
    action, responded = bucket_demands(
        [
            _action("SMALL", 5000),
            _action("BIG", 125000),
            _responded("FILED"),
            _responded("CLOSED", extinguished=True),
            {"source": "outstanding_demand", "din": "UNREAD", "actions": None},
        ]
    )
    assert [row["din"] for row in action] == ["BIG", "SMALL"]
    assert [row["din"] for row in responded] == ["FILED", "CLOSED"]


def test_report_names_every_client_and_hides_the_full_pan() -> None:
    filename = outstanding_demand_report_filename(DEMAND_REPORT_ACTION, _CHECKED)
    subject, text, html = render_outstanding_demand_report(
        kind=DEMAND_REPORT_ACTION,
        advisor_first_name="Priya",
        clients=[
            {"name": "Rahul Sharma", "pan": "ABCDE1234F", "notices": [_action("D1", 4190703)]},
            {"name": "Anita Mehta", "pan": "FGHIJ5678K", "notices": [_action("D2", 86400)]},
        ],
        filename=filename,
        checked_at=_CHECKED,
    )
    assert subject == "Outstanding Demand Report - Action Required"
    assert filename == "Outstanding-Demand-Action-Required-27-Nov-2026.xlsx"
    assert "Hi Priya," in text
    assert "2 of your clients" in text
    assert "Rahul Sharma" in text and "Anita Mehta" in text
    assert "ABCDE****F" in text
    assert "ABCDE1234F" not in text
    assert filename in text
    assert "27-Nov-2026" in text
    assert ">Client</th>" in html


def test_workbook_keeps_the_full_pan_and_one_row_per_demand() -> None:
    content = demand_workbook(
        [
            {
                "name": "Rahul Sharma",
                "pan": "abcde1234f",
                "notices": [_action("D1", 4190703), _responded("D2")],
            }
        ]
    )
    sheet = load_workbook(BytesIO(content)).active
    headers = [cell.value for cell in sheet[1]]
    assert headers[:6] == ["Client", "PAN", "AY", "Section", "DIN", "Raised on"]
    assert [sheet.cell(row, 2).value for row in (2, 3)] == ["ABCDE1234F", "ABCDE1234F"]
    assert sheet.cell(2, 5).value == "D1"
    assert sheet.cell(2, 7).value == 4190703
    assert sheet.cell(3, 10).value == "Disagree with demand"
    assert sheet.freeze_panes == "A2"
    assert sheet.auto_filter.ref.startswith("A1:")


def test_each_rm_gets_only_the_reports_that_have_rows() -> None:
    sent: list[tuple] = []

    async def send(to_email, subject, text, html, attachment):
        sent.append((to_email, subject, text, attachment[0], attachment[2]))
        return True

    priya = AdvisorReports(
        email="priya@example.com",
        first_name="Priya",
        clients=[
            ReportClient("Rahul Sharma", "ABCDE1234F", (_action("D1", 100),), ()),
            ReportClient("Anita Mehta", "FGHIJ5678K", (), (_responded("D2"),)),
        ],
    )
    amit = AdvisorReports(
        email="amit@example.com",
        first_name="Amit",
        clients=[ReportClient("Vikram Iyer", "KLMNO9012P", (), (_responded("D3"),))],
    )
    counts = asyncio.run(
        deliver_demand_reports([priya, amit], checked_at=_CHECKED, send=send)
    )
    assert counts == {"sent_action": 1, "sent_responded": 2, "failed": 0}
    by_subject = {(row[0], row[1]): row for row in sent}
    action = by_subject[("priya@example.com", "Outstanding Demand Report - Action Required")]
    responded = by_subject[("priya@example.com", "Outstanding Demand Report - Response Submitted")]
    assert "Rahul Sharma" in action[2] and "Anita Mehta" not in action[2]
    assert "Anita Mehta" in responded[2] and "Rahul Sharma" not in responded[2]
    assert action[3].endswith(".xlsx")
    sheet = load_workbook(BytesIO(action[4])).active
    assert sheet.cell(2, 5).value == "D1"
    assert ("amit@example.com", "Outstanding Demand Report - Action Required") not in by_subject
    assert by_subject[("amit@example.com", "Outstanding Demand Report - Response Submitted")]


def test_responded_report_subject() -> None:
    subject, text, _html = render_outstanding_demand_report(
        kind=DEMAND_REPORT_RESPONDED,
        advisor_first_name=None,
        clients=[{"name": "Vikram Iyer", "pan": "KLMNO9012P", "notices": [_responded("D3")]}],
        filename="Outstanding-Demand-Response-Submitted-27-Nov-2026.xlsx",
        checked_at=_CHECKED,
    )
    assert subject == "Outstanding Demand Report - Response Submitted"
    assert text.startswith("Hi,\n\n")
    assert "response is already filed for 1 of your clients" in text
    assert "Disagree with demand" in text
