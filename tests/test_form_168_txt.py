"""Unit tests for Form 168 / DOWNLOAD_26AS_TXT helpers and dry-run job."""

from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

pytest.importorskip("playwright")
pytest.importorskip("nucleus")
pytest.importorskip("boto3")
pytest.importorskip("cryptography")


def test_portal_and_db_year_helpers() -> None:
    from app.portal.form_168_txt import (
        DEFAULT_PORTAL_TAX_YEAR,
        db_financial_year,
        form_168_s3_key,
        form_168_txt_filename,
        portal_tax_year,
        sha256_hex,
    )

    assert portal_tax_year(None) == DEFAULT_PORTAL_TAX_YEAR
    assert portal_tax_year("2026-2027") == "2026-27"
    assert db_financial_year("2026-27") == "26-27"
    assert form_168_txt_filename("axkps8309k", "2026-27") == "AXKPS8309K-26-27.txt"

    client_id = uuid4()
    doc_id = uuid4()
    assert form_168_s3_key(client_id, "2026-27", 3, doc_id) == (
        f"advance-tax-26as/26-27/{client_id}/3/{doc_id}"
    )
    assert sha256_hex(b"abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_current_quarter_number_matches_fy_calendar() -> None:
    from app.portal.form_168_txt import current_quarter_number

    assert current_quarter_number(date(2026, 11, 27)) == 3
    assert current_quarter_number(date(2026, 4, 1)) == 1
    assert current_quarter_number(date(2027, 2, 1)) == 4


def test_txt_validation_and_extractors() -> None:
    from app.portal.form_168_txt import (
        extract_fy_from_txt,
        extract_pan_from_txt,
        is_valid_form_168_txt,
    )

    assert not is_valid_form_168_txt(b"short")
    assert not is_valid_form_168_txt(b"<!DOCTYPE html>" + b"x" * 40)
    body = (
        b"Header line\n"
        b"PAN: AXKPS8309K\n"
        b"Financial Year: 2026-27\n"
        + b"row data " * 10
    )
    assert is_valid_form_168_txt(body)
    assert extract_pan_from_txt(body) == "AXKPS8309K"
    assert extract_fy_from_txt(body) == "2026-27"


def test_form_168_menu_label_requires_act_2025() -> None:
    from app.portal.actions.open_traces_form168 import _is_form_168_act_2025_label

    assert _is_form_168_act_2025_label("View Form 168, Income Tax Act 2025")
    assert _is_form_168_act_2025_label("Form 168, Income Tax Act 2025")
    assert not _is_form_168_act_2025_label("View Form 26AS, Income Tax Act 1961")
    assert not _is_form_168_act_2025_label("View Form 26AS")
    assert not _is_form_168_act_2025_label("Annual Tax Statement")
    assert not _is_form_168_act_2025_label("Form 168")


def test_download_26as_txt_registered() -> None:
    from app.portal.workflows.registry import get_runner

    assert get_runner("DOWNLOAD_26AS_TXT") is not None


def test_download_26as_txt_job_requires_valid_year() -> None:
    from app.portal.workflows import download_26as_txt_job as job_mod

    job = SimpleNamespace(
        id=uuid4(),
        client_id=uuid4(),
        assessment_year="bad-year",
        result=None,
        current_step=None,
        status="running",
        error_code=None,
        error_message=None,
        worker_id="worker",
        completed_at=None,
    )
    asyncio.run(job_mod.run_download_26as_txt_job(None, job, workflow="DOWNLOAD_26AS_TXT"))
    assert job.status == "failed"
    assert job.error_code == "MISSING_TAX_YEAR"


def test_download_26as_txt_job_dry_run_completes(monkeypatch) -> None:
    from app.portal.actions.login_outcomes import LoginOutcome
    from app.portal.workflows import download_26as_txt_job as job_mod

    client = SimpleNamespace(id=uuid4(), pan_number="axkps8309k")
    job = SimpleNamespace(
        id=uuid4(),
        client_id=client.id,
        assessment_year=None,
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
    monkeypatch.setattr(job_mod, "run_logout", AsyncMock(return_value=True))
    monkeypatch.setattr(job_mod, "get_client", AsyncMock(return_value=client))

    asyncio.run(job_mod.run_download_26as_txt_job(None, job, workflow="DOWNLOAD_26AS_TXT"))
    assert job.status == "completed"
    assert job.result["dry_run"] is True
    assert job.result["tax_year"] == "2026-27"
    assert job.result["financial_year"] == "26-27"
    assert job.result["pan"] == "AXKPS8309K"
    assert job.result["s3_key"] is None


def test_download_26as_txt_job_stores_file_without_parsing(monkeypatch) -> None:
    from app.portal.actions.download_form168_txt import Form168TxtResult
    from app.portal.actions.login_outcomes import LoginOutcome
    from app.portal.actions.open_traces_form168 import TracesPages
    from app.portal.actions.read_my_profile import ProfileDate
    from app.portal.workflows import download_26as_txt_job as job_mod

    client_id = uuid4()
    client = SimpleNamespace(id=client_id, pan_number="AXKPS8309K")
    job = SimpleNamespace(
        id=uuid4(),
        client_id=client_id,
        assessment_year="2026-27",
        result=None,
        current_step=None,
        status="running",
        error_code=None,
        error_message=None,
        worker_id="worker",
        completed_at=None,
    )
    raw = (
        b"PAN: AXKPS8309K\nFinancial Year: 2026-27\n"
        + b"deductor row " * 20
    )
    upload_id = uuid4()
    quarter_id = uuid4()
    expected_key = f"advance-tax-26as/26-27/{client_id}/3/{upload_id}"

    fake_upload = SimpleNamespace(id=upload_id, quarter_id=quarter_id)
    fake_quarter = SimpleNamespace(id=quarter_id, quarter_number=3)

    class _Nested:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    fake_db = SimpleNamespace(begin_nested=lambda: _Nested())

    monkeypatch.setattr(job_mod.settings, "PORTAL_AUTOMATION_DRY_RUN", False)
    monkeypatch.setattr(job_mod.settings, "S3_BUCKET_NAME", "test-bucket")
    monkeypatch.setattr(job_mod, "get_client", AsyncMock(return_value=client))
    monkeypatch.setattr(
        job_mod, "run_login", AsyncMock(return_value=LoginOutcome.SUCCESS)
    )
    monkeypatch.setattr(
        job_mod,
        "read_my_profile",
        AsyncMock(return_value=ProfileDate(pan="AXKPS8309K", born_on=None)),
    )
    fake_page = SimpleNamespace(url="https://traces.example/")
    monkeypatch.setattr(
        job_mod,
        "open_traces_ats",
        AsyncMock(return_value=TracesPages(page=fake_page)),
    )
    monkeypatch.setattr(
        job_mod,
        "download_form168_txt",
        AsyncMock(
            return_value=Form168TxtResult(
                status="downloaded", tax_year="2026-27", payload=raw
            )
        ),
    )
    monkeypatch.setattr(job_mod, "upload_screenshot", AsyncMock(return_value=None))
    monkeypatch.setattr(
        job_mod, "upload_form_168_txt", AsyncMock(return_value=expected_key)
    )
    monkeypatch.setattr(
        job_mod,
        "_commit_form_168_upload_record",
        AsyncMock(return_value=fake_upload),
    )
    monkeypatch.setattr(
        job_mod, "_existing_upload_for_quarter", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        "app.portal.form_168_upload.resolve_fy_and_current_quarter",
        AsyncMock(return_value=(SimpleNamespace(id=uuid4()), fake_quarter)),
    )
    monkeypatch.setattr(job_mod, "current_quarter_number", lambda: 3)
    monkeypatch.setattr(
        job_mod, "form_168_s3_key", lambda *a, **k: expected_key
    )
    monkeypatch.setattr(job_mod, "uuid4", lambda: upload_id)
    monkeypatch.setattr(job_mod, "run_traces_logout", AsyncMock(return_value=True))
    monkeypatch.setattr(job_mod, "run_logout", AsyncMock(return_value=True))

    class _FakePlaywright:
        async def start(self):
            return self

        async def stop(self):
            return None

    class _FakeBrowser:
        async def close(self):
            return None

    async def _launch(_pw):
        return _FakeBrowser(), fake_page

    monkeypatch.setattr(
        "playwright.async_api.async_playwright",
        lambda: _FakePlaywright(),
    )
    monkeypatch.setattr("app.portal.browser.launch_portal_page", _launch)

    asyncio.run(
        job_mod.run_download_26as_txt_job(fake_db, job, workflow="DOWNLOAD_26AS_TXT")
    )

    assert job.status == "completed"
    assert job.result["status"] == "downloaded"
    assert job.result["s3_key"] == expected_key
    assert job.result["upload_id"] == str(upload_id)
    assert job.result["quarter_id"] == str(quarter_id)
    assert job.result["quarter_number"] == 3
    assert job.result["parse_status"] == "pending"
    assert job.result["content_hash"]
    assert job.result["traces_logged_out"] is True
    assert job.result["logged_out"] is True
    job_mod._commit_form_168_upload_record.assert_awaited()
    kwargs = job_mod._commit_form_168_upload_record.await_args.kwargs
    assert kwargs["content_hash"] == job.result["content_hash"]
    assert len(kwargs["content_hash"]) == 64
