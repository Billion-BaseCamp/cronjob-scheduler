"""Pure-function checks for portal e-verification matching."""

import pytest

from app.portal.actions.read_filed_return_status import (
    ITR_NOT_FILED,
    is_e_verified,
    not_filed_outcome,
)


@pytest.mark.parametrize(
    ("filing_type", "labels", "expected"),
    [
        (
            "Original",
            ("Successfully e-verified",),
            True,
        ),
        (
            "Original",
            ("ITRV Received", "Pending for e-verification", "ITR Filed"),
            True,
        ),
        (
            "Original",
            ("ITR-V received",),
            True,
        ),
        (
            "Rectification",
            (
                "Rectification processed with no demand/refund",
                "Under Processing",
                "Rectification filed",
            ),
            True,
        ),
        (
            " Defective ",
            (),
            True,
        ),
        (
            "Original",
            ("Pending for e-verification", "ITR Filed"),
            False,
        ),
        (
            "Original",
            ("Under Processing", "Processed with refund due"),
            False,
        ),
        # Review cases: bare processed/processing must not promote.
        ("Original", ("Not processed",), False),
        ("Original", ("Return not processed yet",), False),
        ("Original", ("Pending for processing",), False),
        (
            "Original",
            ("e-Verification pending, processing not started",),
            False,
        ),
        ("Original", ("Processing failed",), False),
        ("Original", ("Return unprocessed",), False),
    ],
)
def test_is_e_verified(
    filing_type: str,
    labels: tuple[str, ...],
    expected: bool,
) -> None:
    assert is_e_verified(filing_type, labels) is expected


def test_not_filed_outcome_sets_error() -> None:
    code, message = not_filed_outcome(
        {
            "assessment_year": "2025-26",
            "e_verified": None,
            "itr_status": "No filing found for AY 2025-26",
        }
    ) or ("", "")
    assert code == ITR_NOT_FILED
    assert message == "No filing found for AY 2025-26"


@pytest.mark.parametrize(
    "result",
    [
        {"e_verified": False, "itr_status": "Pending for e-verification"},
        {"e_verified": True, "itr_status": "Successfully e-verified"},
        {"e_verified": None, "itr_status": "dry-run"},
        {"e_verified": None, "itr_status": "no page"},
    ],
)
def test_not_filed_outcome_ignores_other_results(result: dict) -> None:
    assert not_filed_outcome(result) is None
