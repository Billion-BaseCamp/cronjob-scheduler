"""Unit tests for e-proceedings notice parse helpers (no browser)."""

from __future__ import annotations

from datetime import date

from app.portal.actions.read_e_proceedings_notices import (
    ay_display,
    epoch_ms_to_date,
    map_api_notice,
    notices_from_api_payload,
    parse_labeled_field_from_text,
    parse_ui_date,
    pick_latest_notice,
)
from app.portal.notice_diagnosis import attach_summaries, diagnose_notice


def test_parse_ui_date() -> None:
    assert parse_ui_date("16-Sep-2026") == date(2026, 9, 16)
    assert parse_ui_date("Issued On 11-Aug-2026") == date(2026, 8, 11)
    assert parse_ui_date("") is None


def test_parse_description_from_card_text() -> None:
    card_text = (
        "Description :  [ITBA]Notice u/s 142(1)of Income Tax Act 1961. "
        "Issued On :   16-Sep-2026 "
        "Response Due Date :  23-Sep-2026 "
        "Last Response submitted On : - "
    )
    assert (
        parse_labeled_field_from_text(card_text, "Description")
        == "[ITBA]Notice u/s 142(1)of Income Tax Act 1961."
    )
    assert parse_labeled_field_from_text(card_text, "Issued On") == "16-Sep-2026"
    assert (
        parse_labeled_field_from_text(card_text, "Response Due Date") == "23-Sep-2026"
    )


def test_epoch_ms_to_date() -> None:
    # 2026-09-16 roughly (from live portal sample issuedOn)
    assert epoch_ms_to_date(1789559356000) == date(2026, 9, 16)


def test_ay_display() -> None:
    assert ay_display(2025) == "2025-26"
    assert ay_display("2025") == "2025-26"
    assert ay_display("2025-26") == "2025-26"


def test_pick_latest_notice_only() -> None:
    notices = [
        {
            "din": "100117983504",
            "issued_on": "2026-08-11",
            "has_submit_response": True,
        },
        {
            "din": "100120049489",
            "issued_on": "2026-09-16",
            "has_submit_response": True,
        },
        {
            "din": "100115041416",
            "issued_on": "2026-06-20",
            "has_submit_response": False,
        },
        {
            "din": "100115018794",
            "issued_on": "2026-06-20",
            "has_submit_response": True,
        },
    ]
    latest = pick_latest_notice(notices)
    assert latest is not None
    assert latest["din"] == "100120049489"


def test_map_api_notice_actionable() -> None:
    raw = {
        "documentIdentificationNumber": "100120049489",
        "noticeSection": "142(1)",
        "description": "[ITBA]Notice u/s 142(1)",
        "issuedOn": 1789559356000,
        "responseDueDate": 1790149620000,
        "documentReferenceId": "ITBA/AST/F/142(1)/2026-27/1093524726(1)",
        "ay": 2025,
        "proceedingReqId": "61989968",
        "proceedingName": "Assessment Proceeding u/s 143(3)",
        "isSubmitted": "N",
    }
    mapped = map_api_notice(raw)
    assert mapped["din"] == "100120049489"
    assert mapped["filing_provision"] == "142(1)"
    assert mapped["has_submit_response"] is True
    assert mapped["response_state"] == "submit_response"
    assert mapped["assessment_year"] == "2025-26"
    assert mapped["scrape_path"] == "api"


def test_map_api_notice_responded() -> None:
    mapped = map_api_notice(
        {
            "documentIdentificationNumber": "100115041416",
            "noticeSection": "142(1)",
            "issuedOn": 1782000000000,
            "responseDueDate": 1783000000000,
            "ay": 2025,
            "isSubmitted": "Y",
        }
    )
    assert mapped["has_submit_response"] is False
    assert mapped["response_state"] == "view_response"


def test_notices_from_api_payload_detects_din_list() -> None:
    payload = {
        "header": {},
        "messages": [],
        "items": [
            {
                "documentIdentificationNumber": "100120049489",
                "noticeSection": "142(1)",
                "issuedOn": 1789559356000,
                "responseDueDate": 1790149620000,
                "ay": 2025,
                "isSubmitted": "N",
            },
            {
                "documentIdentificationNumber": "100117983504",
                "noticeSection": "142(1)",
                "issuedOn": 1786445707000,
                "responseDueDate": 1787635800000,
                "ay": 2025,
                "isSubmitted": "N",
            },
        ],
    }
    rows = notices_from_api_payload(payload)
    assert len(rows) == 2
    latest = pick_latest_notice(rows)
    assert latest is not None
    assert latest["din"] == "100120049489"


def test_diagnose_notice_142_actionable() -> None:
    summary = diagnose_notice(
        {
            "source": "e_proceedings",
            "filing_provision": "142(1)",
            "response_due_date": "2026-09-23",
            "has_submit_response": True,
        }
    )
    assert "142(1)" in summary
    assert "2026" in summary or "Sep" in summary
    assert "pending" in summary.lower() or "Critical" in summary


def test_attach_summaries() -> None:
    rows = attach_summaries(
        [
            {
                "source": "e_proceedings",
                "din": "1",
                "filing_provision": "142(1)",
                "response_due_date": "2026-09-23",
                "has_submit_response": True,
            }
        ]
    )
    assert rows[0]["summary"]
