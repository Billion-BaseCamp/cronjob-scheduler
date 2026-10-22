"""Foreign-assets password, dialog branch, and dry-run job result."""

from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.portal.actions.login_outcomes import LoginOutcome
from app.portal.foreign_assets import (
    calendar_years_from_job,
    classify_download_dialog,
    foreign_assets_password,
    foreign_assets_pdf_key,
    parse_portal_date,
)
from app.portal.workflows import fetch_foreign_assets_job as job_mod


def test_portal_date_and_password_match_the_portal_example() -> None:
    born = parse_portal_date(" 21-Jan-1991 ")
    assert born == date(1991, 1, 21)
    assert foreign_assets_password("AAAAA1234A", born) == "aaaaa1234a21011991"
    profile = parse_portal_date("02-Jan-1983")
    assert profile == date(1983, 1, 2)
    assert foreign_assets_password("AGSPA4774H", profile) == "agspa4774h02011983"


def test_portal_date_rejects_a_partial_value() -> None:
    assert parse_portal_date("Jan-1983") is None
    assert parse_portal_date("32-Jan-1983") is None
    assert parse_portal_date("") is None


def test_download_dialogs_branch_on_the_sentence() -> None:
    password = (
        "The downloaded PDF will be password protected. To open the file, "
        "you will need to enter the combination of the PAN (in lower case) "
        "and the date of birth. For example aaaaa1234a21011991."
    )
    assert classify_download_dialog(password) == "downloaded"
    assert classify_download_dialog("  No data available for the selected year ") == "no_data"
    assert classify_download_dialog("Session expired") == "unknown"


def test_pdf_key_and_year_list() -> None:
    from app.portal.foreign_assets import legacy_foreign_assets_pdf_key

    job_id = uuid4()
    client_id = uuid4()
    assert foreign_assets_pdf_key(client_id, 2024) == (
        f"portal-automation/foreign-assets/{client_id}/CY-2024.pdf"
    )
    assert legacy_foreign_assets_pdf_key(job_id, 2024) == (
        f"portal-automation/{job_id}/foreign-assets/CY-2024.pdf"
    )
    preferred = SimpleNamespace(
        params={"calendar_years": [2024]},
        result={"calendar_years": [1999]},
    )
    assert calendar_years_from_job(preferred) == [2024]
    fallback = SimpleNamespace(
        params=None,
        result={"calendar_years": ["2022", "2022", "2021"]},
    )
    assert calendar_years_from_job(fallback) == [2022, 2021]
    assert calendar_years_from_job(SimpleNamespace(params={"calendar_years": [True]}, result=None)) == []


def test_missing_years_fails_before_login() -> None:
    job = SimpleNamespace(
        id=uuid4(),
        client_id=uuid4(),
        params={},
        result=None,
        current_step=None,
        status="running",
        error_code=None,
        error_message=None,
        worker_id="worker",
        completed_at=None,
    )

    asyncio.run(
        job_mod.run_fetch_foreign_assets_job(
            None, job, workflow="FETCH_FOREIGN_ASSETS"
        )
    )
    assert job.status == "failed"
    assert job.error_code == "MISSING_CALENDAR_YEARS"


def test_dry_run_does_not_read_or_write_the_client_date(monkeypatch) -> None:
    client = SimpleNamespace(
        id=uuid4(),
        pan_number="AGSPA4774H",
        date_of_birth=date(1991, 1, 21),
    )
    job = SimpleNamespace(
        id=uuid4(),
        client_id=client.id,
        params={"calendar_years": [2024, 2023]},
        result=None,
        current_step=None,
        status="running",
        error_code="old",
        error_message="old",
        worker_id="worker",
        completed_at=None,
    )
    monkeypatch.setattr(job_mod.settings, "PORTAL_AUTOMATION_DRY_RUN", True)
    monkeypatch.setattr(
        job_mod, "run_login", AsyncMock(return_value=LoginOutcome.SUCCESS)
    )
    monkeypatch.setattr(job_mod, "get_client", AsyncMock(return_value=client))

    asyncio.run(
        job_mod.run_fetch_foreign_assets_job(
            None, job, workflow="FETCH_FOREIGN_ASSETS"
        )
    )

    assert job.status == "completed"
    assert job.result["dry_run"] is True
    assert "pdf_password" not in job.result
    assert "portal_date_of_birth" not in job.result
    assert job.result["years"] == [
        {"calendar_year": 2024, "status": "dry_run"},
        {"calendar_year": 2023, "status": "dry_run"},
    ]
    assert client.date_of_birth == date(1991, 1, 21)
