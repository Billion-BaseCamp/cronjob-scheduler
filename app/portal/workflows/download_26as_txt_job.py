"""DOWNLOAD_26AS_TXT: login, open Form 168 / TRACES ATS, store .txt only, logout.

Does **not** parse or delete Part1–10. Replaces the S3 file + upload metadata in
place. Advance-tax Sync owns parsing / refreshing parts.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select

from app.core.config import settings
from app.db.database import AsyncSessionLocal
from app.portal.actions.download_form168_txt import download_form168_txt
from app.portal.actions.login import run_login
from app.portal.actions.login_outcomes import LoginOutcome
from app.portal.actions.logout import run_logout
from app.portal.actions.open_traces_form168 import open_traces_ats, run_traces_logout
from app.portal.actions.read_my_profile import read_my_profile
from app.portal.evidence import (
    Form168TxtUploadError,
    _delete_object,
    upload_form_168_txt,
    upload_screenshot,
)
from app.portal.evidence_step import (
    EVIDENCE_DOWNLOAD_26AS_TXT,
    EVIDENCE_LOGIN,
    EVIDENCE_OPEN_FORM_168,
    EVIDENCE_READ_PROFILE,
)
from app.portal.foreign_assets import normalize_pan
from app.portal.form_168_txt import (
    current_quarter_number,
    db_financial_year,
    extract_fy_from_txt,
    extract_pan_from_txt,
    form_168_s3_key,
    form_168_txt_filename,
    portal_tax_year,
    sha256_hex,
)
from app.portal.form_168_upload import Form168StoreError, remember_form_168_upload
from app.portal.store import get_client
from app.portal.workflows.filed_return_status_job import _apply_login_outcome
from nucleus.models.advance_tax_models.advance_tax_26as_upload import AdvanceTax26asUpload

logger = logging.getLogger(__name__)

STEPS = (
    "LOGIN",
    "READ_PROFILE",
    "OPEN_FORM_168",
    "DOWNLOAD_26AS_TXT",
    "STORE",
    "LOGOUT",
)
_EVIDENCE_RESULT_KEYS = ("evidence_s3_key", "evidence_step", "login_evidence_s3_key")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _merge_result(job, payload: dict[str, Any]) -> None:
    prior = job.result if isinstance(job.result, dict) else {}
    kept = {key: prior[key] for key in _EVIDENCE_RESULT_KEYS if key in prior}
    job.result = {**payload, **kept}


def _fail(job, *, error_code: str, message: str) -> None:
    job.status = "failed"
    job.error_code = error_code
    job.error_message = message
    job.worker_id = None
    job.completed_at = _now()


def _complete(
    job, *, error_code: str | None = None, message: str | None = None
) -> None:
    job.status = "completed"
    job.error_code = error_code
    job.error_message = message
    job.worker_id = None
    job.completed_at = _now()


async def _maybe_screenshot(page: Any, job_id, step_name: str) -> str | None:
    if page is None:
        return None
    try:
        png = await page.screenshot(full_page=True)
    except Exception:
        logger.exception("Screenshot failed for %s", step_name)
        return None
    try:
        return await upload_screenshot(job_id, step_name, png)
    except Exception:
        logger.exception("Evidence upload failed for %s", step_name)
        return None


async def _attach_evidence(job, page: Any, step_name: str) -> None:
    key = await _maybe_screenshot(page, job.id, step_name)
    if not key:
        return
    job.result = {
        **(job.result or {}),
        "evidence_s3_key": key,
        "evidence_step": step_name,
    }


def _result(
    workflow: str,
    *,
    tax_year: str,
    pan: str | None,
    status: str,
    logged_out: bool,
    traces_logged_out: bool | None = None,
    s3_key: str | None = None,
    upload_id: str | None = None,
    quarter_number: int | None = None,
    quarter_id: str | None = None,
    file_size_bytes: int | None = None,
    content_hash: str | None = None,
    parse_status: str | None = None,
    fetched_at: datetime | None = None,
    born_on: Any = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "workflow": workflow,
        "tax_year": tax_year,
        "financial_year": db_financial_year(tax_year),
        "pan": pan,
        "status": status,
        "s3_key": s3_key,
        "upload_id": upload_id,
        "quarter_number": quarter_number,
        "quarter_id": quarter_id,
        "file_size_bytes": file_size_bytes,
        "content_hash": content_hash,
        "logged_out": logged_out,
        "parse_status": parse_status,
    }
    if traces_logged_out is not None:
        payload["traces_logged_out"] = traces_logged_out
    if fetched_at is not None:
        payload["fetched_at"] = fetched_at.isoformat()
    if born_on is not None:
        payload["portal_date_of_birth"] = born_on.isoformat()
    if dry_run:
        payload["dry_run"] = True
    return payload


async def _fail_after_login(
    job,
    efiling: Any,
    shot: Any,
    step_name: str,
    *,
    error_code: str,
    message: str,
    payload: dict[str, Any],
    traces: Any = None,
) -> None:
    await _attach_evidence(job, shot, step_name)
    traces_logged_out = False
    if traces is not None:
        traces_logged_out = await run_traces_logout(traces)
    logged_out = await run_logout(
        None if traces is not None and traces is efiling else efiling
    )
    payload = {
        **payload,
        "traces_logged_out": traces_logged_out,
        "logged_out": logged_out,
    }
    _fail(job, error_code=error_code, message=message)
    _merge_result(job, payload)


async def _existing_upload_for_quarter(
    db, quarter_id
) -> tuple[UUID, str] | None:
    """Return ``(upload_id, s3_key)`` for the quarter, if any."""
    row = (
        await db.execute(
            select(AdvanceTax26asUpload.id, AdvanceTax26asUpload.s3_key).where(
                AdvanceTax26asUpload.quarter_id == quarter_id
            )
        )
    ).first()
    if row is None:
        return None
    return row[0], row[1]


async def _delete_s3_key(bucket: str, key: str | None, *, reason: str) -> None:
    if not bucket or not key or not key.startswith("advance-tax-26as/"):
        return
    try:
        await asyncio.to_thread(_delete_object, bucket, key)
    except Exception:
        logger.warning("Could not delete Form 168 object %s (%s)", key, reason)


async def _commit_form_168_upload_record(**kwargs):
    """Persist upload metadata in its own committed transaction (not the job session)."""
    async with AsyncSessionLocal() as store_db:
        async with store_db.begin():
            return await remember_form_168_upload(store_db, **kwargs)


async def run_download_26as_txt_job(db, job, *, workflow: str) -> None:
    try:
        tax_year = portal_tax_year(getattr(job, "assessment_year", None))
    except ValueError:
        job.current_step = STEPS[0]
        _fail(
            job,
            error_code="MISSING_TAX_YEAR",
            message="A valid Tax Year (e.g. 2026-27) is required.",
        )
        return

    client = await get_client(db, job.client_id)
    if client is None:
        job.current_step = STEPS[0]
        _fail(job, error_code="UNKNOWN", message="Client not found")
        return

    pan_fallback = normalize_pan(getattr(client, "pan_number", None))

    page = None
    browser = None
    playwright = None
    traces_page = None
    try:
        try:
            if not settings.PORTAL_AUTOMATION_DRY_RUN:
                from playwright.async_api import async_playwright

                from app.portal.browser import launch_portal_page

                playwright = await async_playwright().start()
                browser, page = await launch_portal_page(playwright)

            outcome = await run_login(client, page)
            job.current_step = STEPS[0]
            if outcome != LoginOutcome.SUCCESS:
                if outcome != LoginOutcome.MISSING_PASSWORD:
                    await _attach_evidence(job, page, EVIDENCE_LOGIN)
            if not _apply_login_outcome(job, client, outcome):
                return

            if settings.PORTAL_AUTOMATION_DRY_RUN:
                job.current_step = STEPS[5]
                logged_out = await run_logout(page)
                _complete(job)
                _merge_result(
                    job,
                    _result(
                        workflow,
                        tax_year=tax_year,
                        pan=pan_fallback,
                        status="dry_run",
                        logged_out=logged_out,
                        dry_run=True,
                    ),
                )
                return

            job.current_step = STEPS[1]
            profile = await read_my_profile(page)
            pan = profile.pan or pan_fallback
            born_on = profile.born_on
            if not pan:
                await _fail_after_login(
                    job,
                    page,
                    page,
                    EVIDENCE_READ_PROFILE,
                    error_code="UI_DRIFT",
                    message="Could not determine PAN from My Profile or client record.",
                    payload=_result(
                        workflow,
                        tax_year=tax_year,
                        pan=None,
                        status="failed",
                        logged_out=False,
                        born_on=born_on,
                    ),
                )
                return

            job.current_step = STEPS[2]
            opened = await open_traces_ats(page)
            traces_page = opened.page
            if opened.error_code or traces_page is None:
                await _fail_after_login(
                    job,
                    page,
                    traces_page or page,
                    EVIDENCE_OPEN_FORM_168,
                    error_code=opened.error_code or "UI_DRIFT",
                    message=opened.message or "Could not open Form 168 / TRACES.",
                    payload=_result(
                        workflow,
                        tax_year=tax_year,
                        pan=pan,
                        status="failed",
                        logged_out=False,
                        born_on=born_on,
                    ),
                    traces=traces_page,
                )
                return

            job.current_step = STEPS[3]
            scrape = await download_form168_txt(traces_page, tax_year)
            await _attach_evidence(job, traces_page, EVIDENCE_DOWNLOAD_26AS_TXT)

            s3_key: str | None = None
            upload_id_str: str | None = None
            quarter_id_str: str | None = None
            q_num: int | None = None
            file_size: int | None = None
            content_hash: str | None = None
            parse_status: str | None = None
            fetched_at: datetime | None = None
            upload_recorded = False

            if scrape.status == "downloaded" and scrape.payload is not None:
                raw = scrape.payload
                file_pan = extract_pan_from_txt(raw)
                if file_pan and normalize_pan(file_pan) != normalize_pan(pan):
                    scrape.status = "failed"
                    scrape.error_code = "PAN_MISMATCH"
                    scrape.error_message = (
                        f"TXT PAN {file_pan} does not match client PAN {pan}."
                    )
                else:
                    file_fy = extract_fy_from_txt(raw)
                    if file_fy and file_fy != tax_year:
                        scrape.status = "failed"
                        scrape.error_code = "FY_MISMATCH"
                        scrape.error_message = (
                            f"TXT year {file_fy} does not match Tax Year {tax_year}."
                        )

            if scrape.status == "downloaded" and scrape.payload is not None:
                job.current_step = STEPS[4]
                raw = scrape.payload
                q_num = current_quarter_number()
                filename = form_168_txt_filename(pan, tax_year)
                content_hash = sha256_hex(raw)
                file_size = len(raw)
                bucket = (settings.S3_BUCKET_NAME or "").strip()

                old_key: str | None = None
                upload_id = uuid4()
                try:
                    from app.portal.form_168_upload import resolve_fy_and_current_quarter

                    _, quarter = await resolve_fy_and_current_quarter(
                        db, client_id=job.client_id, tax_year=tax_year
                    )
                    existing = await _existing_upload_for_quarter(db, quarter.id)
                    if existing is not None:
                        upload_id, old_key = existing
                    quarter_id_str = str(quarter.id)
                except Form168StoreError as exc:
                    scrape.status = "failed"
                    scrape.error_code = exc.code
                    scrape.error_message = exc.message

                key = form_168_s3_key(job.client_id, tax_year, q_num, upload_id)

                if scrape.status == "downloaded":
                    try:
                        # Overwrite same key when reusing upload_id; never pass
                        # replace_key here — old object is removed only after DB commit.
                        s3_key = await upload_form_168_txt(
                            key=key, payload=raw, replace_key=None
                        )
                    except Form168TxtUploadError as exc:
                        scrape.status = "failed"
                        scrape.error_code = "S3_UPLOAD_FAILED"
                        scrape.error_message = exc.message
                        s3_key = None

                if scrape.status == "downloaded" and s3_key is not None:
                    try:
                        # Own transaction so the row is durable before we drop old_key.
                        upload = await _commit_form_168_upload_record(
                            client_id=job.client_id,
                            tax_year=tax_year,
                            pan=pan,
                            s3_key=s3_key,
                            bucket_name=bucket,
                            original_filename=filename,
                            file_size_bytes=file_size,
                            upload_id=upload_id,
                            content_hash=content_hash,
                        )
                        upload_id_str = str(upload.id)
                        quarter_id_str = str(upload.quarter_id)
                        parse_status = getattr(upload, "parse_status", None) or "pending"
                        upload_recorded = True
                        fetched_at = _now()
                        if old_key and old_key != s3_key and bucket:
                            await _delete_s3_key(
                                bucket, old_key, reason="replaced after DB commit"
                            )
                    except Form168StoreError as exc:
                        upload_recorded = False
                        scrape.status = "failed"
                        scrape.error_code = exc.code
                        scrape.error_message = exc.message
                        if s3_key and s3_key != old_key:
                            await _delete_s3_key(
                                bucket, s3_key, reason="DB record failed"
                            )
                    except Exception:
                        upload_recorded = False
                        logger.exception(
                            "Failed to record Form 168 upload for job %s (s3_key=%s)",
                            job.id,
                            s3_key,
                        )
                        scrape.status = "failed"
                        scrape.error_code = "UPLOAD_RECORD_FAILED"
                        scrape.error_message = (
                            "Form 168 TXT was uploaded to S3, but the upload record "
                            "could not be saved."
                        )
                        if s3_key and s3_key != old_key:
                            await _delete_s3_key(
                                bucket, s3_key, reason="DB record failed"
                            )

            job.current_step = STEPS[5]
            traces_logged_out = await run_traces_logout(traces_page)
            logged_out = await run_logout(
                None if traces_page is page else page
            )

            result_status = scrape.status
            if scrape.status == "downloaded" and not upload_recorded:
                result_status = "failed"

            payload = _result(
                workflow,
                tax_year=tax_year,
                pan=pan,
                status=result_status,
                logged_out=logged_out,
                traces_logged_out=traces_logged_out,
                s3_key=s3_key if upload_recorded else None,
                upload_id=upload_id_str if upload_recorded else None,
                quarter_number=q_num if upload_recorded else None,
                quarter_id=quarter_id_str if upload_recorded else None,
                file_size_bytes=file_size if upload_recorded else None,
                content_hash=content_hash if upload_recorded else None,
                parse_status=parse_status if upload_recorded else None,
                fetched_at=fetched_at,
                born_on=born_on,
            )

            if scrape.status == "downloaded" and not upload_recorded:
                _fail(
                    job,
                    error_code=scrape.error_code or "UPLOAD_RECORD_FAILED",
                    message=scrape.error_message
                    or "Form 168 TXT could not be recorded.",
                )
            elif scrape.status == "downloaded":
                _complete(job)
            elif scrape.status in {"no_data", "year_not_in_dropdown"}:
                _complete(
                    job,
                    error_code=scrape.error_code,
                    message=scrape.error_message,
                )
            else:
                _fail(
                    job,
                    error_code=scrape.error_code or "DOWNLOAD_FAILED",
                    message=scrape.error_message or "Form 168 TXT download failed.",
                )
            _merge_result(job, payload)
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            logger.exception("%s crashed for job %s: %s", workflow, job.id, detail)
            await _attach_evidence(job, traces_page or page, EVIDENCE_LOGIN)
            if traces_page is not None:
                await run_traces_logout(traces_page)
            if page is not None and page is not traces_page:
                try:
                    await run_logout(page)
                except Exception:
                    logger.exception("Logout after crash failed")
            _fail(
                job,
                error_code="UNKNOWN",
                message=(
                    f"Worker crashed at {job.current_step or 'LOGIN'}: {detail}"
                )[:4000],
            )
    finally:
        if browser is not None:
            await browser.close()
        if playwright is not None:
            await playwright.stop()
