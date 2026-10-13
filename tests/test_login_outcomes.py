from __future__ import annotations

from app.portal.actions.login_outcomes import LoginOutcome, classify_login_page

# Visible text of the live "Verify your account" page (no taxpayer data on it).
_VAULT_PAGE = """
* Indicates mandatory fields
Verify your account
Since you have opted for e-Filing Vault Higher Security, login using any of
the below options.
OTP on mobile number registered with Aadhaar
Through Net banking
Continue
Back
"""


def test_vault_higher_security_page_is_not_unknown() -> None:
    outcome = classify_login_page(
        page_text=_VAULT_PAGE,
        url="https://eportal.incometax.gov.in/iec/foservices/#/login",
        still_on_login=True,
        login_step="password",
    )
    assert outcome == LoginOutcome.VAULT_HIGHER_SECURITY


def test_vault_page_wins_over_otp_option_text() -> None:
    outcome = classify_login_page(
        page_text=_VAULT_PAGE + " enter otp",
        still_on_login=True,
        login_step="password",
    )
    assert outcome == LoginOutcome.VAULT_HIGHER_SECURITY


def test_unrecognised_login_page_is_still_unknown() -> None:
    outcome = classify_login_page(
        page_text="Something went wrong",
        url="https://eportal.incometax.gov.in/iec/foservices/#/login/password",
        still_on_login=True,
        login_step="password",
    )
    assert outcome == LoginOutcome.UNKNOWN
