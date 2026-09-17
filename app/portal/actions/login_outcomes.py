"""Turn login-page text into a LoginOutcome. No browser, no network."""

from __future__ import annotations

from enum import Enum


class LoginOutcome(str, Enum):
    SUCCESS = "success"
    INVALID_PASSWORD = "invalid_password"
    INVALID_USER_ID = "invalid_user_id"
    MISSING_PASSWORD = "missing_password"
    CAPTCHA_REQUIRED = "captcha_required"
    OTP_REQUIRED = "otp_required"
    PORTAL_BLOCKED = "portal_blocked"
    NOT_AUTHENTICATED = "not_authenticated"
    DUAL_LOGIN = "dual_login"
    UI_DRIFT = "ui_drift"
    UNKNOWN = "unknown"


def _has(text: str, phrases: tuple[str, ...]) -> bool:
    return any(phrase in text for phrase in phrases)


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


# Pin the password-field mat-error from the live portal.
ASSURED_INVALID_PASSWORD = "invalid password, please retry"

_OTP = (
    "enter otp",
    "one time password",
    "otp has been sent",
    "enter the otp",
    "resend otp",
)
_BLOCKED = ("permission denied", "access denied")
_NOT_AUTHENTICATED = (
    "request is not authenticated",
    "ef500023",
)
_DUAL_LOGIN = (
    "dual login detected",
    "currently active in another window",
    "session already active",
    "ef00177",
)


def is_assured_invalid_password(*, page_text: str, url: str = "") -> bool:
    """True only on the password screen with the portal's password mat-error."""
    if "/login/password" not in (url or "").lower():
        return False
    return ASSURED_INVALID_PASSWORD in _norm(page_text)


def classify_login_page(
    *,
    page_text: str,
    url: str = "",
    has_captcha_widget: bool = False,
    has_otp_widget: bool = False,
    still_on_login: bool = False,
    login_step: str | None = None,
) -> LoginOutcome:
    text = _norm(page_text)
    url_l = (url or "").lower()
    step = (login_step or "").strip().lower()
    on_password_page = "/login/password" in url_l or step == "password"
    on_pan_step = step == "pan" or (
        still_on_login and "login" in url_l and not on_password_page
    )

    if _has(text, _BLOCKED):
        return LoginOutcome.PORTAL_BLOCKED
    if _has(text, _NOT_AUTHENTICATED):
        return LoginOutcome.NOT_AUTHENTICATED
    if _has(text, _DUAL_LOGIN):
        return LoginOutcome.DUAL_LOGIN
    if is_assured_invalid_password(page_text=page_text, url=url) or (
        on_password_page and ASSURED_INVALID_PASSWORD in text
    ):
        return LoginOutcome.INVALID_PASSWORD
    if has_otp_widget or _has(text, _OTP):
        return LoginOutcome.OTP_REQUIRED
    if has_captcha_widget or "enter captcha" in text:
        return LoginOutcome.CAPTCHA_REQUIRED
    if on_pan_step and still_on_login:
        return LoginOutcome.INVALID_USER_ID
    if still_on_login and "login" in url_l:
        return LoginOutcome.UNKNOWN
    return LoginOutcome.SUCCESS
