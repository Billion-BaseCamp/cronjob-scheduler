"""Unit tests for e-proceedings notice parse helpers (no browser)."""

from __future__ import annotations

from datetime import date

import pytest

from app.portal.actions.read_e_proceedings_notices import (
    ay_display,
    epoch_ms_to_date,
    map_api_notice,
    notice_in_window,
    notice_window_start,
    notices_from_api_payload,
    parse_for_your_action_count,
    parse_labeled_field_from_text,
    parse_response_due,
    parse_total_pages,
    parse_ui_date,
    pick_latest_notice,
    proceedings_gap,
)
from app.portal.notice_diagnosis import (
    attach_summaries,
    diagnose_notice,
    notice_needs_action,
)


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


# Card text as on the portal; the due-date row is left out when there is no due date.
_NO_RESPONSE_LETTER_CARD = (
    "Notice/ Communication Reference ID : 900000000001 "
    "Notice u/s ITBA/AST/F/17/2026-27/9000000001(1) Document reference ID "
    "Description :  [ITBA]No Response Letter "
    "Issued On :  05-Mar-2026 "
    "Submit Response Notice/Letter pdf Seek/View Adjournment"
)
_RESPONDED_ISSUE_LETTER_CARD = (
    "Notice/ Communication Reference ID : 900000000002 "
    "Description :  [ITBA]Issue Letter "
    "Issued On :  22-Sep-2026 "
    "Response viewed by AO on :  26-Sep-2026 "
    "View Response"
)


def test_no_response_letter_description_from_card_text() -> None:
    assert (
        parse_labeled_field_from_text(_NO_RESPONSE_LETTER_CARD, "Description")
        == "[ITBA]No Response Letter"
    )


def test_card_without_due_date_row_has_no_due_date() -> None:
    assert parse_response_due(_NO_RESPONSE_LETTER_CARD, None) == (None, False)


def test_later_dates_on_card_are_not_the_due_date() -> None:
    assert parse_response_due(_RESPONDED_ISSUE_LETTER_CARD, None) == (None, False)


def test_due_date_from_label_or_card_text() -> None:
    card = "Issued On :  09-Oct-2026 Response Due Date :  16-Oct-2026 "
    assert parse_response_due(card, " 16-Oct-2026 ") == (date(2026, 10, 16), False)
    assert parse_response_due(card, None) == (date(2026, 10, 16), False)


def test_due_date_label_without_a_readable_date_is_unreadable() -> None:
    assert parse_response_due("Response Due Date :  soon ", None) == (None, True)
    assert parse_response_due("", "16/10/26") == (None, True)


def test_dash_due_date_is_no_due_date() -> None:
    card = (
        "Issued On :  22-Sep-2026 "
        "Response Due Date :  - "
        "Response viewed by AO on :  26-Sep-2026 "
        "View Response"
    )
    assert parse_labeled_field_from_text(card, "Response Due Date") is None
    assert parse_response_due(card, None) == (None, False)
    assert parse_response_due(card, "-") == (None, False)
    assert parse_response_due(card, " — ") == (None, False)


def test_map_api_notice_flags_unreadable_due_date() -> None:
    raw = {
        "documentIdentificationNumber": "900000000003",
        "issuedOn": 1789559356000,
        "responseDueDate": "not a date",
        "isSubmitted": "N",
    }
    mapped = map_api_notice(raw)
    assert mapped["response_due_date"] is None
    assert mapped["response_due_date_unreadable"] is True
    assert notice_needs_action(mapped) is True


def test_map_api_notice_without_due_date_is_not_action() -> None:
    mapped = map_api_notice(
        {
            "documentIdentificationNumber": "900000000004",
            "description": "[ITBA]Issue Letter",
            "issuedOn": 1789559356000,
            "isSubmitted": "N",
        }
    )
    assert mapped["has_submit_response"] is True
    assert "response_due_date_unreadable" not in mapped
    assert notice_needs_action(mapped) is False


def test_map_api_notice_dash_due_date_is_not_action() -> None:
    mapped = map_api_notice(
        {
            "documentIdentificationNumber": "900000000005",
            "description": "[ITBA]Issue Letter",
            "issuedOn": 1789559356000,
            "responseDueDate": "-",
            "isSubmitted": "N",
        }
    )
    assert mapped["response_due_date"] is None
    assert "response_due_date_unreadable" not in mapped
    assert notice_needs_action(mapped) is False


