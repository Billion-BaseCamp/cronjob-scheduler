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
            model="gemini-2.5-flash",
            timeout_seconds=12.0,
        )
    )
    assert answer == "KCIM7"
    assert "gemini-2.5-flash:generateContent" in captured["url"]
    assert captured["headers"]["x-goog-api-key"] == "test-key"
    assert captured["timeout"] == 12.0


def test_solve_captcha_with_gemini_rejects_missing_key_or_http_error(monkeypatch) -> None:
    with pytest.raises(CaptchaSolveError):
        asyncio.run(solve_captcha_with_gemini(b"png", api_key=""))

    fake_resp = MagicMock(status_code=503)
    monkeypatch.setattr(
        "app.portal.captcha_solver.requests.post", lambda *a, **k: fake_resp
    )
    with pytest.raises(CaptchaSolveError):
        asyncio.run(solve_captcha_with_gemini(b"png", api_key="test-key"))


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
    archive_mock = AsyncMock()
    monkeypatch.setattr(job_mod, "upload_ais_json", upload_mock)
    monkeypatch.setattr(job_mod, "remember_ais_json_archive", archive_mock)
    monkeypatch.setattr(job_mod, "upload_screenshot", AsyncMock(return_value=None))
    monkeypatch.setattr(job_mod, "run_ais_logout", AsyncMock(return_value=True))
    monkeypatch.setattr(job_mod, "run_logout", AsyncMock(return_value=True))

    asyncio.run(job_mod.run_download_ais_json_job(None, job, workflow="DOWNLOAD_AIS_JSON"))

    assert job.status == "completed"
    assert job.error_code is None
    assert job.result["ais_json_s3_key"] == expected_key
    assert job.result["financial_year"] == "2025-26"
    assert job.result["pan"] == "AXKPS8309K"
    assert job.result["captcha_attempts"] == 2
    assert job.result["portal_date_of_birth"] == "1991-01-21"
    assert job.result["ais_logged_out"] is True
    assert job.result["logged_out"] is True
    upload_mock.assert_awaited_once_with(
        client_id, "AXKPS8309K", "2025-26", b'{"partB":{"sections":[]}}'
    )
    archive_mock.assert_awaited_once_with(
        None,
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

