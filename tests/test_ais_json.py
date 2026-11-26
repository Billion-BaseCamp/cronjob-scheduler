"""AIS JSON helpers, S3 key, Gemini captcha solver, and workflow tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.portal.ais_json import (
    ais_json_filename,
    ais_json_s3_key,
    is_valid_ais_download,
    normalize_financial_year,
    sanitize_captcha_text,
)
from app.portal.captcha_solver import CaptchaSolveError, solve_captcha_with_gemini


def test_normalize_financial_year_accepts_common_formats() -> None:
    assert normalize_financial_year("2025-26") == "2025-26"
    assert normalize_financial_year("2025-2026") == "2025-26"
    assert normalize_financial_year("FY 2025-26") == "2025-26"
    assert normalize_financial_year("F.Y. 2026-27") == "2026-27"
    assert normalize_financial_year("25-26") == "2025-26"

    for bad in ("", "2025", "2025-28", "../2025-26", "2025/26/extra"):
        with pytest.raises(ValueError):
            normalize_financial_year(bad)


def test_ais_json_s3_key_matches_expected_layout() -> None:
    client_id = uuid4()
    assert ais_json_filename(" axkps8309k ", "2025-2026") == "AXKPS8309K_2025-26.json"
    assert ais_json_s3_key(client_id, "axkps8309k", "2025-26") == (
        f"portal-automation/ais-json/{client_id}/2025-26/AXKPS8309K_2025-26.json"
    )


def test_sanitize_captcha_text() -> None:
    assert sanitize_captcha_text(" K C I M 7 ") == "KCIM7"
    assert sanitize_captcha_text("`ab12CD`") == "ab12CD"
    assert sanitize_captcha_text("The captcha is XY98Z.") == "XY98Z"
    assert sanitize_captcha_text("I see KCIM7 in the image.") == "KCIM7"
    assert sanitize_captcha_text("The text shown is abCD.") == "abCD"
    assert sanitize_captcha_text("Captcha: 123456") == "123456"
    assert sanitize_captcha_text("I cannot read this image.") is None
    assert sanitize_captcha_text("") is None
    assert sanitize_captcha_text("...") is None


def test_is_valid_ais_download_accepts_encrypted_or_plain_ais() -> None:
    encrypted = ("0123456789abcdef" * 4 + "YWJjZGVmZ2g=" * 4).encode("ascii")
    assert is_valid_ais_download(encrypted) is True
    assert is_valid_ais_download(b'{"partB": {"sections": []}}') is True
    assert is_valid_ais_download(b"<html>Permission Denied</html>") is False
    assert is_valid_ais_download(b"{}") is False
    assert is_valid_ais_download(b"") is False


def test_solve_captcha_with_gemini_parses_candidate(monkeypatch) -> None:
    captured: dict = {}

    class _FakeResponse:
        status_code = 200

        def json(self):
            return {
                "candidates": [
                    {"content": {"parts": [{"text": " K C I M 7 \n"}]}}
                ]
            }

    def _fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr("app.portal.captcha_solver.requests.post", _fake_post)

    answer = asyncio.run(
        solve_captcha_with_gemini(
            b"\x89PNG\r\n\x1a\nfake",
            api_key="test-key",
            model="gemini-3.1-flash-lite",
            timeout_seconds=12.0,
        )
    )
    assert answer == "KCIM7"
    assert "gemini-3.1-flash-lite:generateContent" in captured["url"]
    assert captured["headers"]["x-goog-api-key"] == "test-key"
    assert captured["timeout"] == 12.0
    assert captured["json"]["generationConfig"]["thinkingConfig"] == {
        "thinkingBudget": 0
    }


def test_solve_captcha_with_gemini_rejects_missing_key_or_http_error(monkeypatch) -> None:
    with pytest.raises(CaptchaSolveError):
        asyncio.run(solve_captcha_with_gemini(b"png", api_key=""))

    fake_resp = MagicMock(status_code=503)
    monkeypatch.setattr(
        "app.portal.captcha_solver.requests.post", lambda *a, **k: fake_resp
    )
    with pytest.raises(CaptchaSolveError):
        asyncio.run(solve_captcha_with_gemini(b"png", api_key="test-key"))


def test_extract_reference_id() -> None:
    pytest.importorskip("playwright")
    from app.portal.actions.download_ais_json import extract_reference_id

    msg = (
        "Since the file is large, your download request has been submitted "
        "successfully (Reference ID: 1811202611132843) . The requested file "
        "will be available shortly under Activity History."
    )
    assert extract_reference_id(msg) == "1811202611132843"
    assert extract_reference_id("No reference here") is None


def test_download_ais_json_job_requires_valid_fy() -> None:
    pytest.importorskip("playwright")
    from app.portal.workflows import download_ais_json_job as job_mod

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
    asyncio.run(job_mod.run_download_ais_json_job(None, job, workflow="DOWNLOAD_AIS_JSON"))
    assert job.status == "failed"
    assert job.error_code == "MISSING_FINANCIAL_YEAR"


def test_download_ais_json_job_dry_run_completes(monkeypatch) -> None:
    pytest.importorskip("playwright")
    from app.portal.actions.login_outcomes import LoginOutcome
    from app.portal.workflows import download_ais_json_job as job_mod

    client = SimpleNamespace(id=uuid4(), pan_number="axkps8309k")
    job = SimpleNamespace(
        id=uuid4(),
        client_id=client.id,
        assessment_year="2025-2026",
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

    asyncio.run(job_mod.run_download_ais_json_job(None, job, workflow="DOWNLOAD_AIS_JSON"))
    assert job.status == "completed"
    assert job.result["dry_run"] is True
    assert job.result["financial_year"] == "2025-26"
    assert job.result["pan"] == "AXKPS8309K"
    assert job.result["ais_json_s3_key"] is None


def test_download_ais_json_job_uploads_s3_and_records_archive(monkeypatch) -> None:
    pytest.importorskip("playwright")
    from datetime import date
    from app.portal.actions.download_ais_json import AisJsonDownloadResult
    from app.portal.actions.login_outcomes import LoginOutcome
    from app.portal.actions.open_foreign_assets import AisPages
    from app.portal.actions.read_my_profile import ProfileDate
    from app.portal.workflows import download_ais_json_job as job_mod

    client_id = uuid4()
    itr_return_id = uuid4()
    client = SimpleNamespace(id=client_id, pan_number="axkps8309k")
    job = SimpleNamespace(
        id=uuid4(),
        client_id=client_id,
        itr_return_id=itr_return_id,
        assessment_year="2025-26",
        result=None,
        current_step=None,
        status="running",
        error_code=None,
        error_message=None,
        worker_id="worker",
        completed_at=None,
    )

    fake_page = MagicMock()
    fake_page.screenshot = AsyncMock(return_value=b"png")
    fake_ais_page = MagicMock()
    fake_ais_page.screenshot = AsyncMock(return_value=b"png")
    fake_browser = MagicMock()
    fake_browser.close = AsyncMock()
    fake_pw = MagicMock()
    fake_pw.stop = AsyncMock()

    monkeypatch.setattr(job_mod.settings, "PORTAL_AUTOMATION_DRY_RUN", False)
    monkeypatch.setattr(
        "playwright.async_api.async_playwright",
        lambda: SimpleNamespace(start=AsyncMock(return_value=fake_pw)),
    )
    monkeypatch.setattr(
        "app.portal.browser.launch_portal_page",
        AsyncMock(return_value=(fake_browser, fake_page)),
    )
    monkeypatch.setattr(job_mod, "get_client", AsyncMock(return_value=client))
    monkeypatch.setattr(job_mod, "run_login", AsyncMock(return_value=LoginOutcome.SUCCESS))
    monkeypatch.setattr(
        job_mod,
        "read_my_profile",
        AsyncMock(return_value=ProfileDate(pan="AXKPS8309K", born_on=date(1991, 1, 21))),
    )
    monkeypatch.setattr(
        job_mod,
        "open_ais_instructions",
        AsyncMock(return_value=AisPages(page=fake_ais_page)),
    )
    monkeypatch.setattr(
        job_mod,
        "download_ais_json",
        AsyncMock(
            return_value=AisJsonDownloadResult(
                status="downloaded",
                financial_year="2025-26",
                payload=b'{"partB":{"sections":[]}}',
                captcha_attempts=2,
            )
        ),
    )
    expected_key = f"portal-automation/ais-json/{client_id}/2025-26/AXKPS8309K_2025-26.json"
    upload_mock = AsyncMock(return_value=expected_key)
    archive_mock = AsyncMock(return_value=True)
    nested_events: list[str] = []

    class _Savepoint:
        async def __aenter__(self):
            nested_events.append("enter")
            return self

        async def __aexit__(self, exc_type, exc, tb):
            nested_events.append(f"exit:{exc_type.__name__ if exc_type else 'ok'}")
            return False

    fake_db = MagicMock()
    fake_db.begin_nested = MagicMock(return_value=_Savepoint())

    monkeypatch.setattr(job_mod, "upload_ais_json", upload_mock)
    monkeypatch.setattr(job_mod, "remember_ais_json_archive", archive_mock)
    monkeypatch.setattr(job_mod, "upload_screenshot", AsyncMock(return_value=None))
    monkeypatch.setattr(job_mod, "run_ais_logout", AsyncMock(return_value=True))
    monkeypatch.setattr(job_mod, "run_logout", AsyncMock(return_value=True))

    asyncio.run(job_mod.run_download_ais_json_job(fake_db, job, workflow="DOWNLOAD_AIS_JSON"))

    assert job.status == "completed"
    assert job.error_code is None
    assert job.result["ais_json_s3_key"] == expected_key
    assert job.result["archive_recorded"] is True
    assert nested_events == ["enter", "exit:ok"]
    assert job.result["financial_year"] == "2025-26"
    assert job.result["pan"] == "AXKPS8309K"
    assert job.result["captcha_attempts"] == 2
    assert job.result["portal_date_of_birth"] == "1991-01-21"
    assert isinstance(job.result.get("fetched_at"), str)
    assert job.result["ais_logged_out"] is True
    assert job.result["logged_out"] is True
    upload_mock.assert_awaited_once_with(
        client_id, "AXKPS8309K", "2025-26", b'{"partB":{"sections":[]}}'
    )
    archive_mock.assert_awaited_once_with(
        fake_db,
        client_id=client_id,
        itr_return_id=itr_return_id,
        s3_key=expected_key,
        original_filename="AXKPS8309K_2025-26.json",
        file_size_bytes=len(b'{"partB":{"sections":[]}}'),
    )


def test_download_ais_json_job_fails_on_captcha_failure(monkeypatch) -> None:
    pytest.importorskip("playwright")
    from app.portal.actions.download_ais_json import AisJsonDownloadResult
    from app.portal.actions.login_outcomes import LoginOutcome
    from app.portal.actions.open_foreign_assets import AisPages
    from app.portal.actions.read_my_profile import ProfileDate
    from app.portal.workflows import download_ais_json_job as job_mod

    client_id = uuid4()
    client = SimpleNamespace(id=client_id, pan_number="AXKPS8309K")
    job = SimpleNamespace(
        id=uuid4(),
        client_id=client_id,
        itr_return_id=None,
        assessment_year="2025-26",
        result=None,
        current_step=None,
        status="running",
        error_code=None,
        error_message=None,
        worker_id="worker",
        completed_at=None,
    )

    fake_page = MagicMock()
    fake_ais_page = MagicMock()
    fake_browser = MagicMock()
    fake_browser.close = AsyncMock()
    fake_pw = MagicMock()
    fake_pw.stop = AsyncMock()

    monkeypatch.setattr(job_mod.settings, "PORTAL_AUTOMATION_DRY_RUN", False)
    monkeypatch.setattr(
        "playwright.async_api.async_playwright",
        lambda: SimpleNamespace(start=AsyncMock(return_value=fake_pw)),
    )
    monkeypatch.setattr(
        "app.portal.browser.launch_portal_page",
        AsyncMock(return_value=(fake_browser, fake_page)),
    )
    monkeypatch.setattr(job_mod, "get_client", AsyncMock(return_value=client))
    monkeypatch.setattr(job_mod, "run_login", AsyncMock(return_value=LoginOutcome.SUCCESS))
    monkeypatch.setattr(
        job_mod,
        "read_my_profile",
        AsyncMock(return_value=ProfileDate(pan="AXKPS8309K", born_on=None)),
    )
    monkeypatch.setattr(
        job_mod,
        "open_ais_instructions",
        AsyncMock(return_value=AisPages(page=fake_ais_page)),
    )
    monkeypatch.setattr(
        job_mod,
        "download_ais_json",
        AsyncMock(
            return_value=AisJsonDownloadResult(
                status="failed",
                financial_year="2025-26",
                error_code="CAPTCHA_FAILED",
                error_message="Portal rejected the captcha answer. (4 attempt(s)).",
                captcha_attempts=4,
            )
        ),
    )
    monkeypatch.setattr(job_mod, "upload_screenshot", AsyncMock(return_value=None))
    monkeypatch.setattr(job_mod, "run_ais_logout", AsyncMock(return_value=True))
    monkeypatch.setattr(job_mod, "run_logout", AsyncMock(return_value=True))

    asyncio.run(job_mod.run_download_ais_json_job(None, job, workflow="DOWNLOAD_AIS_JSON"))

    assert job.status == "failed"
    assert job.error_code == "CAPTCHA_FAILED"
    assert job.result["captcha_attempts"] == 4
    assert job.result["ais_json_s3_key"] is None


def test_download_ais_json_job_completes_when_archive_upsert_fails(monkeypatch) -> None:
    pytest.importorskip("playwright")
    from datetime import date
    from app.portal.actions.download_ais_json import AisJsonDownloadResult
    from app.portal.actions.login_outcomes import LoginOutcome
    from app.portal.actions.open_foreign_assets import AisPages
    from app.portal.actions.read_my_profile import ProfileDate
    from app.portal.workflows import download_ais_json_job as job_mod

    client_id = uuid4()
    client = SimpleNamespace(id=client_id, pan_number="AXKPS8309K")
    job = SimpleNamespace(
        id=uuid4(),
        client_id=client_id,
        itr_return_id=None,
        assessment_year="2025-26",
        result=None,
        current_step=None,
        status="running",
        error_code=None,
        error_message=None,
        worker_id="worker",
        completed_at=None,
    )

    nested_events: list[str] = []

    class _FakeSavepoint:
        async def __aenter__(self):
            nested_events.append("enter")
            return self

        async def __aexit__(self, exc_type, exc, tb):
            nested_events.append(f"exit:{exc_type.__name__ if exc_type else 'ok'}")
            return False

    fake_db = MagicMock()
    fake_db.begin_nested = MagicMock(return_value=_FakeSavepoint())

    fake_page = MagicMock()
    fake_ais_page = MagicMock()
    fake_browser = MagicMock()
    fake_browser.close = AsyncMock()
    fake_pw = MagicMock()
    fake_pw.stop = AsyncMock()

    monkeypatch.setattr(job_mod.settings, "PORTAL_AUTOMATION_DRY_RUN", False)
    monkeypatch.setattr(
        "playwright.async_api.async_playwright",
        lambda: SimpleNamespace(start=AsyncMock(return_value=fake_pw)),
    )
    monkeypatch.setattr(
        "app.portal.browser.launch_portal_page",
        AsyncMock(return_value=(fake_browser, fake_page)),
    )
    monkeypatch.setattr(job_mod, "get_client", AsyncMock(return_value=client))
    monkeypatch.setattr(job_mod, "run_login", AsyncMock(return_value=LoginOutcome.SUCCESS))
    monkeypatch.setattr(
        job_mod,
        "read_my_profile",
        AsyncMock(return_value=ProfileDate(pan="AXKPS8309K", born_on=date(1991, 1, 21))),
    )
    monkeypatch.setattr(
        job_mod,
        "open_ais_instructions",
        AsyncMock(return_value=AisPages(page=fake_ais_page)),
    )
    monkeypatch.setattr(
        job_mod,
        "download_ais_json",
        AsyncMock(
            return_value=AisJsonDownloadResult(
                status="downloaded",
                financial_year="2025-26",
                payload=b'{"partB":{"sections":[]}}',
                captcha_attempts=1,
            )
        ),
    )
    expected_key = f"portal-automation/ais-json/{client_id}/2025-26/AXKPS8309K_2025-26.json"
    monkeypatch.setattr(job_mod, "upload_ais_json", AsyncMock(return_value=expected_key))
    monkeypatch.setattr(
        job_mod,
        "remember_ais_json_archive",
        AsyncMock(side_effect=RuntimeError("db lock timeout")),
    )
    monkeypatch.setattr(job_mod, "upload_screenshot", AsyncMock(return_value=None))
    monkeypatch.setattr(job_mod, "run_ais_logout", AsyncMock(return_value=True))
    monkeypatch.setattr(job_mod, "run_logout", AsyncMock(return_value=True))

    asyncio.run(job_mod.run_download_ais_json_job(fake_db, job, workflow="DOWNLOAD_AIS_JSON"))

    assert job.status == "failed"
    assert job.error_code == "ARCHIVE_RECORD_FAILED"
    assert job.result["ais_json_s3_key"] == expected_key
    assert job.result["archive_recorded"] is False
    assert nested_events == ["enter", "exit:RuntimeError"]


def test_download_ais_json_job_marks_archive_recorded_false_when_model_missing(
    monkeypatch,
) -> None:
    pytest.importorskip("playwright")
    from datetime import date
    from app.portal.actions.download_ais_json import AisJsonDownloadResult
    from app.portal.actions.login_outcomes import LoginOutcome
    from app.portal.actions.open_foreign_assets import AisPages
    from app.portal.actions.read_my_profile import ProfileDate
    from app.portal.workflows import download_ais_json_job as job_mod

    client_id = uuid4()
    client = SimpleNamespace(id=client_id, pan_number="AXKPS8309K")
    job = SimpleNamespace(
        id=uuid4(),
        client_id=client_id,
        itr_return_id=None,
        assessment_year="2025-26",
        result=None,
        current_step=None,
        status="running",
        error_code=None,
        error_message=None,
        worker_id="worker",
        completed_at=None,
    )

    class _Savepoint:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    fake_db = MagicMock()
    fake_db.begin_nested = MagicMock(return_value=_Savepoint())

    fake_page = MagicMock()
    fake_ais_page = MagicMock()
    fake_browser = MagicMock()
    fake_browser.close = AsyncMock()
    fake_pw = MagicMock()
    fake_pw.stop = AsyncMock()

    monkeypatch.setattr(job_mod.settings, "PORTAL_AUTOMATION_DRY_RUN", False)
    monkeypatch.setattr(
        "playwright.async_api.async_playwright",
        lambda: SimpleNamespace(start=AsyncMock(return_value=fake_pw)),
    )
    monkeypatch.setattr(
        "app.portal.browser.launch_portal_page",
        AsyncMock(return_value=(fake_browser, fake_page)),
    )
    monkeypatch.setattr(job_mod, "get_client", AsyncMock(return_value=client))
    monkeypatch.setattr(job_mod, "run_login", AsyncMock(return_value=LoginOutcome.SUCCESS))
    monkeypatch.setattr(
        job_mod,
        "read_my_profile",
        AsyncMock(return_value=ProfileDate(pan="AXKPS8309K", born_on=date(1991, 1, 21))),
    )
    monkeypatch.setattr(
        job_mod,
        "open_ais_instructions",
        AsyncMock(return_value=AisPages(page=fake_ais_page)),
    )
    monkeypatch.setattr(
        job_mod,
        "download_ais_json",
        AsyncMock(
            return_value=AisJsonDownloadResult(
                status="downloaded",
                financial_year="2025-26",
                payload=b'{"partB":{"sections":[]}}',
                captcha_attempts=1,
            )
        ),
    )
    expected_key = f"portal-automation/ais-json/{client_id}/2025-26/AXKPS8309K_2025-26.json"
    monkeypatch.setattr(job_mod, "upload_ais_json", AsyncMock(return_value=expected_key))
    monkeypatch.setattr(
        job_mod,
        "remember_ais_json_archive",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(job_mod, "upload_screenshot", AsyncMock(return_value=None))
    monkeypatch.setattr(job_mod, "run_ais_logout", AsyncMock(return_value=True))
    monkeypatch.setattr(job_mod, "run_logout", AsyncMock(return_value=True))

    asyncio.run(job_mod.run_download_ais_json_job(fake_db, job, workflow="DOWNLOAD_AIS_JSON"))

    assert job.status == "failed"
    assert job.error_code == "ARCHIVE_RECORD_FAILED"
    assert job.result["ais_json_s3_key"] == expected_key
    assert job.result["archive_recorded"] is False


def test_remember_ais_json_archive_updates_existing_itr_return_id() -> None:
    pytest.importorskip("sqlalchemy")
    from app.portal.ais_json_archive import remember_ais_json_archive

    client_id = uuid4()
    old_itr_id = uuid4()
    new_itr_id = uuid4()
    s3_key = f"portal-automation/ais-json/{client_id}/2025-26/AXKPS8309K_2025-26.json"
    existing = SimpleNamespace(
        id=uuid4(),
        client_id=client_id,
        itr_return_id=old_itr_id,
        s3_key=s3_key,
        original_filename="AXKPS8309K_2025-26.json",
        file_size_bytes=50,
        updated_at=None,
    )

    async def fake_execute(stmt, params=None):
        res = MagicMock()
        res.scalars.return_value.first.return_value = existing
        return res

    db = MagicMock()
    db.bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    db.execute = AsyncMock(side_effect=fake_execute)
    db.flush = AsyncMock()
    db.add = MagicMock()

    recorded = asyncio.run(
        remember_ais_json_archive(
            db,
            client_id=client_id,
            itr_return_id=new_itr_id,
            s3_key=s3_key,
            original_filename="AXKPS8309K_2025-26.json",
            file_size_bytes=123,
        )
    )

    assert recorded is True
    assert existing.itr_return_id == new_itr_id
    assert existing.file_size_bytes == 123
    db.add.assert_not_called()

    # When itr_return_id is None on a later run, preserve the already-linked id.
    asyncio.run(
        remember_ais_json_archive(
            db,
            client_id=client_id,
            itr_return_id=None,
            s3_key=s3_key,
            original_filename="AXKPS8309K_2025-26.json",
            file_size_bytes=124,
        )
    )
    assert existing.itr_return_id == new_itr_id


def test_real_async_session_begin_nested_returns_async_context_manager() -> None:
    pytest.importorskip("sqlalchemy")
    from sqlalchemy.ext.asyncio import AsyncSession, AsyncSessionTransaction

    session = AsyncSession()
    tx = session.begin_nested()
    assert isinstance(tx, AsyncSessionTransaction)
    assert hasattr(tx, "__aenter__") and hasattr(tx, "__aexit__")


def test_remember_ais_json_archive_acquires_advisory_lock() -> None:
    pytest.importorskip("sqlalchemy")
    from app.portal.ais_json_archive import remember_ais_json_archive

    client_id = uuid4()
    s3_key = f"portal-automation/ais-json/{client_id}/2025-26/AXKPS8309K_2025-26.json"
    executed: list[tuple[str, object]] = []

    async def fake_execute(stmt, params=None):
        executed.append((str(stmt), params))
        res = MagicMock()
        res.scalars.return_value.first.return_value = None
        return res

    db = MagicMock()
    db.bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    db.execute = AsyncMock(side_effect=fake_execute)
    db.flush = AsyncMock()
    db.add = MagicMock()

    asyncio.run(
        remember_ais_json_archive(
            db,
            client_id=client_id,
            itr_return_id=None,
            s3_key=s3_key,
            original_filename="AXKPS8309K_2025-26.json",
            file_size_bytes=123,
        )
    )

    assert len(executed) == 2
    assert "pg_advisory_xact_lock" in executed[0][0]
    assert executed[0][1] == {"lock_key": f"ais-archive:{client_id}:{s3_key}"}
    db.add.assert_called_once()
    db.flush.assert_awaited_once()


def test_worker_cooldown_applies_only_to_ais_jobs(monkeypatch) -> None:
    pytest.importorskip("sqlalchemy")
    from app.portal.worker import loop as worker_loop

    workflows = iter(["CHECK_ITR_VERIFICATION", "DOWNLOAD_AIS_JSON", None])
    timeouts: list[float] = []

    class _FakeSessionCtx:
        async def __aenter__(self):
            return MagicMock()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_process_one(db, worker_id: str):
        return next(workflows)

    async def fake_wait_for(awaitable, timeout):
        timeouts.append(timeout)
        if hasattr(awaitable, "close"):
            awaitable.close()
        if len(timeouts) >= 2:
            worker_loop._stop.set()
            return True
        raise asyncio.TimeoutError

    monkeypatch.setattr(worker_loop, "AsyncSessionLocal", lambda: _FakeSessionCtx())
    monkeypatch.setattr(worker_loop, "process_one", fake_process_one)
    monkeypatch.setattr(worker_loop.settings, "WORKER_JOB_COOLDOWN_SECONDS", 3.0)
    monkeypatch.setattr(worker_loop.settings, "WORKER_POLL_SECONDS", 2.0)
    monkeypatch.setattr(worker_loop.asyncio, "wait_for", fake_wait_for)

    worker_loop._stop.clear()
    try:
        asyncio.run(worker_loop._slot(0))
    finally:
        worker_loop._stop.clear()

    # CHECK_ITR_VERIFICATION did NOT sleep; DOWNLOAD_AIS_JSON slept 3.0s; empty queue slept 2.0s.
    assert timeouts == [3.0, 2.0]


def test_find_activity_download_icon_strict_ref_and_today_ist_fallback() -> None:
    pytest.importorskip("playwright")
    from datetime import datetime, timedelta, timezone
    from app.portal.actions.download_ais_json import (
        _find_activity_download_icon,
        _today_ist_date_regex,
    )

    class _FakeIcon:
        def __init__(self, visible: bool, tag: str) -> None:
            self._visible = visible
            self.tag = tag
            self.first = self

        async def count(self) -> int:
            return 1 if self._visible else 0

        async def is_visible(self) -> bool:
            return self._visible

    class _FakeRows:
        def __init__(self, items: list[tuple[str, bool, str]]) -> None:
            self._items = items

        def filter(self, *, has_text):
            matched = [it for it in self._items if has_text.search(it[0])]
            return _FakeRows(matched)

        async def count(self) -> int:
            return len(self._items)

        @property
        def first(self):
            text, visible, tag = self._items[0]
            return SimpleNamespace(locator=lambda _sel: _FakeIcon(visible, tag))

    ist_now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    today_str = ist_now.strftime("%d-%b-%Y")
    old_str = (ist_now - timedelta(days=3)).strftime("%d-%b-%Y")

    # Case 1: reference_id row exists but is still processing (no visible icon) -> must NOT fall back to old FY row!
    rows_processing = _FakeRows(
        [
            (f"{today_str} AIS Downloaded - JSON F.Y. 2025-26 REF999", False, "processing"),
            (f"{old_str} AIS Downloaded - JSON F.Y. 2025-26 REF111", True, "old"),
        ]
    )
    page_processing = SimpleNamespace(locator=lambda _sel: rows_processing)
    picked = asyncio.run(
        _find_activity_download_icon(
            page_processing, financial_year="2025-26", reference_id="REF999"
        )
    )
    assert picked is None

    # Case 2: reference_id=None -> skips 3-day-old row, matches only today's IST row.
    rows_no_ref = _FakeRows(
        [
            (f"{old_str} AIS Downloaded - JSON F.Y. 2025-26 OLD", True, "old"),
            (f"{today_str} AIS Downloaded - JSON F.Y. 2025-26 NEW", True, "today"),
        ]
    )
    page_no_ref = SimpleNamespace(locator=lambda _sel: rows_no_ref)
    picked_today = asyncio.run(
        _find_activity_download_icon(
            page_no_ref, financial_year="2025-26", reference_id=None
        )
    )
    assert picked_today is not None and picked_today.tag == "today"

    # Case 3: today regex matches DD-MMM-YYYY, DD/MM/YYYY, and YYYY-MM-DD.
    rx = _today_ist_date_regex()
    assert rx.search(ist_now.strftime("%d-%b-%Y"))
    assert rx.search(ist_now.strftime("%d/%m/%Y"))
    assert rx.search(ist_now.strftime("%Y-%m-%d"))
    assert not rx.search(old_str)

