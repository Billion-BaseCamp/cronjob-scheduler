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
    assert mapped["notice_section"] == "142(1)"
    assert mapped["has_submit_response"] is True
    assert "response_state" not in mapped
    assert mapped["document_reference_id"] == "ITBA/AST/F/142(1)/2026-27/1093524726(1)"
    assert mapped["assessment_year"] == "2025-26"
    assert "scrape_path" not in mapped


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
    assert "response_state" not in mapped


def test_parse_document_reference_id_from_text() -> None:
    from app.portal.actions.read_e_proceedings_notices import (
        parse_document_reference_id,
    )

    text = (
        "142(1) ITBA/AST/F/142(1)/2026-27/1093524726(1) Document reference ID "
        "Description : [ITBA]Notice"
    )
    assert (
        parse_document_reference_id(text)
        == "ITBA/AST/F/142(1)/2026-27/1093524726(1)"
    )


def test_coerce_notice_section_fallbacks() -> None:
    from app.portal.actions.read_e_proceedings_notices import coerce_notice_section

    assert coerce_notice_section("271AAC(1)") == "271AAC(1)"
    assert coerce_notice_section("142 (1)") == "142(1)"
    assert (
        coerce_notice_section(
            None,
            "[ITBA]Show Cause Notice u/s 271AAC(1)of Income Tax Act 1961.",
        )
        == "271AAC(1)"
    )
    assert (
        coerce_notice_section(
            None,
            None,
            "ITBA/PNL/F/271AAC(1)/2025-26/1085010753(1)",
        )
        == "271AAC(1)"
    )
    # Prefer explicit section over description
    assert (
        coerce_notice_section(
            "142(1)",
            "[ITBA]Show Cause Notice u/s 271AAC(1)of Income Tax Act 1961.",
        )
        == "142(1)"
    )
    # Non-section ITBA path segment must not invent a value
    assert coerce_notice_section("ITBA/NFAC/F/APL_1/2026-27/1091979371(1)") is None


def test_section_re_matches_notice_u_s_values() -> None:
    from app.portal.actions.read_e_proceedings_notices import SECTION_RE

    for value in (
        "142(1)",
        "250",
        "143(3)",
        "139(9)",
        "271A",
        "271AAC(1)",
        "143(1)(a)",
        "148A",
        "226(3)",
        "271FAA",
    ):
        match = SECTION_RE.match(value)
        assert match is not None, value
        assert match.group(1) == value

    assert SECTION_RE.match("ITBA/AST/F/142(1)/2026-27/1093524726(1)") is None
    assert SECTION_RE.match("[ITBA]Show Cause Notice u/s 271AAC(1)") is None


def test_classify_notice_section_catalog() -> None:
    from app.portal.notice_sections import classify_notice_section

    row = classify_notice_section("271AAC(1)")
    assert row["category"] == "Penalties & Fees"
    assert "unexplained" in (row["short_name"] or "").lower()

    row = classify_notice_section("143(2)")
    assert row["short_name"] == "Scrutiny assessment notice"

    row = classify_notice_section("148A")
    assert "reopening" in (row["short_name"] or "").lower()

    row = classify_notice_section("999Z")
    assert row["short_name"] is None


def test_diagnose_notice_271aac_and_250() -> None:
    penalty = diagnose_notice(
        {
            "source": "e_proceedings",
            "notice_section": "271AAC(1)",
            "response_due_date": "2026-01-23",
            "has_submit_response": False,
        }
    )
    assert "271AAC(1)" in penalty
    assert "unexplained" in penalty.lower()
    assert "already responded" in penalty.lower()

    appeal = diagnose_notice(
        {
            "source": "e_proceedings",
            "notice_section": "250",
            "response_due_date": "2026-08-26",
            "has_submit_response": True,
        }
    )
    assert "250" in appeal
    assert "Appellate" in appeal
    assert "pending" in appeal.lower()


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
            "notice_section": "142(1)",
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
                "notice_section": "142(1)",
                "response_due_date": "2026-09-23",
                "has_submit_response": True,
            }
        ]
    )
    assert rows[0]["summary"]
