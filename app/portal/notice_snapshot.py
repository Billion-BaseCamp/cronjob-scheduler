"""Write the latest harvest onto client_notices, then email the advisor.

The table is one row per client + source. Success replaces that source only.
Failure updates the last-attempt columns and leaves the previous notices.

Email goes out only when the job was queued by the cron (``requested_by_sub``
is ``cron``). A send failure never fails the job.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.dialects.postgresql import insert

from app.portal.notice_alerts import (
    ADVISOR_FIXABLE_ERRORS,
    CRON_REQUESTED_BY,
    actionable_notices,
    client_display_name,
    render_action_required_email,
    render_check_failed_email,
)

logger = logging.getLogger(__name__)

_ALERT_STATUSES = frozenset(
    {"completed", "failed", "waiting_for_password", "waiting_for_otp", "waiting_for_human"}
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp_alert(job, **fields: Any) -> None:
    result = dict(job.result) if isinstance(job.result, dict) else {}
    result.update(fields)
    job.result = result


async def _advisor_email(db, client) -> tuple[Optional[str], Optional[str]]:
    from sqlalchemy import select

    from nucleus.models.common_models.advisor import Advisor
    from nucleus.models.common_models.login import Login

    advisor_id = getattr(client, "advisor_id", None)
    if advisor_id is None and getattr(client, "parent_id", None):
        from app.portal.store import get_client

        parent = await get_client(db, client.parent_id)
        advisor_id = getattr(parent, "advisor_id", None) if parent else None
    if advisor_id is None:
        return None, None
    result = await db.execute(
        select(Login.email, Advisor.first_name)
        .join(Advisor, Advisor.id == Login.advisor_id)
        .where(
            Login.advisor_id == advisor_id,
            Login.email.isnot(None),
            Login.email.like("%@%"),
        )
        .limit(1)
    )
    row = result.first()
    if row is None:
        return None, None
    return (row.email or "").strip() or None, (row.first_name or "").strip() or None


async def _send(to_email: str, subject: str, text: str, html: str) -> bool:
    from app.clients.azure_email_client import AzureEmailClientError, get_azure_email_client
    from app.core.config import settings

    if not to_email:
        return False
    if not settings.AZURE_CLIENT_ID or not settings.EMAIL_SENDER:
        logger.info("Notice email skipped: Azure email is not configured")
        return False
    try:
        get_azure_email_client().send_mail(to_email, subject, text, body_html=html)
        return True
    except AzureEmailClientError:
        logger.exception("Notice email failed for %s", to_email)
        return False
    except Exception:
        logger.exception("Notice email failed for %s", to_email)
        return False


async def save_notice_snapshot(
    db,
    *,
    client_id,
    source: str,
    job_id,
    notices: Optional[list[dict[str, Any]]],
    error_code: Optional[str],
    error_message: Optional[str],
    fetched_at: Optional[datetime] = None,
) -> None:
    """Upsert one source. Missing model (nucleus not yet released) is logged, not raised."""
    try:
        from nucleus.models.portal_automation.client_notice import ClientNotice
    except ImportError:
        logger.error(
            "client_notices model is not in the installed nucleus; snapshot skipped"
        )
        return

    now = fetched_at or _now()
    if error_code:
        values = {
            "client_id": client_id,
            "source": source,
            "notices": [],
            "notice_count": 0,
            "action_required_count": 0,
            "fetched_at": None,
            "job_id": None,
            "last_attempt_at": now,
            "last_error_code": error_code,
            "last_error_message": (error_message or "")[:2000] or None,
        }
        update = {
            "last_attempt_at": now,
            "last_error_code": values["last_error_code"],
            "last_error_message": values["last_error_message"],
            "updated_at": now,
        }
    else:
        rows = list(notices or [])
        pending = actionable_notices(rows)
        values = {
            "client_id": client_id,
            "source": source,
            "notices": rows,
            "notice_count": len(rows),
            "action_required_count": len(pending),
            "fetched_at": now,
            "job_id": job_id,
            "last_attempt_at": now,
            "last_error_code": None,
            "last_error_message": None,
        }
        update = {
            "notices": values["notices"],
            "notice_count": values["notice_count"],
            "action_required_count": values["action_required_count"],
            "fetched_at": now,
            "job_id": job_id,
            "last_attempt_at": now,
            "last_error_code": None,
            "last_error_message": None,
            "updated_at": now,
        }

    stmt = insert(ClientNotice).values(**values)
    # WHERE belongs on ON CONFLICT DO UPDATE. Insert has no .where().
    # A slower older run must not replace a harvest that already finished.
    where = None
    if not error_code:
        where = (ClientNotice.fetched_at.is_(None)) | (ClientNotice.fetched_at < now)
    upsert = stmt.on_conflict_do_update(
        constraint="uq_client_notices_client_source",
        set_=update,
        where=where,
    )
    await db.execute(upsert)


async def record_notice_outcome(
    db,
    job,
    client,
    source: str,
    *,
    notices: Optional[list[dict[str, Any]]] = None,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
) -> None:
    """Snapshot the outcome, then email the advisor when this was a cron run."""
    try:
        await save_notice_snapshot(
            db,
            client_id=job.client_id,
            source=source,
            job_id=job.id,
            notices=notices,
            error_code=error_code,
            error_message=error_message,
            fetched_at=getattr(job, "started_at", None) or _now(),
        )
    except Exception:
        logger.exception("Could not save client_notices for job %s", job.id)

    if (getattr(job, "requested_by_sub", None) or "") != CRON_REQUESTED_BY:
        return
    if (getattr(job, "status", None) or "") not in _ALERT_STATUSES:
        return

    pending = actionable_notices(notices) if not error_code else []
    fixable = error_code in ADVISOR_FIXABLE_ERRORS
    if not pending and not fixable:
        return

    try:
        email, first_name = await _advisor_email(db, client)
    except Exception:
        logger.exception("Advisor lookup failed for job %s", job.id)
        _stamp_alert(job, alert_error="advisor lookup failed")
        return
    if not email:
        logger.info("Notice email skipped (no advisor email) job=%s", job.id)
        _stamp_alert(job, alert_error="no advisor email")
        return

    name = client_display_name(client)
    pan = getattr(client, "pan_number", None)
    if pending:
        subject, text, html = render_action_required_email(
            client_name=name,
            pan=pan,
            source=source,
            notices=pending,
            advisor_first_name=first_name,
            checked_at=_now(),
        )
    else:
        subject, text, html = render_check_failed_email(
            client_name=name,
            pan=pan,
            source=source,
            error_code=error_code or "",
            advisor_first_name=first_name,
        )
    sent = await _send(email, subject, text, html)
    if sent:
        _stamp_alert(job, alert_emailed_at=_now().isoformat(), alert_error=None)
        logger.info("Notice email sent job=%s to=%s", job.id, email)
    else:
        _stamp_alert(job, alert_error="email not sent")
