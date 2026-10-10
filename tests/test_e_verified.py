"""Pure-function checks for portal e-verification matching."""

import pytest

from app.portal.actions.read_filed_return_status import is_e_verified


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
