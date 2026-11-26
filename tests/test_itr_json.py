"""Card walk and S3 key for the ITR JSON download."""

from datetime import datetime
from uuid import UUID

import pytest

from app.portal.itr_json import (
    assessment_years_from_job,
    FiledReturnCard,
    choose_itr_json_card,
    counts_by_filing_type,
    download_button_label,
    downloaded_filing_date,
    is_itr_json,
    is_itr_xml,
    itr_file_format,
    itr_file_s3_key,
    itr_json_s3_key,
    itr_xml_s3_key,
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


def test_newest_defective_card_is_the_file() -> None:
    """Defective is not a walk-back type. The newer XML card is the download."""
    older = _card("Defective", 21, month=1, has_download_json=True)
    newer = _card("Defective", 4, month=2, has_download_json=True)

    assert choose_itr_json_card([older, newer]) is newer
    assert latest_filing([older, newer]) == ("Defective", "2026-02-04")


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
    assert itr_xml_s3_key(client_id, "2016-17") == (
        "portal-automation/xml/11111111-1111-1111-1111-111111111111/2016-17/itr.xml"
    )
    assert itr_file_s3_key(client_id, "2020-21") == (
        "portal-automation/xml/11111111-1111-1111-1111-111111111111/2020-21/itr.xml"
    )
    assert itr_file_s3_key(client_id, "2021-22") == (
        "portal-automation/json/11111111-1111-1111-1111-111111111111/2021-22/itr.json"
    )


@pytest.mark.parametrize(
    ("assessment_year", "file_format"),
    [("2021-22", "json"), ("2026-27", "json"), ("2020-21", "xml"), ("2016-17", "xml")],
)
def test_assessment_year_picks_the_file_format(assessment_year: str, file_format: str) -> None:
    assert itr_file_format(assessment_year) == file_format
    label = "Download XML" if file_format == "xml" else "Download JSON"
    assert download_button_label(file_format) == label


def test_itr_xml_is_xml_and_not_json_or_html() -> None:
    assert is_itr_xml(b"<ITRETURN><ITR4>ok</ITR4></ITRETURN>")
    assert not is_itr_xml(b'{"ITR": {"ITR2": {}}}')
    assert not is_itr_xml(b"<html><body>error</body></html>")
    assert not is_itr_xml(b"not xml")


@pytest.mark.parametrize("assessment_year", ["", "2026/27", "..", "2026-27/../secret"])
def test_s3_key_rejects_a_path(assessment_year: str) -> None:
    with pytest.raises(ValueError):
        itr_json_s3_key("client", assessment_year)


def test_job_years_come_from_the_list_or_the_single_column() -> None:
    class _Job:
        def __init__(self, assessment_year: str, result: object) -> None:
            self.assessment_year = assessment_year
            self.result = result

    assert assessment_years_from_job(
        _Job("2026-27", {"assessment_years": ["2024-25", "2026-27", "2024-25"]})
    ) == ["2024-25", "2026-27"]
    assert assessment_years_from_job(_Job("2021-22", None)) == ["2021-22"]


def test_itr_json_must_be_an_object() -> None:
    assert is_itr_json(b'\xef\xbb\xbf{"ITR": {"ITR2": {}}}')
    assert not is_itr_json(b"not json")
    assert not is_itr_json(b"[1]")


_TIMELINE_CARD = """
<app-itr-status>
  <mat-card class="contextBox">
    <mat-label class="contentHeadingText">A.Y. 2021-22</mat-label>
    <div>
      <mat-label>Filing Type</mat-label>
      <mat-label class="leftSideVal">Original</mat-label>
    </div>
    <div class="matStepStatus">Refund Credited to your Bank A/c</div>
    <div class="matStepDate">Feb 17, 2024</div>
    <div class="matStepStatus">Rectification processed with refund due</div>
    <div class="matStepDate">Feb 5, 2024</div>
    <div class="valueBox">
      <mat-label class="rightsideLabel">ITR :</mat-label>
      <mat-label class="fieldVal">ITR-2</mat-label>
    </div>
    <div class="valueBox">
      <mat-label class="rightsideLabel">Acknowledgement No :</mat-label>
      <mat-label class="fieldVal">787204650311221</mat-label>
    </div>
    <div class="valueBox">
      <mat-label class="rightsideLabel">Filing Date :</mat-label>
      <mat-label class="fieldVal">Dec 31, 2021</mat-label>
    </div>
    <div class="valueBox">
      <mat-label class="rightsideLabel">Filing Section :</mat-label>
      <mat-label class="fieldVal">139(1)</mat-label>
    </div>
    <button type="button" class="downloadButton">Download JSON</button>
  </mat-card>
</app-itr-status>
"""


async def test_filing_date_is_the_labeled_box_not_the_timeline() -> None:
    """The stepper's newest date is a processing step, not the filing date."""
    playwright_api = pytest.importorskip("playwright.async_api")
    from app.portal.actions.download_itr_json import _snapshot_page

    async with playwright_api.async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch()
        except Exception as exc:
            pytest.skip(f"Chromium unavailable: {exc}")
        page = await browser.new_page()
        try:
            await page.set_content(_TIMELINE_CARD)
            cards = await _snapshot_page(page, "2021-22", 0, "json")
        finally:
            await browser.close()

    assert len(cards) == 1
    card = cards[0]
    assert card.filing_date == datetime(2021, 12, 31)
    assert card.filing_type == "Original"
    assert card.itr_form == "ITR-2"
    assert card.acknowledgement_no == "787204650311221"
    assert card.filing_section == "139(1)"
    assert card.e_verified_on == ""
    assert card.has_download_json is True
