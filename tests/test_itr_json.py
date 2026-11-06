"""Card walk and S3 key for the ITR JSON download."""

from datetime import datetime
from uuid import UUID

import pytest

from app.portal.itr_json import (
    FiledReturnCard,
    choose_itr_json_card,
    counts_by_filing_type,
    downloaded_filing_date,
    is_itr_json,
    itr_json_s3_key,
    latest_filing,
    parse_acknowledgement_no,
    parse_e_verified_on,
    parse_filing_section,
    parse_filing_type,
    parse_itr_form,
)


def _card(
    filing_type: str,
    day: int,
    *,
    has_download_json: bool,
    month: int = 7,
) -> FiledReturnCard:
    return FiledReturnCard(
        filing_type=filing_type,
        filing_date=datetime(2026, month, day),
        has_download_json=has_download_json,
    )


def test_walk_stops_at_the_revised_return_that_has_json() -> None:
    original = _card("Original", 29, has_download_json=True)
    revised_with_json = _card("Revised", 1, month=9, has_download_json=True)
    revised = _card("Revised", 1, month=10, has_download_json=False)
    rectification = _card("Rectification", 15, month=10, has_download_json=False)
    cards = [original, revised_with_json, revised, rectification]

    chosen = choose_itr_json_card(cards)

    assert chosen is revised_with_json
    assert latest_filing(cards) == ("Rectification", "2026-10-15")
    assert counts_by_filing_type(cards) == {
        "Original": 1,
        "Revised": 2,
        "Rectification": 1,
    }
    assert downloaded_filing_date(chosen) == "2026-09-01"


def test_rectification_with_json_is_downloaded() -> None:
    original = _card("Original", 29, has_download_json=True)
    rectification = _card("Rectification", 15, month=10, has_download_json=True)

    assert choose_itr_json_card([original, rectification]) is rectification
    assert latest_filing([original, rectification]) == ("Rectification", "2026-10-15")


def test_newest_revised_with_json_is_the_latest_filing() -> None:
    original = _card("Original", 29, has_download_json=True)
    revised = _card("Revised", 1, month=9, has_download_json=True)

    assert choose_itr_json_card([original, revised]) is revised
    assert latest_filing([original, revised]) == ("Revised", "2026-09-01")


def test_original_without_json_stops_the_walk() -> None:
    older = _card("Revised", 1, has_download_json=True)
    original = _card("Original", 29, month=8, has_download_json=False)

    assert choose_itr_json_card([older, original]) is None
    assert latest_filing([older, original]) == ("Original", "2026-08-29")


def test_no_older_card_when_the_only_rectification_has_no_json() -> None:
    only = _card("Rectification", 15, has_download_json=False)

    assert choose_itr_json_card([only]) is None
    assert latest_filing([only]) == ("Rectification", "2026-07-15")


_CARD = """
Filing Type
Original
ITR : ITR-2
Acknowledgement No : 585101650290726
Filing Date : Jul 29, 2026
Filing Section : 139(1)
Refund Credited to your Bank A/c
Oct 30, 2026
"""

_DETAILS = """
Successfully e-verified
Aug 6, 2026
Pending for e-verification
Jul 29, 2026
ITR Filed
Jul 29, 2026
"""


def test_card_text_supplies_the_saved_filing() -> None:
    assert parse_itr_form(_CARD) == "ITR-2"
    assert parse_acknowledgement_no(_CARD) == "585101650290726"
    assert parse_filing_section(_CARD) == "139(1)"
    assert parse_e_verified_on(_CARD) == ""
    assert parse_e_verified_on(_DETAILS) == "2026-08-06"


_PENDING = """
Pending for e-verification
Jul 29, 2026
ITR Filed
Jul 29, 2026
"""


def test_pending_e_verification_leaves_the_date_empty() -> None:
    assert parse_e_verified_on(_PENDING) == ""
    chosen = choose_itr_json_card([_card("Original", 29, has_download_json=True)])
    assert chosen is not None
    assert chosen.e_verified_on == ""


def test_filing_type_is_the_label_on_the_card() -> None:
    assert parse_filing_type("Filing Type\nOriginal\n") == "Original"
    assert parse_filing_type("Filing Type : Rectification") == "Rectification"
    assert parse_filing_type("Filing Section : 139(1)") == ""


def test_s3_key_is_one_object_per_client_and_year() -> None:
    client_id = UUID("11111111-1111-1111-1111-111111111111")
    assert itr_json_s3_key(client_id, " 2026-27 ") == (
        "portal-automation/json/11111111-1111-1111-1111-111111111111/2026-27/itr.json"
    )


@pytest.mark.parametrize("assessment_year", ["", "2026/27", "..", "2026-27/../secret"])
def test_s3_key_rejects_a_path(assessment_year: str) -> None:
    with pytest.raises(ValueError):
        itr_json_s3_key("client", assessment_year)


def test_itr_json_must_be_an_object() -> None:
    assert is_itr_json(b'\xef\xbb\xbf{"ITR": {"ITR2": {}}}')
    assert not is_itr_json(b"not json")
    assert not is_itr_json(b"[1]")
