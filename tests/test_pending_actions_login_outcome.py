from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.portal.actions.login_outcomes import LoginOutcome
from app.portal.notice_alerts import CRON_REQUESTED_BY, CRON_SILENT_REQUESTED_BY
from app.portal.workflows.pending_actions_notices_job import _apply_login_outcome


def _job(requested_by_sub):
    return SimpleNamespace(
        requested_by_sub=requested_by_sub,
        status="running",
        error_code=None,
        error_message=None,
        worker_id="w1",
        completed_at=None,
    )


@pytest.mark.parametrize("sub", [CRON_REQUESTED_BY, CRON_SILENT_REQUESTED_BY])
def test_scheduled_check_keeps_the_saved_password(sub) -> None:
    job = _job(sub)
    client = SimpleNamespace(it_portal_pass="encrypted")

    assert _apply_login_outcome(job, client, LoginOutcome.INVALID_PASSWORD) is False

    assert client.it_portal_pass == "encrypted"
    assert job.status == "failed"
    assert job.error_code == "INVALID_PASSWORD"
    assert "kept" in job.error_message


def test_hand_run_check_clears_the_rejected_password() -> None:
    job = _job("advisor-sub")
    client = SimpleNamespace(it_portal_pass="encrypted")

    assert _apply_login_outcome(job, client, LoginOutcome.INVALID_PASSWORD) is False

    assert client.it_portal_pass is None
    assert job.status == "failed"
    assert job.error_code == "INVALID_PASSWORD"


def test_vault_higher_security_fails_with_its_own_code() -> None:
    job = _job(CRON_REQUESTED_BY)
    client = SimpleNamespace(it_portal_pass="encrypted")

    assert (
        _apply_login_outcome(job, client, LoginOutcome.VAULT_HIGHER_SECURITY)
        is False
    )

    assert client.it_portal_pass == "encrypted"
    assert job.status == "failed"
    assert job.error_code == "VAULT_HIGHER_SECURITY"
