"""Keep the weekly e-Proceedings list in step with check results.

The list holds at most one active row per client:

- Any completed check that finds a notice needing action adds the client
  (reason ``action_required``), whoever started it.
- Only a completed monthly check that finds nothing pending removes the
  client. Weekly and hand-run checks never remove; until the next monthly run
  only an advisor or admin can take a client off.
- Rows an advisor added (reason ``manual``) are never removed by a check.
- Failed, waiting, and incomplete checks change nothing.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert

from app.portal.notice_alerts import actionable_notices
from app.portal.notice_schedule import REASON_ACTION_REQUIRED, RUN_E_PROCEEDINGS_MONTHLY

logger = logging.getLogger(__name__)

SOURCE = "e_proceedings"
ADD = "add"
REMOVE = "remove"


def watchlist_change(
    *,
    source: str,
    status: Optional[str],
    error_code: Optional[str],
    pending_count: int,
    monthly: bool,
) -> Optional[str]:
    if source != SOURCE or error_code or status != "completed":
        return None
    if pending_count > 0:
        return ADD
    return REMOVE if monthly else None


def _models():
    from nucleus.models.portal_automation import (
        EProceedingsWatchlist,
        PortalAutomationBatch,
    )

    return EProceedingsWatchlist, PortalAutomationBatch


async def _is_monthly(db, job) -> bool:
    batch_id = getattr(job, "batch_id", None)
    if batch_id is None:
        return False
    _, batch = _models()
    run_type = (
        await db.execute(select(batch.run_type).where(batch.id == batch_id))
    ).scalar_one_or_none()
    return run_type == RUN_E_PROCEEDINGS_MONTHLY


async def _add(db, job) -> None:
    watch, _ = _models()
    stmt = insert(watch).values(
        id=uuid4(),
        client_id=job.client_id,
        reason=REASON_ACTION_REQUIRED,
        job_id=job.id,
        added_by_sub=getattr(job, "requested_by_sub", None),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["client_id"],
        index_where=text("removed_at IS NULL"),
        set_={"job_id": stmt.excluded.job_id},
    )
    await db.execute(stmt)


async def _remove(db, job, now: datetime) -> None:
    watch, _ = _models()
    await db.execute(
        update(watch)
        .where(
            watch.client_id == job.client_id,
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
    status = getattr(job, "status", None)
    if source != SOURCE or error_code or status != "completed":
        return None
    pending_count = len(actionable_notices(notices))
    try:
        async with db.begin_nested():
            change = watchlist_change(
                source=source,
                status=status,
                error_code=error_code,
                pending_count=pending_count,
                monthly=pending_count == 0 and await _is_monthly(db, job),
            )
            if change == ADD:
                await _add(db, job)
            elif change == REMOVE:
                await _remove(db, job, datetime.now(timezone.utc))
    except ImportError:
        logger.error("Watchlist model is not in the installed nucleus; list not updated")
        return None
    except Exception:
        logger.exception("Watchlist update failed for job %s", job.id)
        return None
    return change
