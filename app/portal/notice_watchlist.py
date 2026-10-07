"""Keep the weekly e-Proceedings list in step with checks started by hand.

A single or bulk check that finds notices needing action adds the client as a
standing entry (no batch, reason ``action_required``), so the Monday runs pick
them up before the next monthly run. A later completed check of any kind that
finds nothing pending removes that entry. Entries an advisor added (reason
``manual``) are never touched, and failed checks change nothing.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import text, update
from sqlalchemy.dialects.postgresql import insert

from app.portal.notice_alerts import actionable_notices, is_scheduled
from app.portal.notice_schedule import REASON_ACTION_REQUIRED

logger = logging.getLogger(__name__)

SOURCE = "e_proceedings"
ADD = "add"
REMOVE = "remove"
AUTO_NOTE = "Added by a portal check that found notices needing action."


def watchlist_change(
    *,
    source: str,
    status: Optional[str],
    error_code: Optional[str],
    requested_by: Optional[str],
    pending_count: int,
) -> Optional[str]:
    if source != SOURCE or error_code or status != "completed":
        return None
    if pending_count == 0:
        return REMOVE
    # Scheduled runs already feed the list through the monthly batch.
    if is_scheduled(requested_by):
        return None
    return ADD


def _watch_model():
    from nucleus.models.portal_automation import EProceedingsWatchlist

    return EProceedingsWatchlist


async def _add(db, job, pending_count: int) -> None:
    watch = _watch_model()
    stmt = insert(watch).values(
        id=uuid4(),
        batch_id=None,
        client_id=job.client_id,
        reason=REASON_ACTION_REQUIRED,
        action_required_count=pending_count,
        job_id=job.id,
        added_by_sub=getattr(job, "requested_by_sub", None),
        note=AUTO_NOTE,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["client_id"],
        index_where=text("batch_id IS NULL AND removed_at IS NULL"),
        set_={
            "action_required_count": stmt.excluded.action_required_count,
            "job_id": stmt.excluded.job_id,
        },
        where=watch.reason == REASON_ACTION_REQUIRED,
    )
    await db.execute(stmt)


async def _remove(db, job, now: datetime) -> None:
    watch = _watch_model()
    await db.execute(
        update(watch)
        .where(
            watch.client_id == job.client_id,
            watch.batch_id.is_(None),
            watch.removed_at.is_(None),
            watch.reason == REASON_ACTION_REQUIRED,
        )
        .values(removed_at=now)
    )


async def sync_watchlist(
    db,
    job,
    source: str,
    *,
    notices: Optional[list[dict[str, Any]]],
    error_code: Optional[str],
) -> Optional[str]:
    """Apply the add/remove rule. Errors are logged; they never fail the job."""
    pending = actionable_notices(notices)
    change = watchlist_change(
        source=source,
        status=getattr(job, "status", None),
        error_code=error_code,
        requested_by=getattr(job, "requested_by_sub", None),
        pending_count=len(pending),
    )
    if change is None:
        return None
    try:
        async with db.begin_nested():
            if change == ADD:
                await _add(db, job, len(pending))
            else:
                await _remove(db, job, datetime.now(timezone.utc))
    except ImportError:
        logger.error("Watchlist model is not in the installed nucleus; list not updated")
        return None
    except Exception:
        logger.exception("Watchlist %s failed for job %s", change, job.id)
        return None
    return change
