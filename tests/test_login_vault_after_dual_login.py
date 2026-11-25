from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from playwright.async_api import TimeoutError as PlaywrightTimeout

from app.portal.actions import login
from app.portal.actions.login_outcomes import LoginOutcome
from app.portal.workflows.filed_return_status_job import _apply_login_outcome

_VAULT_TEXT = (
    "Verify your account Since you have opted for e-Filing Vault Higher "
    "Security, login using any of the below options."
)


class _Loc:
    def __init__(self, visible: bool) -> None:
        self.visible = visible
        self.clicked = False

    async def wait_for(self, *, state: str, timeout: int) -> None:
        if not self.visible:
            raise PlaywrightTimeout("not visible")

    async def click(self, timeout: int = 0) -> None:
        self.clicked = True


class _HangLoc:
    async def wait_for(self, *, state: str, timeout: int) -> None:
        await asyncio.sleep(timeout / 1000)
        raise PlaywrightTimeout("not visible")


class _ErrorLoc:
    def __init__(self, text: str) -> None:
        self.text = text

    async def is_visible(self, timeout: int = 0) -> bool:
        return True

    async def inner_text(self) -> str:
        return self.text


class _Page:
    url = "https://portal.example/#/login/password"

    def __init__(self, body: str = "") -> None:
        self.body = body

    async def inner_text(self, selector: str) -> str:
        return self.body


def _run(coro):
    return asyncio.run(coro)


def test_vault_page_after_login_here_is_not_dual_login() -> None:
    outcome = _run(login._wait_logged_in(_Page(), _Loc(False), _Loc(True)))
    assert outcome == LoginOutcome.VAULT_HIGHER_SECURITY


def test_vault_text_after_login_here_timeout_is_not_dual_login() -> None:
    outcome = _run(
        login._wait_logged_in(_Page(_VAULT_TEXT), _Loc(False), _Loc(False))
    )
    assert outcome == LoginOutcome.VAULT_HIGHER_SECURITY


def test_login_here_timeout_without_vault_is_still_dual_login() -> None:
    outcome = _run(
        login._wait_logged_in(_Page("Dashboard loading"), _Loc(False), _Loc(False))
    )
    assert outcome == LoginOutcome.DUAL_LOGIN


def test_expected_dual_login_that_shows_vault_instead(monkeypatch) -> None:
    login_here = _Loc(False)
    locators = {
        login.LOGGED_IN_SELECTOR: _Loc(False),
        login.VAULT_HIGHER_SECURITY_SELECTOR: _Loc(True),
    }
    page = _Page()
    page.locator = lambda selector: SimpleNamespace(first=locators[selector])
    monkeypatch.setattr(login, "_login_here_button", lambda _page: login_here)

    outcome = _run(login._confirm_logged_in(page, expect_dual_login=True))

    assert outcome == LoginOutcome.VAULT_HIGHER_SECURITY
    assert login_here.clicked is False


def test_rejected_password_stops_without_the_login_wait(monkeypatch) -> None:
    hang = _HangLoc()
    locators = {
        login.LOGGED_IN_SELECTOR: hang,
        login.VAULT_HIGHER_SECURITY_SELECTOR: hang,
        login.PASSWORD_MAT_ERROR_SELECTOR: _ErrorLoc(
            "Error : Invalid Password, Please retry."
        ),
    }
    page = _Page()
    page.locator = lambda selector: SimpleNamespace(first=locators[selector])
    monkeypatch.setattr(login, "_login_here_button", lambda _page: hang)

    started = time.monotonic()
    outcome = _run(login._confirm_logged_in(page))
    elapsed = time.monotonic() - started

    assert outcome == LoginOutcome.INVALID_PASSWORD
    assert elapsed < 5


def test_logged_in_page_still_wins_over_another_password_message(monkeypatch) -> None:
    class _Shown:
        async def wait_for(self, *, state: str, timeout: int) -> None:
            return None

    hang = _HangLoc()
    locators = {
        login.LOGGED_IN_SELECTOR: _Shown(),
        login.VAULT_HIGHER_SECURITY_SELECTOR: hang,
        login.PASSWORD_MAT_ERROR_SELECTOR: _ErrorLoc(
            "Please confirm your secure access message"
        ),
    }
    page = _Page()
    page.locator = lambda selector: SimpleNamespace(first=locators[selector])
    monkeypatch.setattr(login, "_login_here_button", lambda _page: hang)

    outcome = _run(login._confirm_logged_in(page))

    assert outcome == LoginOutcome.SUCCESS


def test_filed_return_status_vault_keeps_password() -> None:
    job = SimpleNamespace(
        status="running", error_code=None, error_message=None,
        worker_id="w1", completed_at=None,
    )
    client = SimpleNamespace(it_portal_pass="encrypted")

    assert _apply_login_outcome(job, client, LoginOutcome.VAULT_HIGHER_SECURITY) is False

    assert client.it_portal_pass == "encrypted"
    assert job.status == "failed"
    assert job.error_code == "VAULT_HIGHER_SECURITY"
