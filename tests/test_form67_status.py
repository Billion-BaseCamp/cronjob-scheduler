"""Pure-function checks for the Form 67 View Details page."""

from app.portal.actions.read_form67_status import (
    activity_result,
    activity_verdict,
    filing_date_in_text,
    is_successfully_e_verified,
    result_count,
)
from app.portal.actions.view_filed_forms import is_no_forms_filed_page

_FILED = """
Statement of income from a country or specified territory outside India and Foreign Tax Credit
Form 67
A.Y. 2026-27
Filing Date
21-Aug-2026
Filing Type
Original
Status :
Successfully e-Verified
Acknowledgement No :
590145180210826
Filed By :
SELF
Activity / Status
Form submitted
21-Aug-2026
"""

_OTHER_STATUS = """
Form 67
A.Y. 2026-27
Filing Date
21-Aug-2026
Filing Type
Original
Status :
Pending for e-verification
Acknowledgement No :
590145180210826
Filed By :
SELF
"""


def test_successfully_e_verified_ignores_spacing() -> None:
    assert is_successfully_e_verified(" Successfully e-Verified ")
    assert not is_successfully_e_verified("Pending for e-verification")


def test_filed_page_sets_true_and_keeps_ack() -> None:
    assert activity_verdict(_FILED, "2026-27") == "filed"
    result = activity_result(_FILED, "2026-27")
    assert result["form67_filed"] is True
    assert result["acknowledgement_no"] == "590145180210826"
    assert result["filing_date"] == "21-Aug-2026"
    assert result["filing_type"] == "Original"
    assert result["filed_by"] == "SELF"
    assert result["form67_status"] == "Successfully e-Verified"


def test_other_status_does_not_set_the_checkbox() -> None:
    assert activity_verdict(_OTHER_STATUS, "2026-27") == "not_e_verified"
    result = activity_result(_OTHER_STATUS, "2026-27")
    assert result["form67_filed"] is None
    assert result["form67_status"] == "Pending for e-verification"


def test_wrong_year_is_a_mismatch() -> None:
    assert activity_verdict(_FILED, "2025-26") == "mismatch"


def test_form_10e_page_is_not_form_67() -> None:
    text = """
    Form for furnishing particulars of income under section 192(2A)
    Form 10E
    A.Y. 2026-27
    Filing Date 31-Jul-2026
    Status : Successfully e-Verified
    Acknowledgement No : 975657320310726
    """
    assert activity_verdict(text, "2026-27") == "mismatch"


def test_no_forms_filed_page() -> None:
    assert is_no_forms_filed_page("View Filed Forms\nNo form filed till date")
    assert not is_no_forms_filed_page("Form 67\nView All")


def test_result_count_and_filing_date() -> None:
    assert result_count("1 result(s) found") == 1
    assert result_count("0 result(s) found") == 0
    assert result_count("no count here") is None
    parsed = filing_date_in_text("Filing Date 21-Aug-2026 Form submitted 01-Jan-2026")
    assert parsed is not None
    assert (parsed.year, parsed.month, parsed.day) == (2026, 8, 21)
