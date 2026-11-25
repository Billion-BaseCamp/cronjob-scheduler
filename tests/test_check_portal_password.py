"""CHECK_PORTAL_PASSWORD stops after login and logout."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("playwright")
pytest.importorskip("nucleus")

from app.portal.actions.login_outcomes import LoginOutcome
from app.portal.workflows.check_portal_password import run_check_portal_password


def _job():
    return SimpleNamespace(
        id=uuid4(),
        client_id=uuid4(),
        status="running",
        current_step=None,
        error_code="OLD",
        error_message="old",
        worker_id="worker",
        result=None,
        waiting_since=None,
        completed_at=None,
        attempt=1,
    )


def _patch_dry_run(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.portal.workflows.check_portal_password.settings.PORTAL_AUTOMATION_DRY_RUN",
        True,
    )


async def test_valid_password_completes_after_logout(monkeypatch) -> None:
    job = _job()
    client = SimpleNamespace(id=job.client_id, it_portal_pass="encrypted")
    logged_out = {"called": False}

    async def get_client(db, client_id):
        assert client_id == job.client_id
        return client

    async def run_login(found, page):
        assert found is client
        assert page is None
        return LoginOutcome.SUCCESS

    async def run_logout(page):
        logged_out["called"] = True
        assert page is None
        return True

    _patch_dry_run(monkeypatch)
    monkeypatch.setattr(
        "app.portal.workflows.check_portal_password.get_client", get_client
    )
    monkeypatch.setattr(
        "app.portal.workflows.check_portal_password.run_login", run_login
    )
    monkeypatch.setattr(
        "app.portal.workflows.check_portal_password.run_logout", run_logout
    )

    await run_check_portal_password(None, job)

    assert logged_out["called"] is True
    assert job.status == "completed"
    assert job.current_step == "LOGOUT"
    assert job.error_code is None
    assert job.error_message is None
    assert job.worker_id is None
    assert job.result["workflow"] == "CHECK_PORTAL_PASSWORD"
    assert job.result["password_valid"] is True
    assert job.result["login_ok"] is True
    assert job.result["logged_out"] is True
    assert job.result["dry_run"] is True
    assert client.it_portal_pass == "encrypted"


async def test_invalid_password_fails_and_clears_the_saved_password(monkeypatch) -> None:
    job = _job()
    client = SimpleNamespace(id=job.client_id, it_portal_pass="encrypted")

    async def get_client(db, client_id):
        return client

    async def run_login(found, page):
        return LoginOutcome.INVALID_PASSWORD

    async def run_logout(page):
        raise AssertionError("logout runs only after a valid password")

    _patch_dry_run(monkeypatch)
    monkeypatch.setattr(
        "app.portal.workflows.check_portal_password.get_client", get_client
    )
    monkeypatch.setattr(
        "app.portal.workflows.check_portal_password.run_login", run_login
    )
    monkeypatch.setattr(
        "app.portal.workflows.check_portal_password.run_logout", run_logout
    )

    await run_check_portal_password(None, job)

    assert client.it_portal_pass is None
    assert job.status == "failed"
    assert job.error_code == "INVALID_PASSWORD"
    assert job.current_step == "LOGIN"


async def test_missing_password_fails_without_waiting(monkeypatch) -> None:
    job = _job()
    client = SimpleNamespace(id=job.client_id, it_portal_pass=None)

    async def get_client(db, client_id):
        return client

    async def run_login(found, page):
        return LoginOutcome.MISSING_PASSWORD

    _patch_dry_run(monkeypatch)
    monkeypatch.setattr(
        "app.portal.workflows.check_portal_password.get_client", get_client
    )
    monkeypatch.setattr(
        "app.portal.workflows.check_portal_password.run_login", run_login
    )

    await run_check_portal_password(None, job)

    assert job.status == "failed"
    assert job.error_code == "MISSING_PASSWORD"
    assert job.waiting_since is None
    assert job.completed_at is not None
    assert client.it_portal_pass is None


@pytest.mark.parametrize(
    "outcome, error_code",
    [
        (LoginOutcome.CAPTCHA_REQUIRED, "CAPTCHA_REQUIRED"),
        (LoginOutcome.OTP_REQUIRED, "OTP_REQUIRED"),
        (LoginOutcome.VAULT_HIGHER_SECURITY, "VAULT_HIGHER_SECURITY"),
    ],
)
async def test_unconfirmed_login_fails_and_keeps_the_password(
    monkeypatch, outcome, error_code
) -> None:
    job = _job()
    client = SimpleNamespace(id=job.client_id, it_portal_pass="encrypted")

    async def get_client(db, client_id):
        return client

    async def run_login(found, page):
        return outcome

    async def run_logout(page):
        raise AssertionError("logout runs only after a valid password")

    _patch_dry_run(monkeypatch)
    monkeypatch.setattr(
        "app.portal.workflows.check_portal_password.get_client", get_client
    )
    monkeypatch.setattr(
        "app.portal.workflows.check_portal_password.run_login", run_login
    )
    monkeypatch.setattr(
        "app.portal.workflows.check_portal_password.run_logout", run_logout
    )

    await run_check_portal_password(None, job)

    assert job.status == "failed"
    assert job.error_code == error_code
    assert job.waiting_since is None
    assert job.completed_at is not None
    assert client.it_portal_pass == "encrypted"


async def test_missing_client_fails(monkeypatch) -> None:
    job = _job()

    async def get_client(db, client_id):
        return None

    _patch_dry_run(monkeypatch)
    monkeypatch.setattr(
        "app.portal.workflows.check_portal_password.get_client", get_client
    )

    await run_check_portal_password(None, job)

    assert job.status == "failed"
    assert job.error_code == "UNKNOWN"
    assert job.error_message == "Client not found"