def test_latest_notice_with_due_date_keeps_proceeding_actionable() -> None:
    notices = [
        {
            "source": "e_proceedings",
            "din": "900000000011",
            "description": "[ITBA]Notice u/s 142(1)of Income Tax Act 1961.",
            "issued_on": "2026-10-09",
            "response_due_date": "2026-10-16",
            "has_submit_response": True,
        },
        {
            "source": "e_proceedings",
            "din": "900000000012",
            "description": "[ITBA]Issue Letter",
            "issued_on": "2026-09-22",
            "response_due_date": None,
            "has_submit_response": False,
        },
        {
            "source": "e_proceedings",
            "din": "900000000013",
            "description": "[ITBA]Intimation and Notice u/s 143(2) of the Income-tax Act, 1961.",
            "issued_on": "2026-06-20",
            "response_due_date": None,
            "has_submit_response": True,
        },
    ]
    latest = pick_latest_notice(notices)
    assert latest is not None and latest["din"] == "900000000011"
    assert notice_needs_action(latest) is True
    assert notice_needs_action(notices[2]) is False


def test_diagnose_submit_response_without_due_date() -> None:
    summary = diagnose_notice(
        {
            "source": "e_proceedings",
            "notice_section": "17",
            "description": "[ITBA]Issue Letter",
            "has_submit_response": True,
        }
    )
    assert "no response due date" in summary
    assert "not counted" in summary


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


def test_pick_latest_notice_uses_issued_date_not_din() -> None:
    notices = [
        {"din": "900000000099", "issued_on": "2026-06-20"},
        {"din": "900000000001", "issued_on": "2026-10-09"},
        {"din": "900000000050", "issued_on": "05-Mar-2026"},
    ]
    latest = pick_latest_notice(notices)
    assert latest is not None and latest["din"] == "900000000001"


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


def test_parse_total_pages() -> None:
    assert parse_total_pages(" 1 of 1 pages ") == 1
    assert parse_total_pages("2 of 3 pages") == 3
    assert parse_total_pages("1 of 1 page") == 1
    assert parse_total_pages("") is None
    assert parse_total_pages("Items per Page") is None


def test_parse_for_your_action_count() -> None:
    assert parse_for_your_action_count("For your Action (8) ") == 8
    assert parse_for_your_action_count("For your Action ( 12 )") == 12
    assert parse_for_your_action_count("For your Action (0)") == 0
    assert parse_for_your_action_count("For your Action") is None
    assert parse_for_your_action_count("For your Information (3)") is None


def test_proceedings_gap_complete_list() -> None:
    assert proceedings_gap(found=8, expected=8, total_pages=1) is None
    assert proceedings_gap(found=8, expected=None, total_pages=1) is None
    assert proceedings_gap(found=8, expected=8, total_pages=None) is None


def test_proceedings_gap_more_than_one_page() -> None:
    gap = proceedings_gap(found=50, expected=60, total_pages=2)
    assert gap is not None
    assert "2 pages" in gap


def test_proceedings_gap_count_mismatch() -> None:
    gap = proceedings_gap(found=10, expected=12, total_pages=1)
    assert gap == "read 10 of 12 proceedings"


def test_proceedings_gap_unconfirmed_count() -> None:
    gap = proceedings_gap(found=5, expected=None, total_pages=None)
    assert gap is not None
    assert "confirm" in gap


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


@pytest.mark.parametrize(
    ("today", "start"),
    [
        (date(2026, 10, 14), date(2026, 5, 1)),
        (date(2026, 10, 1), date(2026, 5, 1)),
        (date(2026, 10, 31), date(2026, 5, 1)),
        (date(2027, 1, 5), date(2026, 8, 1)),
        (date(2026, 5, 20), date(2025, 12, 1)),
        (date(2026, 6, 1), date(2026, 1, 1)),
    ],
)
def test_notice_window_start(today: date, start: date) -> None:
    assert notice_window_start(today) == start


TODAY = date(2026, 10, 14)


@pytest.mark.parametrize(
    ("issued_on", "response_due_date", "kept"),
    [
        ("2026-05-01", None, True),
        ("2026-10-14", None, True),
        ("09-Sep-2026", None, True),
        ("2026-04-30", None, False),
        ("2026-03-15", "2026-11-20", True),
        ("2026-03-15", "2026-10-14", True),
        ("2026-03-15", "2026-10-13", False),
        ("2026-03-15", "2026-04-10", False),
        (None, None, True),
        ("-", None, True),
    ],
)
def test_notice_in_window(
    issued_on: str | None, response_due_date: str | None, kept: bool
) -> None:
    notice = {
        "din": "1",
        "issued_on": issued_on,
        "response_due_date": response_due_date,
    }
    assert notice_in_window(notice, TODAY) is kept


def test_old_notice_with_unreadable_due_date_is_dropped() -> None:
    notice = {
        "din": "1",
        "issued_on": "2026-03-15",
        "response_due_date": None,
        "response_due_date_unreadable": True,
    }
    assert notice_in_window(notice, TODAY) is False
